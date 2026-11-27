"""infra/prod/verify_attestations.py (F1-20b, ADR-048 §7) fails closed. No network: a fake web (GitHub API,
bundle storage, GHCR) and a fake `gh` on PATH printing results shaped like a real `gh attestation verify`.
"""

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import pathlib
import random
import re
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
from unittest import mock

PATH = pathlib.Path(__file__).resolve().parents[2] / "infra/prod/verify_attestations.py"
spec = importlib.util.spec_from_file_location("verify_attestations", PATH)
va = importlib.util.module_from_spec(spec)
spec.loader.exec_module(va)

SHA = "1234567890abcdef1234567890abcdef12345678"
OTHER_SHA = "fedcba0987654321fedcba0987654321fedcba09"
WORKFLOW = "https://github.com/PapiCuche/crm-gooddoggy/.github/workflows/images.yml@refs/heads/main"
API = "https://api.github.com/repos/PapiCuche/crm-gooddoggy/attestations/"
FAKE_GH = """#!{python}
import json, os, re, sys
here = os.path.dirname(os.path.abspath(__file__))
if sys.argv[1:] == ["--version"]:
    print(open(os.path.join(here, "version")).read())
    sys.exit(0)
name = re.search(r"/(\\w+)@", sys.argv[3]).group(1)
log = {{"args": sys.argv[1:], "env": dict(os.environ), "bundles": open(sys.argv[5]).read()}}
open(os.path.join(here, "log-" + name), "w").write(json.dumps(log))
print(open(os.path.join(here, "out-" + name)).read())
sys.exit(int(open(os.path.join(here, "rc")).read()))
"""


def digest(body: bytes) -> str:
    return f"sha256:{hashlib.sha256(body).hexdigest()}"


def snappy_literals(data: bytes) -> bytes:
    """Minimal encoder for the fixtures: the length varint, then literals of at most 60 bytes."""
    out, n = bytearray(), len(data)
    while n >= 0x80:
        out.append(n & 0x7F | 0x80)
        n >>= 7
    out.append(n)
    for i in range(0, len(data), 60):
        out += bytes([(len(data[i : i + 60]) - 1) << 2]) + data[i : i + 60]
    return bytes(out)


def certificate(sha: str = SHA) -> dict:
    # Campos de un `gh attestation verify --format json` real (gh 2.98, 2026-11-27), con nuestros valores.
    return {"subjectAlternativeName": WORKFLOW, "buildSignerURI": WORKFLOW, "buildSignerDigest": sha,
            "issuer": "https://token.actions.githubusercontent.com", "githubWorkflowTrigger": "push",
            "runnerEnvironment": "github-hosted", "sourceRepositoryURI": "https://github.com/PapiCuche/crm-gooddoggy",
            "sourceRepositoryDigest": sha, "sourceRepositoryRef": "refs/heads/main", "githubWorkflowSHA": sha,
            "sourceRepositoryIdentifier": "1393140901", "sourceRepositoryOwnerIdentifier": "116849344"}


def result(image_digest: str, cert: dict | None = None, predicate_type: str = va.PREDICATE) -> dict:
    subject = [{"name": "ghcr.io/papicuche/crm-gooddoggy/x", "digest": {"sha256": image_digest[7:]}}]
    statement = {"_type": "https://in-toto.io/Statement/v1", "subject": subject, "predicateType": predicate_type}
    return {"verificationResult": {"signature": {"certificate": cert or certificate()}, "statement": statement}}


class Web:
    """Fake HTTPS: exact URL, or the URL without its query. Records every request and its headers."""

    def __init__(self):
        self.routes: dict[str, object] = {}
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, url, headers):
        self.calls.append((url, dict(headers)))
        body = self.routes.get(url, self.routes.get(url.split("?")[0], va.Fail(f"404 {url}")))
        if isinstance(body, Exception):
            raise body
        return body


class VerifierCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = pathlib.Path(tmp.name)
        self.bin = self.dir / "bin"
        self.bin.mkdir()
        gh = self.bin / "gh"
        gh.write_text(FAKE_GH.format(python=sys.executable))
        gh.chmod(0o755)
        (self.bin / "version").write_text(f"gh version {va.GH_VERSION} (2026-10-26)\nhttps://github.com/cli/cli\n")
        (self.bin / "rc").write_text("0")
        tokens = {"GH_TOKEN": "gho_must_not_pass", "GITHUB_TOKEN": "ghs_must_not_pass"}
        self.enterContext(mock.patch.dict(os.environ, {"PATH": f"{self.bin}:{os.environ['PATH']}", **tokens}))
        self.web = Web()
        self.digests = {}
        lines = []
        for name in va.NAMES:
            self.digests[name] = self.registry(name)
            self.api(name, [{"bundle": {"mediaType": "application/vnd.dev.sigstore.bundle.v0.3+json", "n": name}}])
            self.gh_out(name, [result(self.digests[name])])
            lines.append(f"{name.upper()}_IMAGE=ghcr.io/papicuche/crm-gooddoggy/{name}@{self.digests[name]}")
        lines += [f"POSTGRES_IMAGE=docker.io/library/postgres:18.6@{digest(b'pg')}", f"REVISION={SHA}"]
        self.release = self.dir / "release.env"
        self.release.write_text("\n".join(lines) + "\n")

    def registry(self, name, label=SHA, config_body=None, platform=("linux", "amd64"), index=None):
        repo = f"https://ghcr.io/v2/papicuche/crm-gooddoggy/{name}"
        token = f"https://ghcr.io/token?scope=repository:papicuche/crm-gooddoggy/{name}:pull&service=ghcr.io"
        self.web.routes[token] = b'{"token":"anon"}'
        labels = {"org.opencontainers.image.revision": label} if label else {}
        config = json.dumps({"config": {"Labels": labels, "WorkingDir": f"/{name}"}}).encode()
        self.web.routes[f"{repo}/blobs/{digest(config)}"] = config_body or config
        manifest = json.dumps({"config": {"digest": digest(config)}}).encode()
        self.web.routes[f"{repo}/manifests/{digest(manifest)}"] = manifest
        index = json.dumps({"manifests": index or [
            {"digest": digest(manifest), "platform": {"os": platform[0], "architecture": platform[1]}},
            {"digest": digest(b"attestation manifest"), "platform": {"os": "unknown", "architecture": "unknown"}},
        ]}).encode()
        self.web.routes[f"{repo}/manifests/{digest(index)}"] = index
        return digest(index)

    def api(self, name, items, digest_=None):
        self.web.routes[API + (digest_ or self.digests[name])] = json.dumps({"attestations": items}).encode()

    def gh_out(self, name, results):
        (self.bin / f"out-{name}").write_text(json.dumps(results))

    def verify(self, sha=SHA):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = va.verify(str(self.release), sha, get=self.web)
        self.out, self.err = out.getvalue(), err.getvalue()
        return code

    def assertBlocks(self, fragment=""):
        self.assertEqual(self.verify(), 1, self.out)
        self.assertIn(fragment, self.err)


class GoodReleaseTest(VerifierCase):
    def test_all_three_images_verify(self):
        self.assertEqual(len(set(self.digests.values())), 3)
        self.assertEqual(self.verify(), 0, self.err)
        self.assertEqual(self.out.split(), [w for n in va.NAMES for w in ("verified", n, self.digests[n])])

    def test_no_credentials_anywhere(self):
        self.verify()
        for url, headers in self.web.calls:
            if not url.startswith("https://ghcr.io/v2/"):  # ghcr.io/v2 lleva solo el token anónimo
                self.assertNotIn("Authorization", headers, url)
        log = json.loads((self.bin / "log-backend").read_text())
        self.assertFalse({"GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN"} & set(log["env"]))
        self.assertTrue(log["env"]["GH_CONFIG_DIR"] == log["env"]["HOME"] == log["env"]["DOCKER_CONFIG"])
        self.assertFalse(os.path.exists(log["env"]["HOME"]))  # directorio temporal, vacío y ya borrado

    def test_gh_enforces_the_identity_too(self):
        self.verify()
        args = json.loads((self.bin / "log-proxy").read_text())["args"]
        pairs = dict(zip(args[5::2], args[6::2]))
        self.assertEqual(args[:3], ["attestation", "verify", f"oci://ghcr.io/papicuche/crm-gooddoggy/proxy@{self.digests['proxy']}"])
        self.assertEqual(pairs, {
            "--repo": "PapiCuche/crm-gooddoggy", "--cert-identity": WORKFLOW,
            "--cert-oidc-issuer": "https://token.actions.githubusercontent.com", "--source-ref": "refs/heads/main",
            "--source-digest": SHA, "--signer-digest": SHA, "--predicate-type": "https://slsa.dev/provenance/v1",
            "--deny-self-hosted-runners": "--format",
        })
        self.assertEqual(args[-1], "json")

    def test_one_matching_attestation_passes_in_any_order(self):
        bad = result(self.digests["backend"], {**certificate(), "sourceRepositoryRef": "refs/heads/feature"})
        good = result(self.digests["backend"])
        for order in ([bad, good], [good, bad]):
            self.gh_out("backend", order)
            self.assertEqual(self.verify(), 0, self.err)
        self.gh_out("backend", [bad, bad])
        self.assertBlocks("backend: no attestation of images.yml")

    def test_bundle_from_bundle_url_is_decompressed(self):
        bundle = {"mediaType": "application/vnd.dev.sigstore.bundle.v0.3+json", "pad": "x" * 700}
        self.api("frontend", [{"bundle": None, "bundle_url": "https://blob.example/a.json.sn?sig=1"}])
        self.web.routes["https://blob.example/a.json.sn?sig=1"] = snappy_literals(json.dumps(bundle).encode())
        self.assertEqual(self.verify(), 0, self.err)
        self.assertEqual(json.loads(json.loads((self.bin / "log-frontend").read_text())["bundles"]), bundle)

    def test_follows_full_pages(self):
        items = [{"bundle": {"n": i}} for i in range(100)]
        self.api("backend", items)
        self.web.routes[f"{API}{self.digests['backend']}?predicate_type=provenance&per_page=100&page=2"] = (
            json.dumps({"attestations": [{"bundle": {"n": 100}}]}).encode())
        self.assertEqual(self.verify(), 0, self.err)
        self.assertEqual(len(json.loads((self.bin / "log-backend").read_text())["bundles"].splitlines()), 101)


