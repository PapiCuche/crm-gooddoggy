#!/usr/bin/env python3
"""CD server agent: polls GitHub and decides, with its own data, whether an approved release is deployable.

    sudo python3 infra/prod/cd_agent.py [--config /etc/crm/cd.conf] [--loop]

F1-20e1 (ADR-048 §4, §5, §7 and §11). One cycle per run; --loop repeats it every 2 min. It NEVER deploys:
in mode `approve` it only writes STATE_DIR/deployable.json and logs DEPLOYABLE (deploy.sh is F1-20e2).
Installing it on the server or changing its mode is the owner's decision (amendment E1); no unit ships here.

/etc/crm/cd.conf (root, no group/other write), KEY=value lines:
    MODE=off|approve     anything else, `auto` included (phase 2, not authorized), is off: no network at all
    CD_APPROVERS=login[,login]   the server's own list (§4); never taken from GitHub
    TOKEN_FILE=/etc/crm/cd.env   root, 0600, one line CD_GITHUB_TOKEN=<fine-grained read token> (decision 4)
    STATE_DIR=/var/lib/crm-cd  ENV_DIR=/etc/crm  GIT_DIR=<this checkout>  GIT_REMOTE=<https URL, no credential>

A crm-release deployment record is data, not authentication (any workflow can POST one). It is deployable only
if EVERY check of verify() passes; anything else (network, 304 without cache, 403/429, JSON, a missing field,
any exception) is "not deployable" and deployable.json is removed first. The token goes only in an
unredirected Authorization header to api.github.com: never in argv, environment, files or logs. Received data
is validated (40 hex, sha256: + 64 hex, numeric ids) and reaches subprocesses only as list arguments.
"""

from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import os
import pathlib
import re
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / ".github/scripts"))
import release_record as rr  # noqa: E402  (same checks and fingerprint as release.yml)
from deploy_class import classify  # noqa: E402

