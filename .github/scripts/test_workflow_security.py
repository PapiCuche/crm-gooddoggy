"""Trust boundary de los workflows `pull_request_target` (A-02, delivery-automation.md §5).

Análisis estático con la stdlib: ningún workflow trusted hace checkout del head del PR,
ejecuta código del PR, interpola `${{ }}` dentro de `run` ni pide permisos de escritura
más allá de los permitidos.
"""

import pathlib
import re
import subprocess
import tempfile
import unittest

WORKFLOWS = pathlib.Path(__file__).resolve().parents[1] / "workflows"
ROOT = WORKFLOWS.parents[1]
BUILD = "bash infra/prod/build-images.sh \"$RUNNER_TEMP/tested\" \"$RUNNER_TEMP/gitleaks\""
ALLOWED_WRITES = {"work-item-state.yml": {"issues"}, "work-item-dependencies.yml": {"issues"}}
TRUSTED_REFS = ("github.event.repository.default_branch", "github.event.pull_request.base.sha")
FORBIDDEN_IN_RUN = re.compile(r"\b(pip3? install|npm (ci|install)|pnpm install|uv sync|eval)\b")


def triggers(text):
    """Claves del bloque `on:` (solo ese bloque; no confundir con `permissions: issues:`)."""
    m = re.search(r"^on:\n((?:[ \t]+.*\n|\s*\n)+)", text, re.M)
    return set(re.findall(r"^  ([\w-]+):", m.group(1), re.M)) if m else set()


def trusted_workflows():
    # Trusted = se ejecuta con el código de la rama por defecto y el token del repo base.
    return [p for p in sorted(WORKFLOWS.glob("*.yml")) if triggers(p.read_text()) & {"pull_request_target", "issues"}]


def run_blocks(text):
    lines = text.splitlines()
    blocks = []
    for i, line in enumerate(lines):
        m = re.match(r"^(\s*)(?:- )?run:\s*(.*)$", line)
        if not m:
            continue
        indent, inline = len(m.group(1)), m.group(2)
        if inline not in ("|", ">", "|-", ">-"):
            blocks.append(inline)
            continue
        body = []
        for nxt in lines[i + 1:]:
            if nxt.strip() and len(nxt) - len(nxt.lstrip()) <= indent:
                break
            body.append(nxt)
        blocks.append("\n".join(body))
    return blocks


def permissions(text):
    m = re.search(r"^permissions:\n((?:[ \t]+.*\n?)+)", text, re.M)
    return dict(re.findall(r"^\s+([\w-]+):\s*(\w+)", m.group(1), re.M)) if m else {}


class TrustBoundaryTest(unittest.TestCase):
    def test_trusted_workflows_exist(self):
        names = {p.name for p in trusted_workflows()}
        self.assertTrue({"pr-governance-trusted.yml", "work-item-state.yml", "work-item-dependencies.yml"} <= names, names)

    def test_trusted_governance_job_name(self):
        text = (WORKFLOWS / "pr-governance-trusted.yml").read_text()
        self.assertRegex(text, r"(?m)^\s+name: PR governance \(trusted\)\s*$")

    def test_no_head_references(self):
        # head.sha nunca; head.ref solo como DATO en una variable de entorno (env: KEY: ${{ ... }}).
        env_ref = re.compile(r"^\s+[A-Z][A-Z0-9_]*:\s*\$\{\{\s*github\.event\.pull_request\.head\.ref\s*\}\}\s*$")
        for p in trusted_workflows():
            for line in p.read_text().splitlines():
                if re.search(r"pull_request\.head\.sha\b", line):
                    self.fail(f"{p.name}: head.sha en workflow trusted: {line.strip()}")
                if re.search(r"pull_request\.head\.ref\b", line):
                    self.assertRegex(line, env_ref, f"{p.name}: head.ref fuera de env: {line.strip()}")

    def test_checkout_only_trusted_refs(self):
        for p in trusted_workflows():
            text = p.read_text()
            for m in re.finditer(r"uses:\s*actions/checkout@\S+[^\n]*\n((?:\s+.*\n)*?)(?=\s*- |\Z)", text):
                refs = re.findall(r"ref:\s*(.+)", m.group(1))
                self.assertTrue(refs, f"{p.name}: checkout sin ref explícito")
                for ref in refs:
                    self.assertTrue(any(t in ref for t in TRUSTED_REFS), f"{p.name}: ref no trusted {ref}")

    def test_no_expressions_or_installs_in_run(self):
        for p in trusted_workflows():
            for block in run_blocks(p.read_text()):
                self.assertNotIn("${{", block, f"{p.name}: interpolación en run")
                self.assertIsNone(FORBIDDEN_IN_RUN.search(block), f"{p.name}: comando prohibido")

    def test_no_local_actions(self):
        for p in trusted_workflows():
            self.assertNotRegex(p.read_text(), r"uses:\s*\./", p.name)

    def test_minimal_permissions(self):
        for p in trusted_workflows():
            perms = permissions(p.read_text())
            self.assertTrue(perms, f"{p.name}: sin bloque permissions")
            writes = {k for k, v in perms.items() if v != "read"}
            self.assertLessEqual(writes, ALLOWED_WRITES.get(p.name, set()), f"{p.name}: {perms}")


