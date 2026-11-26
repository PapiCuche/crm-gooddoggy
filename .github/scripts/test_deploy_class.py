import contextlib
import io
import json
import os
import pathlib
import subprocess
import tempfile
import unittest

from deploy_class import DELIVERY_WORKFLOW, classify, declared_class, load_rules, main, path_reasons, top
from pr_governance import missing_sections

ROOT = pathlib.Path(__file__).resolve().parents[2]
IMPORTS = "from django.db import migrations, models\nfrom core.db.operations import CompositeTenantFK, EnableRLS\n"
CREATE = 'migrations.CreateModel(name="Box", fields=[("id", models.UUIDField(primary_key=True))])'
INITIAL = IMPORTS + '\n\nclass Migration(migrations.Migration):\n    initial = True\n    operations = [\n' \
    '        migrations.CreateModel(name="Item", fields=[("id", models.UUIDField(primary_key=True))]),\n    ]\n'
BASE = {
    "backend/apps/shop/models.py": "x = 1\n",
    "backend/apps/shop/migrations/__init__.py": "",
    "backend/apps/shop/migrations/0001_initial.py": INITIAL,
    "frontend/src/app/page.tsx": "export {}\n",
    "docs/readme.md": "doc\n",
    ".github/workflows/ci.yml": "on: push\n",
}
MIG = "backend/apps/shop/migrations/0002_change.py"


def migration(*ops, atomic=None, imports=IMPORTS, extra=""):
    flag = f"    atomic = {atomic}\n" if atomic is not None else ""
    body = "".join(f"        {op},\n" for op in ops)
    return f'{imports}\n\nclass Migration(migrations.Migration):\n{flag}{extra}' \
        f'    dependencies = [("shop", "0001_initial")]\n    operations = [\n{body}    ]\n'


class Repo:
    def __init__(self, test):
        tmp = tempfile.TemporaryDirectory()
        test.addCleanup(tmp.cleanup)
        self.path = tmp.name
        self.git("init", "-q", "-b", "main")
        self.base = self.commit(BASE)

    def git(self, *args):
        cmd = ["git", "-C", self.path, "-c", "user.name=t", "-c", "user.email=t@example.com", *args]
        return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout.strip()

    def commit(self, files, msg="change"):
        for name, text in files.items():
            target = pathlib.Path(self.path, name)
            if text is None:
                target.unlink()
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text)
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", msg)
        return self.git("rev-parse", "HEAD")

    def report(self, declared=None, base=None, head="HEAD"):
        return classify(self.path, base or self.base, head, declared)