class MismatchBlocksTest(VerifierCase):
    def test_every_certificate_field_must_match(self):
        changes = {
            "subjectAlternativeName": WORKFLOW.replace("images.yml", "images-pr.yml"),
            "buildSignerURI": WORKFLOW.replace("refs/heads/main", "refs/heads/evil"),
            "sourceRepositoryRef": "refs/pull/1/merge",
            "sourceRepositoryDigest": OTHER_SHA,
            "buildSignerDigest": OTHER_SHA,
            "sourceRepositoryURI": "https://github.com/someone/crm-gooddoggy",
            "issuer": "https://accounts.example.com",
            "runnerEnvironment": "self-hosted",
            "githubWorkflowTrigger": "workflow_dispatch",
            "sourceRepositoryIdentifier": "1393140902",
            "sourceRepositoryOwnerIdentifier": 116849344,  # entero, no la cadena del certificado
        }
        for field, value in changes.items():
            with self.subTest(field=field):
                self.gh_out("proxy", [result(self.digests["proxy"], {**certificate(), field: value})])
                self.assertBlocks("proxy: no attestation of images.yml on refs/heads/main")
                cert = certificate()
                del cert[field]
                self.gh_out("proxy", [result(self.digests["proxy"], cert)])
                self.assertBlocks("proxy: no attestation")

    def test_other_digest_or_predicate(self):
        self.gh_out("backend", [result(self.digests["frontend"])])
        self.assertBlocks("backend: no attestation")
        self.gh_out("backend", [result(self.digests["backend"], predicate_type="https://spdx.dev/Document/v2.3")])
        self.assertBlocks("backend: no attestation")
        self.gh_out("backend", [{"verificationResult": {"statement": "junk"}}])
        self.assertBlocks("backend: no attestation")

    def test_missing_attestation(self):
        self.api("frontend", [])
        self.assertBlocks("frontend: no provenance attestation for this digest")
        self.assertIn("verified backend", self.out)  # las demás se informan, pero el resultado es 1

    def test_gh_failures(self):
        (self.bin / "rc").write_text("1")
        self.assertBlocks("gh attestation exited 1")
        (self.bin / "rc").write_text("0")
        (self.bin / "out-backend").write_text("not json")
        self.assertBlocks("backend: invalid JSON")
        self.gh_out("backend", {"verificationResult": {}})
        self.assertBlocks("backend: gh returned no list")
        self.gh_out("backend", [])
        self.assertBlocks("backend: no attestation of images.yml")

    def test_gh_version_is_pinned(self):
        for version in ("gh version 2.103.30 (2026-10-26)", "gh version 2.98.0 (2026-08-20)"):
            (self.bin / "version").write_text(version)
            self.assertBlocks(f"gh must be version {va.GH_VERSION}")

    def test_gh_missing_or_hanging(self):
        with mock.patch.dict(os.environ, {"PATH": str(self.dir / "nowhere")}):
            self.assertBlocks("gh --version")
        with mock.patch.object(va.subprocess, "run", side_effect=subprocess.TimeoutExpired("gh", 1)):
            self.assertBlocks("timed out")

    def test_network_and_api_errors(self):
        self.web.routes[API + self.digests["backend"]] = va.Fail("HTTP Error 403: rate limit exceeded")
        self.assertBlocks("backend: HTTP Error 403")
        self.web.routes[API + self.digests["backend"]] = b"<html>"
        self.assertBlocks("backend: invalid JSON")
        self.web.routes[API + self.digests["backend"]] = b'{"message": "Not Found"}'
        self.assertBlocks("backend: unexpected response format")
        self.api("backend", [{"bundle": None, "bundle_url": "http://blob.example/a.sn"}])
        self.assertBlocks("backend: bundle_url is not https")
        self.api("backend", [{"bundle": None, "bundle_url": "https://blob.example/a.sn"}])
        self.web.routes["https://blob.example/a.sn"] = b"\x05\x00ab"
        self.assertBlocks("backend: truncated snappy data")
        self.web.routes["https://blob.example/a.sn"] = snappy_literals(b'["not", "a", "bundle"]')
        self.assertBlocks("backend: an attestation bundle is not a JSON object")
        self.api("backend", [{"bundle": "inline but not an object"}])
        self.assertBlocks("backend: an attestation bundle is not a JSON object")

    def test_random_bundle_bodies_block_the_release(self):
        rng = random.Random(48)
        self.api("backend", [{"bundle": None, "bundle_url": "https://blob.example/r.sn"}])
        for _ in range(30):
            self.web.routes["https://blob.example/r.sn"] = rng.randbytes(rng.randrange(1, 40))
            self.assertBlocks("backend: ")

    def test_real_fetch_fails_closed(self):
        for error in (urllib.error.HTTPError("u", 429, "Too Many Requests", None, None),
                      urllib.error.URLError("offline"), TimeoutError("timed out")):
            with mock.patch.object(va.urllib.request, "urlopen", side_effect=error), self.assertRaises(va.Fail):
                va.fetch("https://api.github.com/x", {})
        with self.assertRaisesRegex(va.Fail, "not https"):
            va.fetch("http://api.github.com/x", {})
        with mock.patch.object(va.urllib.request, "urlopen") as urlopen:
            urlopen.return_value.__enter__.return_value.read.return_value = b"{}"
            self.assertEqual(va.fetch("https://api.github.com/x", {"Accept": "a"}), b"{}")
            self.assertIsNone(urlopen.call_args.args[0].get_header("Authorization"))
            urlopen.return_value.__enter__.return_value.read.return_value = b"x" * (va.MAX_BYTES + 1)
            self.assertRaisesRegex(va.Fail, "larger", va.fetch, "https://api.github.com/x", {})

    def test_registry_checks(self):
        amd64 = {"platform": {"os": "linux", "architecture": "amd64"}}
        cases = {
            "the image label org.opencontainers.image.revision is not the SHA": {"label": OTHER_SHA},
            "the image label org.opencontainers.image.revision": {"label": SHA[:12]},  # un prefijo del SHA no vale
            "unexpected response format": {"label": None},
            "the index has no single linux/amd64 manifest": {"platform": ("linux", "arm64")},
            "blobs/sha256:": {"config_body": b'{"config": {"Labels": {}}}'},  # la config no es la de su hash
            "invalid digest": {"index": [{"digest": "../../evil", **amd64}]},
            "the index has no single linux/amd64": {"index": [{"digest": digest(b"a"), **amd64},
                                                             {"digest": digest(b"b"), **amd64}]},
        }
        for message, change in cases.items():
            with self.subTest(message):
                self.digests["frontend"] = self.registry("frontend", **change)
                self.api("frontend", [{"bundle": {"n": 1}}])
                self.gh_out("frontend", [result(self.digests["frontend"])])
                self.release.write_text(re.sub(r"(?m)^FRONTEND_IMAGE=.*@.*$",
                                               f"FRONTEND_IMAGE={va.REGISTRY_REPO}/frontend@{self.digests['frontend']}",
                                               self.release.read_text()))
                self.assertBlocks(f"frontend: {message}")

    def test_registry_manifest_must_hash_to_the_digest(self):
        index_url = f"https://ghcr.io/v2/papicuche/crm-gooddoggy/proxy/manifests/{self.digests['proxy']}"
        self.web.routes[index_url] = self.web.routes[index_url] + b" "
        self.assertBlocks("proxy: manifests/sha256:")


