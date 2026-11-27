"""infra/prod/cd_agent.py (F1-20e1, ADR-048 §4, §5, §7, §11): every reason not to declare a release deployable.

No network: a fake GitHub opener serves canned API answers; real throwaway git repositories (fetched from a
local path); fake cd-preflight.sh and verify_attestations.py. The token canary must never leave its header.
"""

import contextlib
import datetime as dt
import importlib.util
import io
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import zipfile
from unittest import mock

import release_record as rr
from test_release_record import Repo, approval, env_lines, run_json

PATH = pathlib.Path(__file__).resolve().parents[2] / "infra/prod/cd_agent.py"
spec = importlib.util.spec_from_file_location("cd_agent", PATH)
agent = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agent)

REPO = agent.REPO
NOW = dt.datetime(2026, 11, 27, 15, 0, 0, tzinfo=dt.timezone.utc)
TOKEN = "canaryTOKEN_" + "q" * 30
DEPLOYMENTS = "deployments?task=crm-release&environment=production&per_page=10"
REAL_RUN = subprocess.run
REMOTE_RE = agent.REMOTE_RE  # the real one; setUp swaps it for local paths


def t(hours_ago):
    return (NOW - dt.timedelta(hours=hours_ago)).strftime(rr.TIME)


def job(name, run_id=7, attempt=1, started=1):
    return {"name": name, "run_id": run_id, "run_attempt": attempt, "conclusion": "success", "started_at": t(started)}


def zipped(text, name="release.env"):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr(name, text)
    return out.getvalue()


class Opener:
    """GitHub API double: routes by path; an int is an HTTP error; `not_modified` answers 304 to the ETag."""

    def __init__(self, routes):
        self.routes, self.requests, self.not_modified = routes, [], False

    def open(self, req, timeout):
        self.requests.append(req)
        value = self.routes.get(req.full_url.removeprefix(f"{agent.API}/repos/{REPO}/"), 404)
        if self.not_modified and req.get_header("If-none-match") == '"v1"':
            value = 304
        if isinstance(value, int):
            raise urllib.error.HTTPError(req.full_url, value, "fake", {}, None)
        if isinstance(value, Exception):
            raise value
        resp = io.BytesIO(value if isinstance(value, bytes) else json.dumps(value).encode())
        resp.headers = {"ETag": '"v1"'}
        return resp


class AgentTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = pathlib.Path(tmp.name)
        (d / "git").mkdir()
        self.repo = Repo(str(d / "git"))
        self.sha, self.base = self.repo.a1, self.repo.base
        (d / "state").mkdir()
        (d / "etc").mkdir()
        self.state, self.conf = d / "state", d / "etc/cd.conf"
        (d / "etc/release.env").write_text(f"REVISION={self.base}\n")
        self.token = d / "etc/cd.env"
        self.token.write_text(f"CD_GITHUB_TOKEN={TOKEN}\n")
        self.token.chmod(0o600)
        self.settings = {"MODE": "approve", "CD_APPROVERS": "owner, deputy", "TOKEN_FILE": str(self.token),
                         "STATE_DIR": str(self.state), "ENV_DIR": str(d / "etc"), "GIT_DIR": self.repo.root,
                         "GIT_REMOTE": self.repo.root}
        payload = {"schema": 1, "sha": self.sha, "fingerprint": rr.fingerprint(env_lines(self.sha)),
                   "base": self.base, "class": "A1", "repository": REPO, "run_id": "7", "run_attempt": "1",
                   "images_run_id": "42", "approved_by": ["owner"], "approved_at": t(1), "expires_at": t(-23)}
        self.record = {"id": 900, "sha": self.sha, "ref": self.sha, "task": "crm-release",
                       "environment": "production", "created_at": t(1), "payload": payload,
                       "creator": {"login": "github-actions[bot]", "type": "Bot", "id": 41898282}}
        self.run = {"id": 7, "path": ".github/workflows/release.yml", "event": "workflow_dispatch",
                    "head_branch": "main", "head_sha": self.repo.docs, "status": "completed", "conclusion": "success",
                    "run_attempt": 1, "display_title": f"release {self.sha} base {self.base}",
                    "repository": {"full_name": REPO}, "head_repository": {"full_name": REPO},
                    "actor": {"login": "agent-bot"}, "triggering_actor": {"login": "agent-bot"}}
        self.jobs = {"total_count": 2, "jobs": [job("release prepare", started=2), job("release approve")]}
        self.images_jobs = {"total_count": 2, "jobs": [job(n, 42) for n in rr.IMAGES_JOBS]}
        self.approvals = [approval("owner")]
        self.images_runs = run_json(self.sha, run_attempt=1, run_started_at=t(4))
        self.artifact = {"id": 5, "name": "release-env", "expired": False, "created_at": t(3),
                         "workflow_run": {"id": 42, "head_sha": self.sha}}  # before prepare (2 h ago)
        self.routes = {
            DEPLOYMENTS: [self.record], "actions/runs/7": self.run,
            "actions/runs/7/attempts/1/jobs?per_page=100": self.jobs, "actions/runs/7/approvals": self.approvals,
            f"actions/workflows/images.yml/runs?head_sha={self.sha}&per_page=100": self.images_runs,
            "actions/runs/42/jobs?filter=latest&per_page=100": self.images_jobs,
            "actions/runs/42/artifacts?name=release-env": {"artifacts": [self.artifact]},
            "actions/artifacts/5/zip": zipped("\n".join(env_lines(self.sha)) + "\n"),
        }
        self.opener = Opener(self.routes)
        self.argv = []
        for name, value in (("REMOTE_RE", re.compile(".+")), ("STATE_DIR", str(self.state)),  # a local path stands in for the https remote
                            ("PREFLIGHT", [sys.executable, "-c", "print('PREFLIGHT OK')"]),
                            ("ATTEST", [sys.executable, "-c", "import sys; sys.exit(sys.argv[1:3] != ['--sha', "
                                        f"'{self.sha}'])"])):
            patcher = mock.patch.object(agent, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        spy = mock.patch.object(subprocess, "run", side_effect=self.spy)
        spy.start()
        self.addCleanup(spy.stop)

    def spy(self, argv, **kwargs):
        self.argv.append((list(argv), dict(kwargs.get("env") or {})))
        return REAL_RUN(argv, **kwargs)

    def tearDown(self):
        # The token only ever travels in an unredirected header to api.github.com; never in argv or env.
        for req in self.opener.requests:
            self.assertTrue(req.full_url.startswith("https://api.github.com/repos/"))
            self.assertNotIn(TOKEN, req.full_url)
            self.assertNotIn("Authorization", req.headers)
            self.assertEqual(req.unredirected_hdrs["Authorization"], f"Bearer {TOKEN}")
        self.assertNotIn(TOKEN, repr(self.argv))

    def cycle(self, now=NOW, mode=0o644, **settings):
        if mode:
            self.conf.write_text("".join(f"{k}={v}\n" for k, v in {**self.settings, **settings}.items()))
            self.conf.chmod(mode)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = agent.once(str(self.conf), self.opener, now)
        self.assertNotIn(TOKEN, out.getvalue())
        return code, out.getvalue()

    def refused(self, reason, code=0, **settings):
        got, text = self.cycle(**settings)
        self.assertIn(reason, text)
        self.assertNotIn("DEPLOYABLE", text)
        self.assertEqual(got, code, text)
        self.assertFalse((self.state / "deployable.json").exists())
        return text

    def set_sha(self, sha):
        for obj in (self.record, self.record["payload"]):
            obj.update({"sha": sha, "ref": sha} if obj is self.record else {"sha": sha})
        self.run["display_title"] = f"release {sha} base {self.base}"

    def test_deployable_only_with_every_check_green(self):
        (self.state / "deployable.json.tmp").write_text("stale")
        (self.state / "deployable.json.tmp").chmod(0o666)  # a stale .tmp never lends its mode
        code, text = self.cycle()
        self.assertEqual(code, 0, text)
        self.assertIn(f"DEPLOYABLE record 900: {self.sha} class A1", text)
        result = json.loads((self.state / "deployable.json").read_text())
        self.assertEqual((result["approved_at"], result["approved_by"], result["base"]), (t(1), ["owner"], self.base))
        self.assertEqual(os.stat(self.state / "deployable.json").st_mode & 0o777, 0o600)
        self.assertTrue(any(a[:2] == agent.PREFLIGHT[:2] for a, _ in self.argv))
        self.assertFalse(any("deploy.sh" in " ".join(a) for a, _ in self.argv))  # never deploys

    def test_any_mode_but_approve_is_off_without_network(self):
        other = self.state.with_name("other")  # a STATE_DIR other than the default is cleared too
        other.mkdir()
        for mode in ("off", "auto", "AUTO", "", "deploy", "approve,auto"):
            with self.subTest(mode=mode):
                (other / "deployable.json").write_text("{}")
                self.refused("mode off", MODE=mode, STATE_DIR=str(other))
                self.assertFalse((other / "deployable.json").exists())
        self.assertEqual((self.opener.requests, self.argv), ([], []))

    def test_forged_records_from_anywhere_but_release_yml_on_main(self):
        cases = [(self.record["creator"], {"login": "owner", "type": "User", "id": 1}, "github-actions[bot]"),
                 (self.record, {"task": "deploy"}, "not a crm-release record"),
                 (self.record, {"ref": self.repo.docs}, "record sha/ref differ"),
                 (self.record["creator"], {"id": 1}, "github-actions[bot]"),  # a user can take the login
                 (self.run, {"path": ".github/workflows/ci.yml"}, "release run: path"),
                 (self.run, {"event": "push"}, "release run: event"),
                 (self.run, {"head_branch": "feature"}, "release run: head_branch"),
                 (self.run, {"display_title": "release x"}, "release run: display_title"),
                 (self.run, {"conclusion": "failure"}, "release run: conclusion"),
                 (self.run, {"head_repository": {"full_name": "fork/crm"}}, "not from this repository"),
                 (self.record["payload"], {"repository": "fork/crm"}, "another repository")]
        for obj, change, reason in cases:
            with self.subTest(reason=reason), mock.patch.dict(obj, change):
                self.refused(reason)

    def test_head_branch_main_from_a_tag_is_not_main(self):
        self.run["head_sha"] = self.repo.side  # a tag named main gives head_branch == main
        self.refused("head_sha not in main")

    def test_sha_not_an_ancestor_of_main(self):
        self.set_sha(self.repo.side)
        self.refused("the SHA is not an ancestor of main")

    def test_unauthorized_approver_self_approval_and_rejection(self):
        for approvals, run, reason in (([approval("mallory")], {}, "not in CD_APPROVERS"),
                                       ([approval("owner")], {"actor": {"login": "Owner"}}, "self-approval"),
                                       ([approval("owner")], {"triggering_actor": {"login": "owner"}}, "self-approval"),
                                       ([approval("owner"), approval("deputy", "rejected")], {}, "approval state"),
                                       ([], {}, "no approval")):
            with self.subTest(reason=reason), mock.patch.dict(self.run, run):
                self.approvals[:] = approvals
                self.refused(reason)

    def test_tuple_is_recomputed(self):
        other = zipped("\n".join(env_lines(self.sha, BACKEND_IMAGE=f"{rr.REGISTRY}/backend@sha256:{'b' * 64}")))
        cases = [("actions/artifacts/5/zip", other, "fingerprint differs"),
                 ("actions/artifacts/5/zip", zipped("x", "other.txt"), "not exactly one small release.env"),
                 ("actions/runs/42/artifacts?name=release-env", {"artifacts": []}, "release-env artifact"),
                 (f"actions/workflows/images.yml/runs?head_sha={self.sha}&per_page=100",
                  run_json(self.sha, id=43), "not THE images run")]
        for path, value, reason in cases:
            with self.subTest(reason=reason), mock.patch.dict(self.routes, {path: value}):
                self.refused(reason)
        with mock.patch.dict(self.record["payload"], {"class": "A2"}):
            self.refused("class recomputed as A1")
        pathlib.Path(self.settings["ENV_DIR"], "release.env").write_text(f"REVISION={self.repo.docs}\n")
        self.refused("base is not the REVISION in production")

    def test_approval_time_is_the_approve_job_start_not_the_payload(self):
        self.jobs["jobs"][1]["started_at"] = t(25)  # the forged payload still says approved 1 h ago
        self.refused("approval expired")
        self.jobs["jobs"][1]["started_at"] = t(-1)
        self.refused("approval expired")

    def test_exactly_one_job_per_name(self):
        self.jobs["jobs"].insert(0, job("release approve", started=30))
        self.jobs["total_count"] = 3
        self.refused("two jobs named 'release approve'")
        self.jobs["jobs"].pop(0)
        self.jobs["total_count"] = 2
        self.images_jobs["jobs"].append(job("images publish", 42))
        self.images_jobs["total_count"] = 3
        self.refused("two jobs named 'images publish'")

    def test_rerun_with_another_run_attempt(self):
        self.run["run_attempt"] = 2
        self.refused("run_attempt is '2'")
        self.run["run_attempt"] = 1
        self.jobs["jobs"][1]["run_attempt"] = 2
        self.refused("another run or attempt")

    def test_preflight_or_provenance_red(self):
        with mock.patch.object(agent, "ATTEST", [sys.executable, "-c", "exit(1)"]):
            self.refused("verify_attestations.py failed")
        with mock.patch.object(agent, "PREFLIGHT", [sys.executable, "-c", "print('BLOQUEA lock: x'); exit(1)"]):
            self.assertIn("preflight: BLOQUEA lock", self.refused("cd-preflight.sh is not green"))

    def test_every_field_is_validated(self):
        p = self.record["payload"]
        for key, bad in (("sha", self.sha.upper()), ("base", self.base[:39]), ("fingerprint", "sha256:abc"),
                         ("run_id", "7; id"), ("run_attempt", True), ("images_run_id", "0"), ("class", "C"),
                         ("run_id", None)):
            with self.subTest(key=key, bad=bad), mock.patch.dict(p, {key: bad}):
                self.refused("not deployable")
        self.assertFalse([r for r in self.opener.requests if r.full_url.endswith("/actions/runs/7")])  # before any use

    def test_304_reverifies_the_cached_body_and_the_window_moves(self):
        self.assertEqual(self.cycle()[0], 0)
        self.opener.not_modified, first = True, len(self.opener.requests)
        self.assertIn("DEPLOYABLE", self.cycle()[1])
        self.assertEqual(self.opener.requests[first].get_header("If-none-match"), '"v1"')
        self.refused("nothing deployable", now=NOW + dt.timedelta(hours=24))

    def test_fail_closed_on_api_network_git_and_config_errors(self):
        for value, reason in ((403, "answered 403"), (429, "answered 429"), (500, "answered 500"),
                              (b"{not json", "invalid JSON"), ({"a": 1}, "not a list"),
                              (urllib.error.URLError("down"), "unreachable")):
            with self.subTest(reason=reason), mock.patch.dict(self.routes, {DEPLOYMENTS: value}):
                self.refused(reason, code=1)
        self.refused("cannot fetch main", code=1, GIT_REMOTE=self.repo.root + "/missing")
        self.refused("nobody can approve", CD_APPROVERS="")  # no list: nobody can approve

    def test_token_file_must_be_private_and_remote_without_credentials(self):
        self.token.chmod(0o640)
        self.refused("must be a regular file of root, mode 0600", code=1)
        self.token.chmod(0o600)
        self.token.write_text(f"GITHUB_TOKEN={TOKEN}\n")
        self.refused("no valid CD_GITHUB_TOKEN", code=1)
        for bad in ("short", f"{TOKEN} x", f"{TOKEN}:x"):
            self.token.write_text(f"CD_GITHUB_TOKEN={bad}\n")
            self.refused("no valid CD_GITHUB_TOKEN", code=1)
        self.token.write_text(f"CD_GITHUB_TOKEN={TOKEN}\n")
        self.refused("must be a regular file of root, mode go-w", code=1, mode=0o664)  # group-writable cd.conf
        link = self.token.with_name("link")
        link.symlink_to(self.token)
        self.refused("not deployable: OSError", code=1, TOKEN_FILE=str(link))  # O_NOFOLLOW
        self.assertEqual(self.opener.requests, [])
        with mock.patch.object(agent, "REMOTE_RE", REMOTE_RE):
            self.refused("GIT_REMOTE must be", code=1, GIT_REMOTE=f"https://x:{TOKEN}@github.com/a/b")
        for url in ("http://github.com/a/b", "ssh://github.com/a/b", "file:///srv/repo", "https://u@github.com/a/b"):
            self.assertIsNone(REMOTE_RE.fullmatch(url))
        self.assertTrue(REMOTE_RE.fullmatch(agent.REMOTE))
        with self.assertRaises(agent.Refused):
            agent.HttpsOnly().redirect_request(None, None, 302, "", {}, "http://blob.example/x")

    def test_images_rerun_after_the_preparation_is_refused(self):
        # A re-run of the same images run (same id, still green) with a new artifact and a forged record that
        # carries the new fingerprint: the digests are not the approved ones (ADR-048 §5).
        lines = env_lines(self.sha, BACKEND_IMAGE=f"{rr.REGISTRY}/backend@sha256:{'b' * 64}")
        self.routes["actions/artifacts/5/zip"] = zipped("\n".join(lines))
        self.record["payload"]["fingerprint"] = rr.fingerprint(lines)
        self.artifact["created_at"] = t(1.5)
        self.refused("created after the preparation")
        for change, reason in (({"workflow_run": {"id": 43, "head_sha": self.sha}}, "not of that run"),
                               ({"workflow_run": {"id": 42, "head_sha": self.repo.docs}}, "not of that run"),
                               ({"created_at": None}, "artifact created_at is not")):
            with self.subTest(change=change), mock.patch.dict(self.artifact, {"created_at": t(3), **change}):
                self.refused(reason)
        self.artifact["created_at"] = t(3)
        self.images_runs["workflow_runs"][0].update(run_attempt=2, run_started_at=t(1.5))
        self.refused("images run re-run after the preparation")
        self.images_runs["workflow_runs"][0].update(run_attempt=1, run_started_at=t(4))
        self.artifact["expired"] = True
        self.refused("not exactly one live release-env artifact")

    def test_incomplete_job_list(self):
        self.jobs["total_count"] = 3  # another page exists: a duplicate could hide there
        self.refused("incomplete list")

    def test_no_old_verdict_survives_an_unreadable_or_missing_config(self):
        for breaks in (lambda: self.conf.chmod(0o666), self.conf.unlink):
            self.assertIn("DEPLOYABLE", self.cycle()[1])
            breaks()
            self.refused("not deployable", code=1, mode=None)


if __name__ == "__main__":
    unittest.main()
