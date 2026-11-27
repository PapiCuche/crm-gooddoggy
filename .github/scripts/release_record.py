"""Prepare a release and check its approval before writing the deployment record (ADR-048 §4, §5, §11; F1-20c).

Used only by `.github/workflows/release.yml`, from the checkout of `main`. Three steps, each fails closed:

- `images-run`: picks THE images.yml run of the SHA (push to main in this repository, successful, publish and
  attest jobs successful) from the GitHub API JSON; the workflow downloads `release-env` only from that run id.
- `prepare`: inputs, ancestry `base → sha → main`, strict release.env, fingerprint, class over `(base, sha]`
  (deploy_class.py from main). Class C, `none` or any error: no approval is requested (C stays in phase 0).
- `approve`: after the `production` environment approval, recomputes the fingerprint and the class, checks
  the approvals (only logins of CD_APPROVERS, never who triggered the run, no rejection) and the 24 h window,
  and prints the deployment request. The record is data: it deploys nothing (the GO by SHA still rules).

Standard library only; no network (the workflow calls the API with `gh`); no secret ever reaches this code.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import subprocess
import sys

from deploy_class import MIGRATIONS, classify, markdown

SHA = re.compile(r"[0-9a-f]{40}")
LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")
REGISTRY = "ghcr.io/papicuche/crm-gooddoggy"
IMAGES = {
    "BACKEND_IMAGE": rf"{REGISTRY}/backend@sha256:[0-9a-f]{{64}}",
    "FRONTEND_IMAGE": rf"{REGISTRY}/frontend@sha256:[0-9a-f]{{64}}",
    "PROXY_IMAGE": rf"{REGISTRY}/proxy@sha256:[0-9a-f]{{64}}",
    "POSTGRES_IMAGE": r"docker\.io/library/postgres:[\w.-]+@sha256:[0-9a-f]{64}",
    "REDIS_IMAGE": r"docker\.io/library/redis:[\w.-]+@sha256:[0-9a-f]{64}",
}
APPROVABLE = ("A1", "A2", "A3", "B")  # phase 1 (ADR-048 §3); C stays in phase 0, `none` deploys nothing
VALID_FOR = dt.timedelta(hours=24)  # §5 and decision 10
ENVIRONMENT = "production"
IMAGES_WORKFLOW = ".github/workflows/images.yml"
IMAGES_JOBS = ("images publish", "images attest")
TIME = "%Y-%m-%dT%H:%M:%SZ"
COMMON = ("sha", "base", "images-run-id", "release-env", "repo", "out")
COMMANDS = {"images-run": ("sha", "repository", "runs"), "images-jobs": ("run-id", "jobs"), "prepare": COMMON,
            "approve": (*COMMON, "fingerprint", "class", "prepared-at", "run-id", "run-attempt", "repository",
                        "approvals", "approvers", "actor", "triggering-actor")}
OPTIONAL = {"repo", "approvers", "actor", "triggering-actor"}


class Refused(Exception):
    """Any reason not to prepare or not to record. The message never carries a token (there is none here)."""


def read(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.read(1 << 20)


def load(path: str):
    return json.loads(read(path))


def sha_arg(value: str, what: str) -> str:
    if not SHA.fullmatch(value or ""):
        raise Refused(f"{what} must be 40 lowercase hex characters")
    return value


def images_run(runs: dict, sha: str, repository: str) -> int:
    """The id of the single images.yml run of `sha`: push to main in this repository, completed, success."""
    found = runs.get("workflow_runs")
    if not isinstance(found, list) or len(found) != 1:
        raise Refused(f"expected exactly one images.yml run for {sha}, found {len(found or [])}")
    run = found[0]
    expected = {"head_sha": sha, "event": "push", "head_branch": "main", "path": IMAGES_WORKFLOW,
                "status": "completed", "conclusion": "success"}
    for key, value in expected.items():
        if run.get(key) != value:
            raise Refused(f"images run: {key} is {str(run.get(key))[:60]!r}, not {value!r}")
    if (run.get("repository") or {}).get("full_name") != repository or \
            (run.get("head_repository") or {}).get("full_name") != repository:
        raise Refused("images run: not from this repository")
    if not isinstance(run.get("id"), int) or run["id"] <= 0:
        raise Refused("images run: invalid id")
    return run["id"]


def jobs_ok(jobs: dict, run_id: int) -> None:
    """publish and attest of the latest attempt of that run, both successful (no attestation: no release)."""
    found = jobs.get("jobs") or []
    if any(j.get("run_id") != run_id for j in found):
        raise Refused("images run: a job of another run")
    seen = {j.get("name"): j.get("conclusion") for j in found}
    for name in IMAGES_JOBS:
        if seen.get(name) != "success":
            raise Refused(f"images run: job {name!r} is {seen.get(name)!r}, not 'success'")


def release_env(text: str, sha: str) -> list[str]:
    """Strict release.env of images.yml: five images by digest and REVISION, nothing else, each once."""
    lines = text.splitlines()
    values = dict(line.split("=", 1) for line in lines if "=" in line)
    if len(lines) != len(IMAGES) + 1:
        raise Refused("release.env: unexpected, repeated or missing lines")
    for key, pattern in IMAGES.items():
        if not re.fullmatch(pattern, values.get(key, "")):
            raise Refused(f"release.env: {key} is not an image by digest")
    if values.get("REVISION") != sha:
        raise Refused("release.env: REVISION is not the SHA")
    return sorted(lines)


def fingerprint(lines: list[str]) -> str:
    """sha256 of the canonical release.env (sorted lines, one LF after each): the §5 fingerprint."""
    return "sha256:" + hashlib.sha256("".join(f"{line}\n" for line in sorted(lines)).encode()).hexdigest()


def git_ok(repo: str, *args: str) -> bool:
    return subprocess.run(["git", "-C", repo, *args], capture_output=True).returncode == 0


def release_class(repo: str, base: str, sha: str) -> dict:
    """Class over (base, sha] with the classifier of this checkout (main), fail closed."""
    if not git_ok(repo, "merge-base", "--is-ancestor", base, sha):
        raise Refused("base is not an ancestor of the SHA (never backwards, ADR-048 §5)")
    if not git_ok(repo, "merge-base", "--is-ancestor", sha, "HEAD"):
        raise Refused("the SHA is not in main")
    report = classify(repo, base, sha)
    if report["errors"] or report["class"] not in APPROVABLE:
        raise Refused(f"class {report['class']}: no approval in phase 1 (C stays in phase 0, none deploys nothing)")
    return report


def migrations(report: dict) -> list[str]:
    return sorted({f"{f['path']} ({f['status']}, {f['class']})" for c in report["commits"] for f in c["files"]
                   if MIGRATIONS.match(f["path"])})


def prepare(args: argparse.Namespace) -> tuple[dict, str]:
    sha, base = sha_arg(args.sha, "sha"), sha_arg(args.base, "base")
    lines = release_env(read(args.release_env), sha)
    report = release_class(args.repo, base, sha)
    record = {"sha": sha, "base": base, "class": report["class"], "fingerprint": fingerprint(lines),
              "images_run_id": str(args.images_run_id), "prepared_at": args.now.strftime(TIME)}
    summary = [
        f"# Release `{sha[:12]}` preparado: clase **{report['class']}**", "",
        "> Rige la fase 0: el GO exacto por SHA. Este registro no despliega nada (ADR-048, enmienda E1).", "",
        f"- SHA `{sha}`, base `{base}`, {len(report['commits'])} commits",
        f"- Huella del release.env: `{record['fingerprint']}` (run de images {record['images_run_id']})",
        f"- Preparado {record['prepared_at']}; la aprobación vale para este SHA y caduca a las 24 h (§5)", "",
        "## Migraciones del rango", "", *([f"- `{m}`" for m in migrations(report)] or ["- ninguna"]), "",
        "## release.env", "", "```", *lines, "```", "",
        markdown(report).replace("Solo informativa en la fase 1 (ADR-048): no despliega, no aprueba y no bloquea nada.",
                                 "Aquí la clase sí cuenta: con C o `none` no se pide aprobación (C sigue en fase 0)."),
    ]
    return record, "\n".join(summary)


def approvers(raw: str) -> set[str]:
    names = {n.lower() for n in re.split(r"[\s,]+", raw or "") if n}
    if not names or not all(LOGIN.fullmatch(n) for n in names):
        raise Refused("CD_APPROVERS is empty or invalid: nobody can approve")
    return names


def approved_by(entries: list, allowed: set[str], actors: set[str]) -> list[str]:
    """Logins that approved. Every entry: production only, approved, a User of CD_APPROVERS, not a run actor."""
    if not isinstance(entries, list) or not entries:
        raise Refused("no approval in the production environment (is it protected with reviewers?)")
    logins = []
    for entry in entries:
        user = entry.get("user") or {}
        login = str(user.get("login", "")).lower()
        if [e.get("name") for e in entry.get("environments") or []] != [ENVIRONMENT]:
            raise Refused("an approval entry is not for the production environment only")
        if entry.get("state") != "approved":
            raise Refused(f"approval state {str(entry.get('state'))[:20]!r}")
        if user.get("type") != "User" or login not in allowed:
            raise Refused(f"{login[:40]!r} is not in CD_APPROVERS")
        if login in actors:
            raise Refused(f"{login[:40]!r} triggered this run: self-approval is not valid (ADR-048 §4)")
        logins.append(login)
    return sorted(set(logins))


def approve(args: argparse.Namespace) -> dict:
    sha, base = sha_arg(args.sha, "sha"), sha_arg(args.base, "base")
    if not all(re.fullmatch(r"[1-9][0-9]*", n) for n in (args.images_run_id, args.run_id, args.run_attempt)) or \
            not re.fullmatch(r"[\w.-]+/[\w.-]+", args.repository):
        raise Refused("invalid run id, run attempt or repository")
    try:
        prepared = dt.datetime.strptime(args.prepared_at, TIME).replace(tzinfo=dt.timezone.utc)
    except ValueError:
        raise Refused("invalid prepared_at") from None
    if not dt.timedelta(0) <= args.now - prepared <= VALID_FOR:
        raise Refused("approved more than 24 h after the preparation: prepare the release again (§5)")
    lines = release_env(read(args.release_env), sha)
    if fingerprint(lines) != args.fingerprint:
        raise Refused("the release.env of the images run changed since the preparation (§5)")
    cls = getattr(args, "class")
    if release_class(args.repo, base, sha)["class"] != cls:
        raise Refused("the class changed since the preparation (§5)")
    actors = {a.lower() for a in (args.actor, args.triggering_actor) if a}
    by = approved_by(load(args.approvals), approvers(args.approvers), actors)
    payload = {"schema": 1, "sha": sha, "fingerprint": args.fingerprint, "base": base, "class": cls,
               "repository": args.repository, "run_id": args.run_id, "run_attempt": args.run_attempt,
               "images_run_id": args.images_run_id, "approved_by": by,
               "prepared_at": args.prepared_at, "approved_at": args.now.strftime(TIME),
               "expires_at": (args.now + VALID_FOR).strftime(TIME)}
    return {"ref": sha, "task": "crm-release", "environment": ENVIRONMENT, "auto_merge": False,
            "required_contexts": [], "production_environment": True, "transient_environment": False,
            "description": f"Release {sha[:12]} class {cls}, approved (ADR-048 §5)",
            "payload": payload}


def main(argv: list[str] | None = None, now: dt.datetime | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    for name, flags in COMMANDS.items():
        s = sub.add_parser(name)
        for flag in flags:
            s.add_argument(f"--{flag}", required=flag not in OPTIONAL, default="." if flag == "repo" else "")
    args = p.parse_args(argv)
    args.now = now or dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    try:
        if args.cmd == "images-run":
            print(images_run(load(args.runs), sha_arg(args.sha, "sha"), args.repository))
        elif args.cmd == "images-jobs":
            jobs_ok(load(args.jobs), int(args.run_id))
        elif args.cmd == "prepare":
            record, summary = prepare(args)
            print(summary)
            with open(args.out, "w", encoding="utf-8") as out:
                out.writelines(f"{key}={value}\n" for key, value in record.items())
        else:
            request = approve(args)
            with open(args.out, "w", encoding="utf-8") as out:
                json.dump(request, out, indent=2)
            print(f"Aprobado por {', '.join(request['payload']['approved_by'])}: registro listo")
    except (Refused, OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