class ReleaseEnvTest(VerifierCase):
    def test_revision_must_be_the_sha(self):
        self.assertEqual(self.verify(OTHER_SHA), 1)
        self.assertIn("REVISION is not --sha", self.err)
        self.assertEqual(self.web.calls, [])
        self.assertEqual(self.verify(SHA.upper()), 1)
        self.assertIn("40 lowercase hex", self.err)

    def test_malformed_release_blocks_before_any_request(self):
        good = self.release.read_text()
        bad = {
            "foreign registry": good.replace("ghcr.io/papicuche/", "ghcr.io/evil/"),
            "tag, not digest": good.replace(f"proxy@{self.digests['proxy']}", f"proxy:{SHA}"),
            "wrong name": good.replace("crm-gooddoggy/proxy@", "crm-gooddoggy/backend@"),
            "missing image": "".join(line for line in good.splitlines(True) if not line.startswith("FRONTEND")),
            "repeated line": good + f"REVISION={SHA}\n",
            "blank line": good + "\n",
            "space": good.replace("REVISION=", "REVISION= "),
        }
        for case, text in bad.items():
            with self.subTest(case=case):
                self.release.write_text(text)
                self.assertBlocks("release.env")
                self.assertEqual(self.web.calls, [])
        self.release.unlink()
        self.assertBlocks("release.env")

    def test_cli(self):
        with mock.patch.object(va, "verify", return_value=1) as verify:
            self.assertEqual(va.main(["--sha", SHA, "r.env"]), 1)
        verify.assert_called_once_with("r.env", SHA)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as usage:
            va.main(["r.env"])
        self.assertEqual(usage.exception.code, 2)