Refused = rr.Refused
REPO = "PapiCuche/crm-gooddoggy"
API = "https://api.github.com"
REMOTE = f"https://github.com/{REPO}.git"
REMOTE_RE = re.compile(r"https://[A-Za-z0-9.-]+(:[0-9]{1,5})?/[\w.-]+/[\w.-]+")  # no user:token@
MAIN = "refs/remotes/cd-agent/main"
BOT = ("github-actions[bot]", "Bot", 41898282)  # the only creator of a crm-release record (§11)
WORKFLOW = ".github/workflows/release.yml"
JOBS = ("release prepare", "release approve")
EVERY = 120  # seconds between polls (§11)
NUM = re.compile(r"[1-9][0-9]{0,19}")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
TOKEN = re.compile(r"[A-Za-z0-9_]{20,255}")
ETAG = re.compile(r'(W/)?"[\w.:+/=-]{1,200}"')
LIMIT = 4 << 20
ENV = {"PATH": "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin", "LC_ALL": "C", "GIT_TERMINAL_PROMPT": "0",
       "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}  # no token, no credential helper
PREFLIGHT = [str(ROOT / "infra/prod/cd-preflight.sh")]
ATTEST = [sys.executable, "-I", str(ROOT / "infra/prod/verify_attestations.py")]


def private_file(path: str, secret: bool) -> str:
    """A regular file of root (or of whoever runs the tests), not a symlink, 0600 if it holds a secret."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid not in (0, os.getuid()) or \
                info.st_mode & (0o077 if secret else 0o022):
            raise Refused(f"{path} must be a regular file of root, mode {'0600' if secret else 'go-w'}")
        return os.read(fd, 1 << 16).decode()
    finally:
        os.close(fd)


def parse(text: str) -> dict:
    return dict(line.strip().split("=", 1) for line in text.splitlines() if "=" in line and line[:1] != "#")


def num(value, what: str) -> str:
    text = str(value) if isinstance(value, (int, str)) and not isinstance(value, bool) else ""
    if not NUM.fullmatch(text):
        raise Refused(f"{what} is not a numeric id")
    return text


def when(value, what: str) -> dt.datetime:
    try:
        return dt.datetime.strptime(value, rr.TIME).replace(tzinfo=dt.timezone.utc)
    except (TypeError, ValueError):
        raise Refused(f"{what} is not a UTC time") from None


class HttpsOnly(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not newurl.startswith("https://"):
            raise Refused("redirect to a non-https URL")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class GitHub:
    def __init__(self, token: str, opener=None) -> None:
        self._token = token
        self.opener = opener or urllib.request.build_opener(HttpsOnly)

    def get(self, path: str, etag: str | None = None, raw: bool = False):
        """(body, ETag). body None on a 304. Any other non-2xx, network or JSON error refuses."""
        req = urllib.request.Request(API + path, headers={"Accept": "application/vnd.github+json",
                                                          "X-GitHub-Api-Version": "2022-11-28",
                                                          "User-Agent": "crm-cd-agent"})
        req.add_unredirected_header("Authorization", f"Bearer {self._token}")  # not to the artifact blob
        if etag:
            req.add_header("If-None-Match", etag)
        try:
            with self.opener.open(req, timeout=30) as resp:
                body, tag = resp.read(LIMIT + 1), resp.headers.get("ETag")
        except urllib.error.HTTPError as error:
            if error.code == 304 and etag:
                return None, etag
            raise Refused(f"GitHub answered {error.code} for {path.split('?')[0]} (rate limit?)") from None
        except (OSError, ValueError) as error:  # URLError, timeouts, TLS
            raise Refused(f"GitHub unreachable ({type(error).__name__})") from None
        if len(body) > LIMIT:
            raise Refused("GitHub response too large")
        if raw:
            return body, None
        try:
            return json.loads(body), tag
        except ValueError:
            raise Refused(f"GitHub answered invalid JSON for {path.split('?')[0]}") from None


def git(repo: str, *args: str) -> bool:
    try:
        return subprocess.run(["git", "-C", repo, *args], check=False, capture_output=True, timeout=300, env=ENV).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def one_per_name(body, run_id: int, names: tuple[str, ...], attempt: int | None = None) -> dict:
    """Exactly one job per name (release_record keeps the last one of a repeated name), all successful."""
    jobs = body.get("jobs")
    if not isinstance(jobs, list) or body.get("total_count") != len(jobs):
        raise Refused(f"jobs of run {run_id}: incomplete list")
    found: dict = {}
    for job in jobs:
        if job.get("run_id") != run_id or (attempt is not None and job.get("run_attempt") != attempt):
            raise Refused(f"jobs of run {run_id}: a job of another run or attempt")
        if job.get("name") in found:
            raise Refused(f"jobs of run {run_id}: two jobs named {str(job.get('name'))[:40]!r}")
        found[job.get("name")] = job
    for name in names:
        if (found.get(name) or {}).get("conclusion") != "success":
            raise Refused(f"jobs of run {run_id}: {name!r} missing or not successful")
    return found


def release_env_of(blob: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            if archive.namelist() != ["release.env"] or archive.getinfo("release.env").file_size > 1 << 16:
                raise Refused("release-env artifact: not exactly one small release.env")
            return archive.read("release.env").decode()
    except (zipfile.BadZipFile, UnicodeDecodeError, OSError):
        raise Refused("release-env artifact: not a readable zip") from None


class Agent:
    def __init__(self, conf: dict, gh: GitHub, now: dt.datetime, log=print) -> None:
        self.conf, self.gh, self.now, self.log = conf, gh, now, log
        self.state = pathlib.Path(conf.get("STATE_DIR", "/var/lib/crm-cd"))
        self.git_dir = conf.get("GIT_DIR", str(ROOT))

    def api(self, path: str):
        body = self.gh.get(f"/repos/{REPO}/{path}")[0]
        if not isinstance(body, (dict, list)):
            raise Refused(f"GitHub answered no object for {path.split('?')[0]}")
        return body

    def in_main(self, sha: str) -> bool:
        return git(self.git_dir, "merge-base", "--is-ancestor", sha, MAIN)

    def revision(self) -> str:
        """REVISION of the release in production (the base of §5)."""
        values = parse(rr.read(str(pathlib.Path(self.conf.get("ENV_DIR", "/etc/crm"), "release.env"))))
        return rr.sha_arg(values.get("REVISION", ""), "REVISION of the release in production")

    def deployments(self) -> list:
        """The latest records, with ETag. On a 304 the cached body is verified again (the 24 h window moves)."""
        cache, tag_file = self.state / "deployments.json", self.state / "etag"
        try:
            cached, etag = json.loads(cache.read_text()), tag_file.read_text().strip()
        except (OSError, ValueError):
            cached, etag = None, None
        body, tag = self.gh.get(f"/repos/{REPO}/deployments?task=crm-release&environment=production&per_page=10",
                                etag if cached is not None and ETAG.fullmatch(etag or "") else None)
        if body is None:
            body = cached
        elif tag and ETAG.fullmatch(tag):
            write(cache, json.dumps(body))
            write(tag_file, tag)
        if not isinstance(body, list):
            raise Refused("deployments: not a list")
        return body

    def cycle(self) -> dict | None:
        records = self.deployments()
        remote = self.conf.get("GIT_REMOTE", REMOTE)
        if not REMOTE_RE.fullmatch(remote):
            raise Refused("GIT_REMOTE must be an https URL without credentials")
        if records and not git(self.git_dir, "fetch", "--quiet", "--no-tags", remote, f"+refs/heads/main:{MAIN}"):
            raise Refused("cannot fetch main: ancestry unknown")
        for record in records:
            rid = str(record.get("id"))[:20] if isinstance(record, dict) else "?"
            try:
                if self.now - when(record.get("created_at"), "created_at") > rr.VALID_FOR:
                    continue  # created after the approval: older than 24 h cannot be valid
                result = self.verify(record)
            except (Refused, KeyError, TypeError, AttributeError, ValueError) as error:
                self.log(f"record {rid}: not deployable: {error if isinstance(error, Refused) else type(error).__name__}")
                continue
            write(self.state / "deployable.json", json.dumps(result, indent=2))
            self.log(f"DEPLOYABLE record {rid}: {result['sha']} class {result['class']} until {result['expires_at']}"
                     " (approve mode: nothing is deployed)")
            return result
        self.log("nothing deployable")
        return None

    def verify(self, record: dict) -> dict:
        if tuple((record.get("creator") or {}).get(k) for k in ("login", "type", "id")) != BOT:
            raise Refused("record not created by github-actions[bot]")
        if record.get("task") != "crm-release" or record.get("environment") != "production":
            raise Refused("not a crm-release record of production")
        p = record["payload"]
        if not isinstance(p, dict) or p.get("schema") != 1 or p.get("repository") != REPO:
            raise Refused("payload: unknown schema or another repository")
        sha, base = rr.sha_arg(p.get("sha"), "sha"), rr.sha_arg(p.get("base"), "base")
        if record.get("sha") != sha or record.get("ref") != sha:
            raise Refused("record sha/ref differ from the payload")
        if not DIGEST.fullmatch(str(p.get("fingerprint"))) or p.get("class") not in rr.APPROVABLE:
            raise Refused("payload: invalid fingerprint or class")
        run_id, attempt, images_id = (num(p.get(k), k) for k in ("run_id", "run_attempt", "images_run_id"))
        # payload approved_at / expires_at are ignored on purpose: whoever forges the record controls them.

        run = self.api(f"actions/runs/{run_id}")
        expected = {"id": int(run_id), "path": WORKFLOW, "event": "workflow_dispatch", "head_branch": "main",
                    "status": "completed", "conclusion": "success", "run_attempt": int(attempt),
                    "display_title": f"release {sha} base {base}"}
        for key, value in expected.items():
            if run.get(key) != value:
                raise Refused(f"release run: {key} is {str(run.get(key))[:60]!r}, not {value!r}")
        if any((run.get(k) or {}).get("full_name") != REPO for k in ("repository", "head_repository")):
            raise Refused("release run: not from this repository")
        if not self.in_main(rr.sha_arg(run.get("head_sha"), "release run head_sha")):
            raise Refused("release run: head_sha not in main (a tag or branch named main?)")
        if not self.in_main(sha):
            raise Refused("the SHA is not an ancestor of main")
        actors = {(run.get(k) or {}).get("login") for k in ("actor", "triggering_actor")}
        if not all(isinstance(a, str) and a for a in actors):
            raise Refused("release run: unknown actor")

        jobs = one_per_name(self.api(f"actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100"),
                            int(run_id), JOBS, int(attempt))
        approved_at = when(jobs["release approve"].get("started_at"), "approve job started_at")
        if not dt.timedelta(0) <= self.now - approved_at <= rr.VALID_FOR:
            raise Refused("approval expired: the approve job started more than 24 h ago (§5)")
        by = rr.approved_by(self.api(f"actions/runs/{run_id}/approvals"),
                            rr.approvers(self.conf.get("CD_APPROVERS", "")), {a.lower() for a in actors})

        if base != self.revision():
            raise Refused("base is not the REVISION in production (deployed since the preparation, §5)")
        runs = self.api(f"actions/workflows/images.yml/runs?head_sha={sha}&per_page=100")
        if str(rr.images_run(runs, sha, REPO)) != images_id:
            raise Refused("images_run_id is not THE images run of the SHA")
        one_per_name(self.api(f"actions/runs/{images_id}/jobs?filter=latest&per_page=100"), int(images_id),
                     rr.IMAGES_JOBS)
        artifacts = self.api(f"actions/runs/{images_id}/artifacts?name=release-env").get("artifacts")
        if not isinstance(artifacts, list) or len(artifacts) != 1 or artifacts[0].get("name") != "release-env" \
                or artifacts[0].get("expired") is not False:
            raise Refused("images run: not exactly one live release-env artifact")
        artifact = num(artifacts[0].get("id"), "artifact id")
        lines = rr.release_env(release_env_of(self.gh.get(f"/repos/{REPO}/actions/artifacts/{artifact}/zip",
                                                          raw=True)[0]), sha)
        if rr.fingerprint(lines) != p["fingerprint"]:
            raise Refused("fingerprint differs from the recomputed release.env (§5)")
        report = classify(self.git_dir, base, sha)
        if report["errors"] or report["class"] != p["class"]:
            raise Refused(f"class recomputed as {report['class']}, not {p['class']} (§5)")
        self.attest(sha, p["fingerprint"], lines)
        self.preflight()
        return {"deployment_id": num(record.get("id"), "record id"), "sha": sha, "base": base,
                "class": p["class"], "fingerprint": p["fingerprint"], "images_run_id": images_id,
                "run_id": run_id, "run_attempt": attempt, "approved_by": by,
                "approved_at": approved_at.strftime(rr.TIME),
                "expires_at": (approved_at + rr.VALID_FOR).strftime(rr.TIME),
                "verified_at": self.now.strftime(rr.TIME)}

    def attest(self, sha: str, fingerprint: str, lines: list[str]) -> None:
        """verify_attestations.py once per release (anonymous quota): its result is cached by fingerprint."""
        done, key = self.state / "attested", f"{sha} {fingerprint}"
        if done.exists() and done.read_text() == key:
            return
        env_file = self.state / "release.env.verify"
        write(env_file, "".join(f"{line}\n" for line in lines))
        if not run([*ATTEST, "--sha", sha, str(env_file)], 600):
            raise Refused("verify_attestations.py failed (provenance, §7)")
        write(done, key)

    def preflight(self) -> None:
        if not run(PREFLIGHT, 300, self.log):
            raise Refused("cd-preflight.sh is not green (§7)")


def run(argv: list[str], timeout: int, log=None) -> bool:
    try:
        done = subprocess.run(argv, check=False, capture_output=True, text=True, timeout=timeout, env=ENV)
    except (OSError, subprocess.SubprocessError):
        return False
    for line in done.stdout.splitlines():
        if log and line.startswith("BLOQUEA"):
            log(f"preflight: {line[:200]}")
    return done.returncode == 0


def write(path: pathlib.Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(text)
    os.replace(tmp, path)


def once(config: str, opener=None, now: dt.datetime | None = None) -> int:
    try:
        conf = parse(private_file(config, secret=False))
        pathlib.Path(conf.get("STATE_DIR", "/var/lib/crm-cd"), "deployable.json").unlink(missing_ok=True)
        if conf.get("MODE") != "approve":
            print("mode off: no polling and nothing deployable (only MODE=approve polls; auto is phase 2)")
            return 0
        token = parse(private_file(conf.get("TOKEN_FILE", "/etc/crm/cd.env"), secret=True)).get("CD_GITHUB_TOKEN")
        if not TOKEN.fullmatch(token or ""):
            raise Refused("TOKEN_FILE has no valid CD_GITHUB_TOKEN")
        Agent(conf, GitHub(token, opener), now or dt.datetime.now(dt.timezone.utc)).cycle()
        return 0
    except Exception as error:  # noqa: BLE001 — fail closed; the type only, never a value
        print(f"not deployable: {error if isinstance(error, Refused) else type(error).__name__}", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", default="/etc/crm/cd.conf")
    parser.add_argument("--loop", action="store_true", help="poll every 2 min instead of once")
    args = parser.parse_args(argv)
    while True:
        code = once(args.config)
        if not args.loop:
            return code
        time.sleep(EVERY)


if __name__ == "__main__":
    sys.exit(main())
