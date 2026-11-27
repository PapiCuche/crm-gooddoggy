"""release_record.py (F1-20c, ADR-048 §4 and §5): every reason not to prepare or not to record. No network."""

import contextlib
import datetime as dt
import hashlib
import io
import json
import pathlib
import subprocess
import tempfile
import unittest

import release_record as rr

REPO = "PapiCuche/crm-gooddoggy"
NOW = dt.datetime(2026, 11, 27, 15, 0, 0, tzinfo=dt.timezone.utc)
D = "sha256:" + "a" * 64
MIGRATION = 'from django.db import migrations, models\n\n\nclass Migration(migrations.Migration):\n' \
    '    dependencies = [("notes", "0001_initial")]\n' \
    '    operations = [migrations.AddField(model_name="n", name="x", field=models.IntegerField(null=True))]\n'


def env_lines(sha, **over):
    values = {
        "BACKEND_IMAGE": f"{rr.REGISTRY}/backend@{D}", "FRONTEND_IMAGE": f"{rr.REGISTRY}/frontend@{D}",
        "PROXY_IMAGE": f"{rr.REGISTRY}/proxy@{D}", "POSTGRES_IMAGE": f"docker.io/library/postgres:18.6@{D}",
        "REDIS_IMAGE": f"docker.io/library/redis:8.8.3-alpine@{D}", "REVISION": sha,
    }
    values.update(over)
    return [f"{k}={v}" for k, v in values.items() if v is not None]


def run_json(sha, **over):
    run = {"id": 42, "head_sha": sha, "event": "push", "head_branch": "main", "path": rr.IMAGES_WORKFLOW,
           "status": "completed", "conclusion": "success", "repository": {"full_name": REPO},
           "head_repository": {"full_name": REPO}}
    run.update(over)
    return {"workflow_runs": [run]}


def approval(login="owner", state="approved", envs=("production",), kind="User"):
    return {"state": state, "environments": [{"name": e} for e in envs], "user": {"login": login, "type": kind}}


class Repo:
    """A throwaway git repository: base → A1 commit (sha) → C commit; a side commit off main."""

    def __init__(self, root):
        self.root = root
        self.git("init", "-q", "-b", "main")
        self.base = self.commit("docs/a.md", "x")
        self.a1 = self.commit("frontend/src/app/page.tsx", "x")
        self.migration = self.commit("backend/apps/notes/migrations/0002_x.py", MIGRATION)
        self.c = self.commit("infra/prod/deploy.sh", "x")
        self.docs = self.commit("docs/b.md", "x")
        self.git("checkout", "-q", "-b", "side", self.base)
        self.side = self.commit("frontend/src/app/other.tsx", "x")
        self.git("checkout", "-q", "main")

    def git(self, *args):
        return subprocess.run(["git", "-C", self.root, "-c", "user.name=t", "-c", "user.email=t@t", *args],
                              check=True, capture_output=True, text=True).stdout.strip()

    def commit(self, path, text):
        f = pathlib.Path(self.root, path)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
        self.git("add", path)
        self.git("commit", "-q", "-m", f"add {path}")
        return self.git("rev-parse", "HEAD")