class SnappyTest(unittest.TestCase):
    def test_literals_and_every_copy_form(self):
        self.assertEqual(va.unsnappy(snappy_literals(b"x" * 1000)), b"x" * 1000)
        self.assertEqual(va.unsnappy(b"\x00"), b"")
        # "abcd" + copy1 (len 4, off 4) + copy2 (len 3, off 2, overlapping) + copy4 (len 2, off 11)
        stream = b"\x0d" + b"\x0cabcd" + bytes([0x01, 0x04]) + bytes([0x0A, 0x02, 0x00]) + bytes([0x07, 11, 0, 0, 0])
        self.assertEqual(va.unsnappy(stream), b"abcdabcdcdcab")
        long_literal = bytes([200, 1]) + bytes([61 << 2, 199, 0]) + b"y" * 200  # literal with 2-byte length
        self.assertEqual(va.unsnappy(long_literal), b"y" * 200)
        self.assertEqual(va.unsnappy(bytes([100, 60 << 2, 99]) + b"z" * 100), b"z" * 100)  # 1-byte length

    def test_malformed_input_fails(self):
        cases = {
            b"": "truncated", b"\x80": "truncated", b"\xff\xff\xff\xff\xff\x01": "too long",
            b"\x05\x00a": "shorter than declared", b"\x05\x08ab": "truncated", b"\x03\x04ab": "shorter than declared",
            b"\x05\x00a\x01\x02": "out of range", b"\x05\x00a\x01\x00": "out of range", b"\x02\x01": "truncated",
            b"\x08\x00a\x0e\x00": "truncated", b"\x04\xfc\xff\xff\xff\xff": "truncated",
        }
        for data, message in cases.items():
            with self.subTest(data=data), self.assertRaisesRegex(va.Fail, message):
                va.unsnappy(data)

    def test_declared_size_is_capped(self):
        too_big = va.MAX_BYTES + 1
        varint = bytes([too_big & 0x7F | 0x80, too_big >> 7 & 0x7F | 0x80, too_big >> 14 & 0x7F | 0x80, too_big >> 21])
        with self.assertRaisesRegex(va.Fail, "over the cap"):
            va.unsnappy(varint + b"\x00a")
        bomb = bytes([0x80, 0x80, 0x80, 0x04]) + b"\x00a" + b"\xfe\x01\x00" * 200_000  # copies: ~13 MB if unbounded
        with self.assertRaisesRegex(va.Fail, "longer than declared"):  # se corta en el tope, no al final
            va.unsnappy(bomb)

    def test_fuzz_never_hangs_or_exceeds_the_cap(self):
        rng = random.Random(1168)
        seed = snappy_literals(json.dumps({"mediaType": "bundle", "x": list(range(50))}).encode())
        seed += bytes([0x01, 0x10, 0x0A, 0x20, 0x00])
        inputs = [rng.randbytes(rng.randrange(1, 64)) for _ in range(200)]
        for _ in range(200):
            data = bytearray(seed)
            for _ in range(rng.randrange(1, 4)):
                data[rng.randrange(len(data))] = rng.randrange(256)
            inputs.append(bytes(data[: rng.randrange(1, len(data) + 1)]))
        start = time.monotonic()
        for data in inputs:
            try:
                self.assertLessEqual(len(va.unsnappy(data)), va.MAX_BYTES)
            except va.Fail:
                pass
        self.assertLess(time.monotonic() - start, 20)



if __name__ == "__main__":
    unittest.main()