class DeployClassTest(unittest.TestCase):
    def cls(self, files, **kw):
        repo = Repo(self)
        repo.commit(files)
        return repo.report(**kw)["class"]

    def migration_class(self, *ops, **kw):
        return self.cls({MIG: migration(*ops, **kw)})

    def test_one_class_per_row_of_section_2(self):
        cases = [
            ({"docs/x.md": "d", ".github/workflows/ci.yml": "on: pull_request\n"}, "none"),
            ({"frontend/src/a.ts": "a", "frontend/messages/es-PE.json": "{}"}, "A1"),
            ({"backend/apps/shop/views.py": "v", "frontend/src/a.ts": "a"}, "A2"),
            ({MIG: migration(CREATE)}, "A3"),
            ({"backend/apps/accounts/auth.py": "a"}, "B"),
            ({"backend/uv.lock": "l"}, "B"),
            ({"frontend/Dockerfile": "FROM x"}, "B"),
            ({"infra/prod/compose.yaml": "c"}, "C"),
        ]
        for files, expected in cases:
            with self.subTest(files=list(files)):
                self.assertEqual(self.cls(files), expected)

    def test_new_table_with_rls_index_and_constraint_is_a3(self):
        ops = (CREATE, 'EnableRLS("Box")', 'migrations.AddIndex(model_name="box", index=models.Index(fields=["id"], name="i"))',
               'migrations.AddConstraint(model_name="box", constraint=models.UniqueConstraint(fields=["id"], name="u"))',
               'migrations.AddField(model_name="box", name="n", field=models.IntegerField(default=0))',
               'CompositeTenantFK("Box", "id", "Box", "fk")')
        self.assertEqual(self.migration_class(*ops), "A3")

    def test_add_field_on_existing_table(self):
        cases = [
            ("models.IntegerField(null=True)", "A3"),
            ("models.IntegerField(db_default=0)", "A3"),
            ("models.IntegerField(null=True, default=1)", "A3"),
            ("models.IntegerField(default=0)", "C"),  # default= de Django en NOT NULL no es A3
            ("models.IntegerField()", "C"),
            ("models.IntegerField(null=NULLABLE)", "C"),
            ("models.IntegerField(null=False, default=0)", "C"),
            ("models.IntegerField(null=True, unique=True)", "B"),
            ("models.IntegerField(null=True, db_index=True)", "B"),
            ('models.ForeignKey(null=True, on_delete=models.CASCADE, to="shop.item")', "B"),
            ('models.ForeignKey(null=True, db_index=False, db_constraint=False, on_delete=models.CASCADE, to="x")', "A3"),
        ]
        for field, expected in cases:
            with self.subTest(field=field):
                self.assertEqual(self.migration_class(f'migrations.AddField(model_name="item", name="n", field={field})'), expected)

    def test_index_on_existing_table_is_b(self):
        op = 'migrations.AddIndex(model_name="Item", index=models.Index(fields=["id"], name="i"))'
        self.assertEqual(self.migration_class(op), "B")

    def test_concurrent_index_needs_a_non_atomic_migration(self):
        imports = IMPORTS + "from django.contrib.postgres.operations import AddIndexConcurrently\n"
        op = 'AddIndexConcurrently(model_name="item", index=models.Index(fields=["id"], name="i"))'
        self.assertEqual(self.migration_class(op, atomic=False, imports=imports), "A3")
        self.assertEqual(self.migration_class(op, imports=imports), "C")
        self.assertEqual(self.migration_class(op, atomic=True, imports=imports), "C")

    def test_operations_outside_section_2_2_are_c(self):
        sdf = "from core.db.operations import SecurityDefinerFunction\n"
        for op in ("migrations.RunPython(f)", 'migrations.RunSQL("SELECT 1")',
                   "migrations.SeparateDatabaseAndState(state_operations=[], database_operations=[])",
                   'migrations.AlterField(model_name="item", name="id", field=models.UUIDField(primary_key=True))',
                   'migrations.RemoveField(model_name="item", name="x")', 'migrations.DeleteModel(name="Item")',
                   'migrations.RenameField(model_name="item", old_name="a", new_name="b")',
                   'migrations.AddConstraint(model_name="item", constraint=models.CheckConstraint(name="c"))',
                   'EnableRLS("Item")', 'CompositeTenantFK("Item", "id", "Item", "fk")',
                   'SecurityDefinerFunction("f", "", "", "int", "SELECT 1")', 'migrations.AlterModelOptions(name="item")'):
            with self.subTest(op=op):
                self.assertEqual(self.migration_class(op, imports=IMPORTS + sdf), "C")

    def test_composite_fk_to_an_existing_table_is_b(self):
        self.assertEqual(self.migration_class(CREATE, 'CompositeTenantFK("Box", "item", "Item", "fk")'), "B")

    def test_imports_decide_the_operation_not_its_name(self):
        spoofs = [
            (IMPORTS + "from django.db.migrations import RunSQL as CreateModel\n", 'CreateModel("SELECT 1")'),
            (IMPORTS + "from mylib import migrations\n", CREATE),
            (IMPORTS + "migrations = models\n", CREATE),
        ]
        for imports, op in spoofs:
            with self.subTest(imports=imports.splitlines()[-1]):
                self.assertEqual(self.migration_class(op, imports=imports), "C")

    def test_migrations_it_cannot_analyze_are_c(self):
        for files in (
            {MIG: "class Migration(:\n"},
            {MIG: IMPORTS + "import os\nos.system('x')\n" + migration(CREATE, imports="")},
            {MIG: migration(imports=IMPORTS + "OPS = []\n").replace("operations = [\n    ]", "operations = OPS")},
            {MIG: migration(CREATE, extra="    def apply(self, *a):\n        pass\n")},
            {MIG: migration(CREATE, imports=IMPORTS + "X = print('x')\n")},
            {"backend/apps/shop/migrations/notes.txt": "x"},
            {"backend/apps/shop/migrations/sub/0003.py": migration(CREATE)},
            {"backend/apps/new/migrations/__init__.py": "import os\n"},
        ):
            with self.subTest(files=str(files)[:80]):
                self.assertEqual(self.cls(files), "C")
        self.assertEqual(self.cls({"backend/apps/new/migrations/__init__.py": "# paquete\n"}), "A3")

    def test_shipped_migrations_are_never_edited(self):
        self.assertEqual(self.cls({"backend/apps/shop/migrations/0001_initial.py": INITIAL + "\n"}), "C")
        self.assertEqual(self.cls({"backend/apps/shop/migrations/0001_initial.py": None}), "C")
        repo = Repo(self)
        repo.commit({MIG: migration('migrations.AddField(model_name="item", name="n", field=models.IntegerField())')})
        repo.commit({MIG: migration(CREATE)})  # una migración nueva del rango se corrige y se analiza
        commits = repo.report()["commits"]
        self.assertEqual([c["class"] for c in commits], ["C", "A3"])

    def test_unknown_paths_are_c(self):
        for path in ("infra/docker/compose.yaml", "frontend/tsconfig.json", "Makefile", "infra/env/.env.example", "x.bin"):
            with self.subTest(path=path):
                self.assertEqual(self.cls({path: "x"}), "C")

    def test_table_covers_every_sensitive_path_of_section_2_1(self):
        rules = load_rules()
        sensitive = [
            "backend/apps/accounts/x.py", "backend/apps/access/x.py", "backend/apps/members/x.py",
            "backend/apps/organizations/x.py", "backend/apps/provisioning/x.py", "backend/core/tenancy/x.py",
            "backend/core/db/operations.py", "backend/core/sealed.py", "backend/core/redaction.py",
            "backend/core/http.py", "backend/config/settings.py", "backend/config/urls.py", "backend/config/asgi.py",
            "frontend/src/proxy.ts", "frontend/next.config.ts", "backend/apps/ai_gateway/x.py",
            "backend/apps/ai_agents/x.py", "backend/apps/pricing/x.py", "backend/core/money.py",
            "backend/apps/quotes/approvals.py", "backend/apps/inventory/reservations.py",
            ".github/workflows/images.yml", ".github/workflows/release.yml", ".github/scripts/deploy_class.py",
            "backend/pyproject.toml", "backend/uv.lock", "frontend/package.json", "frontend/pnpm-lock.yaml",
            "backend/Dockerfile", "frontend/Dockerfile",
        ] + [f"backend/apps/conversations/{f}.py" for f in (
            "ingress", "inbound", "meta_webhook", "meta_page", "whatsapp", "graph", "channels", "accounts",
            "credentials", "delivery", "outbound_media", "limiter", "throttle")]
        for path in sensitive:
            with self.subTest(path=path):
                self.assertIn(top(r["class"] for r in path_reasons(rules, path)), ("B", "C"))
        for path in ("infra/prod/Caddyfile.shared", "infra/prod/compose.shared.yaml", "infra/prod/deploy.sh",
                     "infra/prod/host-mode.sh", ".github/scripts/deploy_class_rules.json"):
            with self.subTest(path=path):
                self.assertEqual(top(r["class"] for r in path_reasons(rules, path)), "C")

    def test_delivery_workflows_are_at_least_b(self):
        self.assertEqual(self.cls({".github/workflows/x.yml": "jobs:\n  a:\n    environment: production\n"}), "B")
        self.assertEqual(self.cls({".github/workflows/x.yml": "run: echo ${{ secrets.DEPLOY_KEY }}\n"}), "B")
        self.assertEqual(self.cls({".github/workflows/x.yml": "env:\n  T: ${{ secrets.GITHUB_TOKEN }}\n"}), "none")
        none = {p.name for p in (ROOT / ".github/workflows").glob("*.yml") if not DELIVERY_WORKFLOW.search(p.read_text())}
        # Lista revisada: ninguno construye, publica ni despliega imágenes ni usa más secreto que GITHUB_TOKEN.
        self.assertEqual(none, {"deploy-class.yml", "pr-governance.yml", "pr-governance-trusted.yml", "security.yml",
                                "work-item-dependencies.yml", "work-item-state.yml"})

    def test_release_is_the_highest_commit_even_if_reverted(self):
        repo = Repo(self)
        repo.commit({"infra/prod/deploy.sh": "x"})
        repo.commit({"infra/prod/deploy.sh": None})
        repo.commit({"frontend/src/a.ts": "a"})
        report = repo.report()
        self.assertEqual([c["class"] for c in report["commits"]], ["C", "C", "A1"])
        self.assertEqual(report["class"], "C")
        self.assertEqual({d["path"] for d in report["decisive"]}, {"infra/prod/deploy.sh"})

    def test_merges_classify_their_own_changes(self):
        repo = Repo(self)
        repo.git("checkout", "-q", "-b", "side")
        repo.commit({"backend/apps/shop/views.py": "v"})
        repo.git("checkout", "-q", "main")
        repo.commit({"frontend/src/a.ts": "a"})
        repo.git("merge", "-q", "--no-commit", "side")
        repo.commit({"infra/prod/compose.yaml": "evil merge"}, msg="merge")
        report = repo.report()
        self.assertEqual(report["class"], "C")
        self.assertEqual([c["class"] for c in report["commits"]], ["A2", "A1", "C"])

    def test_symlinks_are_c(self):
        repo = Repo(self)
        os.symlink("/etc/passwd", os.path.join(repo.path, "frontend/src/link.ts"))
        repo.commit({})
        self.assertEqual(repo.report()["class"], "C")

    def test_bad_ranges_fail_closed(self):
        repo = Repo(self)
        head = repo.commit({"frontend/src/a.ts": "a"})
        backwards = repo.report(base=head, head=repo.base)
        self.assertEqual(backwards["class"], "C")
        self.assertIn("no desciende", backwards["errors"][0])
        self.assertEqual(repo.report(base="0" * 40)["class"], "C")
        self.assertEqual(repo.report(base=head)["class"], "none")  # rango vacío

    def test_declared_class_only_raises(self):
        repo = Repo(self)
        repo.commit({"backend/apps/shop/views.py": "v"})
        self.assertEqual(repo.report("B")["class"], "B")
        lower = repo.report("A1")
        self.assertEqual((lower["class"], lower["computed"]), ("A2", "A2"))
        self.assertIn("se ignora", lower["notes"][0])
        self.assertEqual(repo.report("invalid")["class"], "C")

    def test_declared_class_from_the_pr_body(self):
        section = "## Clase de despliegue\n\n<!-- Declarada: C -->\n\nDeclarada: {}\n\n## Riesgos y deuda técnica\n"
        self.assertEqual(declared_class(section.format("`B`")), "B")
        self.assertEqual(declared_class(section.format("")), None)
        self.assertEqual(declared_class(section.format("X9")), "invalid")
        self.assertIsNone(declared_class("## Objetivo\n\nDeclarada: C\n"))  # sin la sección: sin declaración
        self.assertIsNone(declared_class(None))

    def test_template_keeps_governance_and_declares_nothing_by_default(self):
        template = (ROOT / ".github/pull_request_template.md").read_text()
        self.assertEqual(missing_sections(template), [])
        self.assertIn("## Clase de despliegue", template)
        self.assertIsNone(declared_class(template))
        old_body = template.replace("## Clase de despliegue", "## Otra")
        self.assertEqual(missing_sections(old_body), [])  # un PR sin la sección sigue pasando governance

    def test_cli_writes_json_and_markdown(self):
        repo = Repo(self)
        repo.commit({"frontend/src/a.ts": "a"})
        event = pathlib.Path(repo.path, "event.json")
        event.write_text(json.dumps({"pull_request": {"body": "## Clase de despliegue\n\nDeclarada: B\n"}}))
        out = pathlib.Path(repo.path, "out.json")
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = main(["--repo", repo.path, "--base", repo.base, "--head", "HEAD", "--event", str(event),
                         "--json", str(out), "--markdown"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.read_text())["class"], "B")
        self.assertIn("## Clase de despliegue: **B**", stdout.getvalue())
        self.assertIn("frontend/src/a.ts", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