class InputsAndImagesRunTest(unittest.TestCase):
    sha = "1" * 40

    def test_sha_must_be_40_lowercase_hex(self):
        for bad in ("", "1" * 39, "A" * 40, "1" * 41, "1" * 39 + "g", f"{'1' * 40}\n"):
            with self.subTest(bad=bad), self.assertRaises(rr.Refused):
                rr.sha_arg(bad, "sha")
        self.assertEqual(rr.sha_arg(self.sha, "sha"), self.sha)

    def test_the_single_successful_push_run_of_main_in_this_repository(self):
        self.assertEqual(rr.images_run(run_json(self.sha), self.sha, REPO), 42)
        bad = {"head_sha": "2" * 40, "event": "pull_request", "head_branch": "feature", "status": "in_progress",
               "path": ".github/workflows/images-pr.yml", "conclusion": "failure",
               "repository": {"full_name": "fork/crm"}, "head_repository": {"full_name": "fork/crm"}, "id": 0}
        for key, value in bad.items():
            with self.subTest(key=key), self.assertRaises(rr.Refused):
                rr.images_run(run_json(self.sha, **{key: value}), self.sha, REPO)
        for runs in ({"workflow_runs": []}, {}, {"workflow_runs": run_json(self.sha)["workflow_runs"] * 2}):
            with self.subTest(runs=len(runs.get("workflow_runs", []))), self.assertRaises(rr.Refused):
                rr.images_run(runs, self.sha, REPO)

    def test_publish_and_attest_of_that_run_succeeded(self):
        ok = [{"run_id": 42, "name": "images publish", "conclusion": "success"},
              {"run_id": 42, "name": "images attest", "conclusion": "success"}]
        rr.jobs_ok({"jobs": ok}, 42)
        for jobs in (ok[:1], [ok[0], {**ok[1], "conclusion": "failure"}], [ok[0], {**ok[1], "conclusion": "skipped"}],
                     [ok[0], {**ok[1], "run_id": 7}], []):
            with self.subTest(jobs=jobs), self.assertRaises(rr.Refused):
                rr.jobs_ok({"jobs": jobs}, 42)


class ReleaseEnvTest(unittest.TestCase):
    sha = "1" * 40

    def test_strict_release_env(self):
        self.assertEqual(rr.release_env("\n".join(env_lines(self.sha)) + "\n", self.sha), sorted(env_lines(self.sha)))
        cases = {
            "missing image": env_lines(self.sha, REDIS_IMAGE=None),
            "tag, not digest": env_lines(self.sha, BACKEND_IMAGE=f"{rr.REGISTRY}/backend:latest"),
            "foreign registry": env_lines(self.sha, PROXY_IMAGE=f"ghcr.io/evil/crm/proxy@{D}"),
            "short digest": env_lines(self.sha, FRONTEND_IMAGE=f"{rr.REGISTRY}/frontend@{D[:-1]}"),
            "other revision": env_lines(self.sha, REVISION="2" * 40),
            "extra line": env_lines(self.sha) + ["EXTRA=1"],
            "repeated line": env_lines(self.sha) + [env_lines(self.sha)[0]],
            "blank line": env_lines(self.sha)[:2] + [""] + env_lines(self.sha)[2:],
            "two trailing newlines": env_lines(self.sha) + ["", ""],
            "suffix": env_lines(self.sha, POSTGRES_IMAGE=f"docker.io/library/postgres:18.6@{D} x"),
        }
        for name, lines in cases.items():
            with self.subTest(name), self.assertRaises(rr.Refused):
                rr.release_env("\n".join(lines), self.sha)

    def test_fingerprint_is_the_canonical_sorted_release_env(self):
        lines = env_lines(self.sha)
        expected = "sha256:" + hashlib.sha256("".join(f"{x}\n" for x in sorted(lines)).encode()).hexdigest()
        self.assertEqual(rr.fingerprint(lines), expected)
        self.assertEqual(rr.fingerprint(list(reversed(lines))), expected)
        other = env_lines(self.sha, BACKEND_IMAGE=f"{rr.REGISTRY}/backend@sha256:{'b' * 64}")
        self.assertNotEqual(rr.fingerprint(other), expected)


