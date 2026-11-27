#!/usr/bin/env python3
"""Verify the signed provenance of the three own images of a release (ADR-048 §7 and §11; F1-20b).

    python3 infra/prod/verify_attestations.py --sha <40-hex SHA> <release.env>

Per own image of the release.env (REVISION must be the SHA): the provenance attestations of its digest from
the GitHub API without a token; `gh attestation verify --bundle` (pinned GH_VERSION, never authenticated)
for the Sigstore signature and the signer images.yml on refs/heads/main at that SHA; the same fields checked
again here (AT LEAST ONE attestation must match all of them, in any order); and the image config read from
GHCR anonymously, every hash checked, with org.opencontainers.image.revision equal to the SHA.
Fails closed: exit 0 only if all three pass; any mismatch, missing attestation, network error or 403/429
is exit 1. Read-only, stdlib only. Anonymous api.github.com allows 60 requests/hour per IP (shared with the
other application of the server); this makes 3 to 9 (up to 3 pages per image), so the agent (F1-20e1)
runs it once per release.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

REPO = "PapiCuche/crm-gooddoggy"
REPO_ID, OWNER_ID = "1393140901", "116849344"  # GitHub ids (strings in the certificate)
REGISTRY_REPO = "ghcr.io/papicuche/crm-gooddoggy"
WORKFLOW = f"https://github.com/{REPO}/.github/workflows/images.yml@refs/heads/main"
ISSUER = "https://token.actions.githubusercontent.com"
PREDICATE = "https://slsa.dev/provenance/v1"
GH_VERSION = "2.103.3"
NAMES = ("backend", "frontend", "proxy")
MAX_BYTES = 8 << 20  # cap of every download and of a decompressed bundle
PAGES = 3  # 100 attestations per page; more than 300 for one digest is not ours
TIMEOUT = 60
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
MEDIA = "application/vnd.oci.image.index.v1+json, application/vnd.oci.image.manifest.v1+json"  # buildx: OCI


class Fail(Exception):
    """Any reason to block. The message never carries a credential (there is none)."""


Get = Callable[[str, dict[str, str]], bytes]
Run = Callable[[list[str], dict[str, str]], str]


def fetch(url: str, headers: dict[str, str]) -> bytes:
    if not url.startswith("https://"):
        raise Fail("refusing a URL that is not https")
    request = urllib.request.Request(url, headers={"User-Agent": "crm-verify-attestations", **headers})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:  # noqa: S310 (https only, above)
            body = response.read(MAX_BYTES + 1)
    except (OSError, ValueError, http.client.HTTPException) as error:  # HTTPError (403/429 too), timeouts
        raise Fail(f"{url.split('?')[0]}: {error}") from None
    if len(body) > MAX_BYTES:
        raise Fail(f"{url.split('?')[0]}: response larger than {MAX_BYTES} bytes")
    return body


def run_command(cmd: list[str], env: dict[str, str]) -> str:
    try:
        done = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=4 * TIMEOUT, check=False)
    except (OSError, subprocess.SubprocessError) as error:
        raise Fail(f"{cmd[0]} {cmd[1]}: {error}") from None
    if done.returncode != 0:
        raise Fail(f"{cmd[0]} {cmd[1]} exited {done.returncode}: {done.stderr.strip()[-300:]}")
    return done.stdout


def load(body: bytes | str) -> Any:
    try:
        return json.loads(body)
    except (ValueError, RecursionError):
        raise Fail("invalid JSON") from None


def unsnappy(data: bytes) -> bytes:
    """Raw snappy block (the API's `bundle_url`). Untrusted input: bounded, anything malformed fails."""
    pos = 0

    def take(n: int) -> bytes:
        nonlocal pos
        if n < 0 or pos + n > len(data):
            raise Fail("truncated snappy data")
        pos += n
        return data[pos - n : pos]

    size = shift = 0
    while True:
        byte = take(1)[0]
        size |= (byte & 0x7F) << shift
        shift += 7
        if byte < 0x80:
            break
        if shift > 28:
            raise Fail("snappy length too long")
    if size > MAX_BYTES:
        raise Fail("snappy length over the cap")
    out = bytearray()
    while pos < len(data):
        tag = take(1)[0]
        kind = tag & 3
        if kind == 0:
            length = tag >> 2
            if length >= 60:
                length = int.from_bytes(take(length - 59), "little")
            out += take(length + 1)
        else:
            if kind == 1:
                length, offset = 4 + ((tag >> 2) & 7), (tag >> 5) << 8 | take(1)[0]
            else:
                length, offset = 1 + (tag >> 2), int.from_bytes(take(2 if kind == 2 else 4), "little")
            if not 0 < offset <= len(out):
                raise Fail("snappy copy out of range")
            start = len(out) - offset
            for i in range(length):
                out.append(out[start + i])
        if len(out) > size:
            raise Fail("snappy data longer than declared")
    if len(out) != size:
        raise Fail("snappy data shorter than declared")
    return bytes(out)


def parse_release(path: str, sha: str) -> dict[str, str]:
    """{name: digest} of the three own images. The file comes from images.yml: strict format."""
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise Fail("--sha must be 40 lowercase hex characters")
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read(65536)
    except (OSError, ValueError) as error:
        raise Fail(f"release.env: {error}") from None
    values: dict[str, str] = {}
    for line in text.splitlines():
        match = re.fullmatch(r"([A-Z][A-Z_]*)=(\S+)", line)
        if not match or match[1] in values:
            raise Fail(f"release.env: invalid or repeated line {line[:80]!r}")
        values[match[1]] = match[2]
    if values.get("REVISION") != sha:
        raise Fail("release.env: REVISION is not --sha")
    images = {}
    for name in NAMES:
        ref = values.get(f"{name.upper()}_IMAGE", "")
        match = re.fullmatch(rf"{re.escape(REGISTRY_REPO)}/{name}@(sha256:[0-9a-f]{{64}})", ref)
        if not match:
            raise Fail(f"release.env: {name.upper()}_IMAGE is not {REGISTRY_REPO}/{name}@sha256:<digest>")
        images[name] = match[1]
    return images


def bundles(digest: str, get: Get) -> list[Any]:
    found = []
    for page in range(1, PAGES + 1):
        url = f"https://api.github.com/repos/{REPO}/attestations/{digest}?predicate_type=provenance&per_page=100&page={page}"
        items = load(get(url, {"Accept": "application/vnd.github+json"}))["attestations"]
        for item in items:
            bundle = item.get("bundle")
            if not bundle:
                link = item.get("bundle_url")
                if not isinstance(link, str) or not link.startswith("https://"):
                    raise Fail("bundle_url is not https")
                bundle = load(unsnappy(get(link, {})))
            if not isinstance(bundle, dict):
                raise Fail("an attestation bundle is not a JSON object")
            found.append(bundle)
        if len(items) < 100:
            break
    if not found:
        raise Fail("no provenance attestation for this digest")
    return found


def gh_verify(name: str, digest: str, sha: str, found: list[Any], run: Run) -> list[Any]:
    with tempfile.TemporaryDirectory() as home:
        path = os.path.join(home, "bundles.jsonl")
        with open(path, "w", encoding="utf-8") as handle:
            handle.writelines(json.dumps(b) + "\n" for b in found)
        # Sin credenciales: ni token de GitHub ni sesión de gh ni de Docker (registro anónimo).
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": home, "GH_CONFIG_DIR": home,
               "DOCKER_CONFIG": home, "GH_NO_UPDATE_NOTIFIER": "1", "GH_PROMPT_DISABLED": "1"}
        version = run(["gh", "--version"], env)
        if not version.startswith(f"gh version {GH_VERSION} "):
            raise Fail(f"gh must be version {GH_VERSION}: {version.splitlines()[:1]}")
        out = run(["gh", "attestation", "verify", f"oci://{REGISTRY_REPO}/{name}@{digest}", "--bundle", path,
                   "--repo", REPO, "--cert-identity", WORKFLOW, "--cert-oidc-issuer", ISSUER,
                   "--source-ref", "refs/heads/main", "--source-digest", sha, "--signer-digest", sha,
                   "--predicate-type", PREDICATE, "--deny-self-hosted-runners", "--format", "json"], env)
    results = load(out)
    if not isinstance(results, list):
        raise Fail("gh returned no list of results")
    return results