def jobs(text):
    """Bloque de texto de cada job (claves de dos espacios bajo `jobs:`)."""
    body = text[re.search(r"^jobs:\n", text, re.M).end():]
    parts = re.split(r"(?m)^  ([\w-]+):\s*\n", body)
    return dict(zip(parts[1::2], parts[2::2]))


def job_permissions(block):
    m = re.search(r"^    permissions:\n((?:      .*\n)+)", block, re.M)
    return dict(re.findall(r"^\s+([\w-]+):\s*(\w+)", m.group(1), re.M)) if m else {}


class ActionPinningTest(unittest.TestCase):
    """F1-15: toda acción, en todos los workflows, fijada por el SHA completo de su commit."""

    def test_every_action_pinned_by_sha(self):
        for p in sorted(WORKFLOWS.glob("*.yml")):
            for ref in re.findall(r"uses:\s*([^\s#]+)", p.read_text()):
                self.assertRegex(ref, r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$", f"{p.name}: {ref}")

    def test_only_images_publishes_packages(self):
        for p in sorted(WORKFLOWS.glob("*.yml")):
            if p.name != "images.yml":
                self.assertNotRegex(p.read_text(), r"packages:\s*write", p.name)

    def test_only_images_attests(self):
        # F1-20b: ningún otro workflow (images-pr.yml incluido: código del PR) firma ni atesta nada.
        for p in sorted(WORKFLOWS.glob("*.yml")):
            if p.name != "images.yml":
                self.assertNotRegex(p.read_text(), r"id-token|attestations:|actions/attest|cosign|sigstore", p.name)


class ImagesWorkflowTest(unittest.TestCase):
    """F1-15 (ADR-033 §7): publica en GHCR con `packages: write`; la frontera no se puede ensanchar."""

    text = (WORKFLOWS / "images.yml").read_text()

    def test_only_push_to_main(self):
        # Ni PRs (tampoco de forks), ni workflow_run, ni dispatch manual desde otra rama.
        self.assertEqual(triggers(self.text), {"push"})
        self.assertRegex(self.text, r"(?m)^on:\n  push:\n    branches: \[main\]\n\n")

    def test_packages_write_only_in_publish_job(self):
        self.assertEqual(permissions(self.text), {"contents": "read"})
        found = jobs(self.text)
        self.assertEqual(set(found), {"publish", "attest"})
        self.assertEqual(job_permissions(found["publish"]), {"contents": "read", "packages": "write"})
        self.assertEqual(len(re.findall(r"packages:\s*write", self.text)), 1)

    def test_attest_job_is_minimal(self):
        # F1-20b (ADR-048 §7): firmar solo tras publish, solo en main, sin checkout ni código del repo, y
        # sin tocar las imágenes (ni push-to-registry ni sesión en el registro).
        attest = jobs(self.text)["attest"]
        self.assertEqual(job_permissions(attest), {"id-token": "write", "attestations": "write"})
        self.assertEqual(len(re.findall(r"(?m)^\s+(?:id-token|attestations):", self.text)), 2)
        self.assertRegex(attest, r"(?m)^    needs: publish$")
        self.assertRegex(attest, r"(?m)^    if: github\.event_name == 'push' && github\.ref == 'refs/heads/main'$")
        self.assertEqual(run_blocks(attest), [])
        self.assertNotRegex(attest, r"checkout|push-to-registry|docker|secrets\.|github-token|continue-on-error")
        steps = re.findall(r"uses: (\S+) # v[\d.]+\n\s+with:\n\s+subject-name: (.+)\n\s+subject-digest: (.+)\n", attest)
        self.assertEqual(len(re.findall(r"- name:", attest)), 3)
        self.assertEqual(len(re.findall(r"(?m)^          \S", attest)), 6)  # solo subject-name y subject-digest
        self.assertEqual(steps, [
            ("actions/attest@1e69f48acb82d1966a394da916b4c1698aa569d6", f"${{{{ needs.publish.outputs.repo }}}}/{n}",
             f"${{{{ needs.publish.outputs.{n} }}}}") for n in ("backend", "frontend", "proxy")
        ])

    def test_attested_digests_are_the_validated_release(self):
        # Las salidas de publish se escriben después de validar release.env, desde los mismos digests.
        self.assertIn('echo "$name=$digest" >> "$RUNNER_TEMP/digests"', self.text)
        output = self.text.index('{ echo "repo=$repo"; cat "$RUNNER_TEMP/digests"; } >> "$GITHUB_OUTPUT"')
        self.assertLess(self.text.index("grep -qxE 'REVISION=[0-9a-f]{40}' \"$release\""), output)
        for name in ("repo", "backend", "frontend", "proxy"):
            self.assertIn(f"{name}: ${{{{ steps.push.outputs.{name} }}}}", self.text)

    def test_no_secret_but_github_token(self):
        self.assertEqual(set(re.findall(r"\$\{\{\s*secrets\.(\w+)", self.text)), {"GITHUB_TOKEN"})
        self.assertNotRegex(self.text, r"pull_request|workflow_run")

    def test_no_expressions_in_run(self):
        for block in run_blocks(self.text):
            self.assertNotIn("${{", block)

    def test_checkout_without_persisted_credentials(self):
        self.assertEqual(self.text.count("actions/checkout@"), self.text.count("persist-credentials: false"))

    def test_builder_and_scanner_pinned_by_digest(self):
        for var in ("BUILDKIT_IMAGE", "SBOM_GENERATOR"):
            self.assertRegex(self.text, rf"(?m)^  {var}: \S+:[\w.-]+@sha256:[0-9a-f]{{64}}$")
        self.assertIn('--driver-opt "image=$BUILDKIT_IMAGE"', self.text)
        self.assertIn('--sbom="generator=$SBOM_GENERATOR"', self.text)

    def test_every_build_targets_the_pinned_builder(self):
        script = (ROOT / "infra/prod/build-images.sh").read_text()
        builds = re.findall(r"docker buildx build ([^\n]*)", self.text + script)
        self.assertEqual(len(builds), 2)  # la de prueba (build-images.sh, --load) y la de publicar (--push)
        for args in builds:
            self.assertIn("--builder crm-images", args)
        self.assertNotRegex(script, r"--push|docker login|GITHUB_TOKEN")

    def test_publishes_only_the_tested_config(self):
        # F1-15b: build-images.sh anota la configuración de cada imagen probada; publish compara con la
        # del registro antes de escribir su línea de release.env, que además lleva REVISION.
        script = (ROOT / "infra/prod/build-images.sh").read_text()
        self.assertIn('> "$out/$name.config"', script)
        self.assertIn(BUILD, self.text)
        self.assertLess(self.text.index(BUILD), self.text.index("--push"))
        check = self.text.index('[ "$pushed" = "$tested" ] || {')
        self.assertLess(self.text.index('tested=$(cat "$RUNNER_TEMP/tested/$name.config")'), check)
        self.assertLess(check, self.text.index('_IMAGE=$repo/$name@$digest" >> "$release"'))
        self.assertIn('echo "REVISION=$GITHUB_SHA" >> "$release"', self.text)


class DeployClassWorkflowTest(unittest.TestCase):
    """F1-20a: la clase de despliegue solo informa; solo lee el repositorio, sin secretos ni entornos."""

    text = (WORKFLOWS / "deploy-class.yml").read_text()

    def test_read_only_without_secrets_or_environments(self):
        self.assertEqual(triggers(self.text), {"pull_request", "push"})
        self.assertEqual(permissions(self.text), {"contents": "read"})
        self.assertEqual([job_permissions(b) for b in jobs(self.text).values()], [{}])
        self.assertNotRegex(self.text, r"secrets\.|environment:|continue-on-error")


class ImagesPullRequestTest(unittest.TestCase):
    """F1-15b: los PR construyen y prueban las imágenes como images.yml, sin credenciales ni publicar."""

    text = (WORKFLOWS / "images-pr.yml").read_text()
    publish = (WORKFLOWS / "images.yml").read_text()

    def test_only_pull_request(self):
        self.assertEqual(triggers(self.text), {"pull_request"})
        self.assertNotIn("pull_request_target", self.text)

    def test_read_only_without_secrets(self):
        self.assertEqual(permissions(self.text), {"contents": "read"})
        self.assertEqual(set(jobs(self.text)), {"build"})
        self.assertEqual(job_permissions(jobs(self.text)["build"]), {})
        self.assertNotRegex(self.text, r"secrets\.|packages|id-token|attestations:|docker login|--push|GITHUB_TOKEN")
        for block in run_blocks(self.text):
            self.assertNotIn("${{", block)
        self.assertEqual(self.text.count("actions/checkout@"), self.text.count("persist-credentials: false"))

    def test_same_build_as_publish(self):
        self.assertIn(BUILD, self.text)
        for var in ("BUILDKIT_IMAGE", "GITLEAKS_VERSION", "GITLEAKS_SHA256"):
            line = re.search(rf"(?m)^  {var}: .+$", self.publish).group(0)
            self.assertIn(line, self.text, var)
        builder = re.search(r"docker buildx create [^\n]+", self.publish).group(0)
        self.assertIn(builder, self.text)

    def test_runs_when_an_image_input_changes(self):
        for path in ("backend/**", "frontend/**", "infra/docker/**", "infra/prod/caddy/**", "infra/prod/build-images.sh"):
            self.assertIn(f'      - "{path}"', self.text)


class BaseImagePinningTest(unittest.TestCase):
    """F1-15b: validate.sh rechaza un FROM o un `# syntax=` sin digest en cualquier Dockerfile."""

    def validate(self, root=None):
        args = ["sh", str(ROOT / "infra/prod/validate.sh"), "--dockerfiles"] + ([str(root)] if root else [])
        return subprocess.run(args, capture_output=True, text=True)

    def test_repository_dockerfiles_are_pinned(self):
        result = self.validate()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_unpinned_bases_are_rejected(self):
        pin = "@sha256:" + "a" * 64
        good = f"# syntax=docker/dockerfile:1.27.1{pin}\nFROM python:3.14{pin} AS build\nFROM node:24{pin}\n"
        cases = {
            "ok": (good, 0),
            "from": (good + "FROM python:3.14-slim\n", 1),
            "lowercase": (good + "from python:3.14-slim\n", 1),
            "platform": (good + "FROM --platform=linux/amd64 node:24 AS x\n", 1),
            "short digest": (good + f"FROM node:24{pin[:-1]}\n", 1),
            "syntax": (good.replace(pin, "", 1), 1),
        }
        for name, (content, code) in cases.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as tmp:
                (pathlib.Path(tmp) / "sub").mkdir()
                (pathlib.Path(tmp) / "sub/Dockerfile.dev").write_text(content)
                result = self.validate(tmp)
                self.assertEqual(result.returncode, code, result.stderr)
                if code:
                    self.assertIn("sin @sha256:<digest>", result.stderr)
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self.validate(tmp).returncode, 1)  # ningún Dockerfile: raíz equivocada


if __name__ == "__main__":
    unittest.main()