class ApprovalsTest(unittest.TestCase):
    def check(self, entries, actors=("agent",)):
        return rr.approved_by(entries, rr.approvers("Owner, suplente"), set(actors))

    def test_only_listed_users_who_did_not_trigger_the_run(self):
        self.assertEqual(self.check([approval("OWNER")]), ["owner"])
        self.assertEqual(self.check([approval("owner"), approval("suplente")]), ["owner", "suplente"])
        refused = {
            "no approval (environment without reviewers)": [],
            "not a list": {"state": "approved"},
            "rejected": [approval(state="rejected")],
            "pending": [approval(state="pending")],
            "rejected after an approval": [approval(), approval("suplente", state="rejected")],
            "other environment": [approval(envs=("staging",))],
            "two environments": [approval(envs=("production", "staging"))],
            "no environment": [approval(envs=())],
            "not in CD_APPROVERS": [approval("intruder")],
            "listed plus an unlisted approver": [approval(), approval("intruder")],
            "a bot": [approval(kind="Bot")],
            "no user": [{"state": "approved", "environments": [{"name": "production"}]}],
        }
        for name, entries in refused.items():
            with self.subTest(name), self.assertRaises(rr.Refused):
                self.check(entries)

    def test_who_triggered_the_run_never_approves_it(self):
        for actors in (("owner",), ("agent", "Owner")):
            with self.subTest(actors=actors), self.assertRaises(rr.Refused):
                self.check([approval("owner")], {a.lower() for a in actors})

    def test_approvers_list(self):
        self.assertEqual(rr.approvers(" Owner,suplente  x-1 "), {"owner", "suplente", "x-1"})
        for raw in ("", " , ", "owner;rm", "-owner", "a" * 40):
            with self.subTest(raw=raw), self.assertRaises(rr.Refused):
                rr.approvers(raw)


class PrepareAndApproveTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.repo = Repo(cls.tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def path(self, name, content):
        p = pathlib.Path(self.tmp.name, "..", f"{pathlib.Path(self.tmp.name).name}-{name}").resolve()
        p.write_text(content if isinstance(content, str) else json.dumps(content))
        self.addCleanup(p.unlink, missing_ok=True)
        return str(p)

    def main(self, *argv, now=NOW):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = rr.main(list(argv), now=now)
        return code, out.getvalue(), err.getvalue()

    def prepare(self, sha, base=None):
        env = self.path("release.env", "\n".join(env_lines(sha)) + "\n")
        out = self.path("prepared", "")
        code, summary, err = self.main("prepare", "--sha", sha, "--base", base or self.repo.base, "--images-run-id",
                                       "42", "--release-env", env, "--repo", self.repo.root, "--out", out)
        return code, summary, err, dict(line.split("=", 1) for line in pathlib.Path(out).read_text().splitlines())

    def approve(self, sha, prepared, now=NOW, entries=None, **over):
        args = {"--sha": sha, "--base": self.repo.base, "--images-run-id": "42", "--run-id": "9001",
                "--run-attempt": "2", "--repository": REPO,
                "--release-env": self.path("release.env", "\n".join(env_lines(sha))),
                "--fingerprint": prepared["fingerprint"], "--class": prepared["class"],
                "--prepared-at": prepared["prepared_at"], "--repo": self.repo.root,
                "--approvals": self.path("approvals.json", [approval()] if entries is None else entries),
                "--approvers": "owner", "--actor": "agent", "--triggering-actor": "agent",
                "--out": self.path("deployment.json", "")}
        args.update(over)
        code, _, err = self.main("approve", *[x for kv in args.items() for x in kv], now=now)
        text = pathlib.Path(args["--out"]).read_text()
        return code, err, json.loads(text) if code == 0 else text

    def test_prepare_records_the_tuple_and_a_readable_summary(self):
        code, summary, err, prepared = self.prepare(self.repo.migration)
        self.assertEqual(code, 0, err)
        self.assertEqual(prepared, {"sha": self.repo.migration, "base": self.repo.base, "class": prepared["class"],
                                    "fingerprint": rr.fingerprint(env_lines(self.repo.migration)),
                                    "images_run_id": "42", "prepared_at": "2026-11-27T15:00:00Z"})
        self.assertIn(prepared["class"], rr.APPROVABLE)
        self.assertIn("Rige la fase 0: el GO exacto por SHA", summary)
        self.assertIn("backend/apps/notes/migrations/0002_x.py", summary)
        self.assertIn(f"REVISION={self.repo.migration}", summary)
        self.assertNotIn("no bloquea nada", summary)

    def test_prepare_refuses_class_c_none_backwards_and_outside_main(self):
        cases = {"class C": (self.repo.c, None), "none": (self.repo.docs, self.repo.c),
                 "empty range": (self.repo.base, None), "backwards": (self.repo.base, self.repo.a1),
                 "not in main": (self.repo.side, None), "bad base": (self.repo.a1, "x" * 40)}
        for name, (sha, base) in cases.items():
            with self.subTest(name):
                code, summary, err, prepared = self.prepare(sha, base)
                self.assertEqual((code, summary, prepared), (1, "", {}))
                self.assertIn("error:", err)

    def test_approve_writes_the_bound_record(self):
        _, _, _, prepared = self.prepare(self.repo.a1)
        self.assertEqual(prepared["class"], "A1")
        code, err, _ = self.approve(self.repo.a1, prepared, now=NOW + rr.VALID_FOR)  # 24 h exactas: vale
        self.assertEqual(code, 0, err)
        code, err, request = self.approve(self.repo.a1, prepared, now=NOW + dt.timedelta(hours=1))
        self.assertEqual(code, 0, err)
        self.assertEqual(request, {
            "ref": self.repo.a1, "task": "crm-release", "environment": "production", "auto_merge": False,
            "required_contexts": [], "production_environment": True, "transient_environment": False,
            "description": f"Release {self.repo.a1[:12]} class A1, approved (ADR-048 §5)",
            "payload": {"schema": 1, "sha": self.repo.a1, "fingerprint": prepared["fingerprint"],
                        "base": self.repo.base, "class": "A1", "repository": REPO, "run_id": "9001",
                        "run_attempt": "2", "images_run_id": "42",
                        "approved_by": ["owner"], "prepared_at": "2026-11-27T15:00:00Z",
                        "approved_at": "2026-11-27T16:00:00Z", "expires_at": "2026-11-28T16:00:00Z"}})

    def test_approve_refuses_anything_that_changed_or_expired(self):
        _, _, _, prepared = self.prepare(self.repo.a1)
        second = dt.timedelta(seconds=1)
        cases = {
            "after 24 h": {"now": NOW + rr.VALID_FOR + second},
            "prepared in the future": {"now": NOW - second},
            "other fingerprint (images rerun)": {"--fingerprint": "sha256:" + "0" * 64},
            "other class": {"--class": "B"},
            "class C": {"--sha": self.repo.c, "--class": "C"},
            "other base": {"--base": self.repo.a1},
            "no approval": {"entries": []},
            "self-approval": {"--triggering-actor": "owner"},
            "actor approval": {"--actor": "Owner"},
            "no approvers": {"--approvers": ""},
            "invalid run id": {"--run-id": "0"}, "invalid run attempt": {"--run-attempt": "1 x"},
            "invalid repository": {"--repository": "a/b/c"},
            "invalid images run id": {"--images-run-id": "42; x"},
            "invalid prepared_at": {"--prepared-at": "yesterday"},
            "invalid sha": {"--sha": "HEAD"},
        }
        for name, over in cases.items():
            with self.subTest(name):
                now, entries = over.pop("now", NOW), over.pop("entries", None)
                sha = over.pop("--sha", self.repo.a1)
                code, err, text = self.approve(sha, prepared, now=now, entries=entries, **over)
                self.assertEqual((code, text), (1, ""))
                self.assertIn("error:", err)

    def test_images_run_command_prints_only_the_id(self):
        runs = self.path("runs", run_json("1" * 40))
        for sha, expected in (("1" * 40, (0, "42\n")), ("x", (1, ""))):
            code, out, _ = self.main("images-run", "--sha", sha, "--repository", REPO, "--runs", runs)
            self.assertEqual((code, out), expected)

if __name__ == "__main__":
    unittest.main()