def matches(result: Any, digest: str, sha: str) -> bool:
    """One verified attestation, checked field by field here as well (in case a gh flag regresses)."""
    expected = {
        "issuer": ISSUER, "subjectAlternativeName": WORKFLOW, "buildSignerURI": WORKFLOW,
        "sourceRepositoryURI": f"https://github.com/{REPO}", "sourceRepositoryRef": "refs/heads/main",
        "sourceRepositoryDigest": sha, "buildSignerDigest": sha, "githubWorkflowTrigger": "push",
        "runnerEnvironment": "github-hosted",
        # Por id, no solo por nombre: un repositorio borrado y recreado con el mismo nombre no pasa.
        "sourceRepositoryIdentifier": REPO_ID, "sourceRepositoryOwnerIdentifier": OWNER_ID,
    }
    try:
        verified = result["verificationResult"]
        cert, statement = verified["signature"]["certificate"], verified["statement"]
        subjects = [s["digest"]["sha256"] for s in statement["subject"]]
        return (
            all(cert[key] == value for key, value in expected.items())
            and statement["predicateType"] == PREDICATE
            and digest.removeprefix("sha256:") in subjects
        )
    except (KeyError, TypeError):
        return False


def revision(name: str, digest: str, get: Get) -> Any:
    """org.opencontainers.image.revision of the linux/amd64 config, every hash checked."""
    repo = f"{REGISTRY_REPO.removeprefix('ghcr.io/')}/{name}"
    token = load(get(f"https://ghcr.io/token?scope=repository:{repo}:pull&service=ghcr.io", {}))["token"]
    headers = {"Authorization": f"Bearer {token}", "Accept": MEDIA}  # token anónimo de solo lectura

    def blob(kind: str, wanted: Any) -> Any:
        if not isinstance(wanted, str) or not DIGEST.fullmatch(wanted):
            raise Fail(f"invalid digest in the registry: {str(wanted)[:80]!r}")
        body = get(f"https://ghcr.io/v2/{repo}/{kind}/{wanted}", headers)
        if f"sha256:{hashlib.sha256(body).hexdigest()}" != wanted:
            raise Fail(f"{kind}/{wanted} does not match its content")
        return load(body)

    manifest = blob("manifests", digest)
    if "manifests" in manifest:
        amd64 = [m["digest"] for m in manifest["manifests"]
                 if m.get("platform", {}).get("os") == "linux" and m["platform"].get("architecture") == "amd64"]
        if len(amd64) != 1:
            raise Fail("the index has no single linux/amd64 manifest")
        manifest = blob("manifests", amd64[0])
    return blob("blobs", manifest["config"]["digest"])["config"]["Labels"]["org.opencontainers.image.revision"]


def verify_image(name: str, digest: str, sha: str, get: Get, run: Run) -> None:
    try:
        results = gh_verify(name, digest, sha, bundles(digest, get), run)
        if not any(matches(r, digest, sha) for r in results):
            raise Fail("no attestation of images.yml on refs/heads/main for this SHA and digest")
        if revision(name, digest, get) != sha:
            raise Fail("the image label org.opencontainers.image.revision is not the SHA")
    except (KeyError, TypeError, AttributeError, IndexError) as error:
        raise Fail(f"unexpected response format ({type(error).__name__})") from None


def verify(path: str, sha: str, get: Get = fetch, run: Run = run_command) -> int:
    try:
        images = parse_release(path, sha)
    except Fail as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    status = 0
    for name, digest in images.items():
        try:
            verify_image(name, digest, sha, get, run)
        except Fail as error:
            print(f"error: {name}: {error}", file=sys.stderr)
            status = 1
        else:
            print(f"verified {name} {digest}")
    return status


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sha", required=True, help="the 40-hex main commit of the release")
    parser.add_argument("release_env", help="release.env written by images.yml")
    args = parser.parse_args(argv)
    return verify(args.release_env, args.sha)


if __name__ == "__main__":
    sys.exit(main())
