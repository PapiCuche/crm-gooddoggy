"""Deploy class of a release range (ADR-048 §2, §2.1 and §2.2; F1-20a).

Classifies every commit of `(base, head]` with the path table `deploy_class_rules.json` and the
expansion rules for new migrations, and reports the highest class with the reason of every file.
It fails closed: whatever it cannot classify, and any error, is class C. It only reads the local
git repository: no network, no token, no secrets. Phase 1: informative only. Standard library only.
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import html
import json
import pathlib
import re
import subprocess
import sys

ORDER = ("none", "A1", "A2", "A3", "B", "C")
RANK = {c: i for i, c in enumerate(ORDER)}
RULES_FILE = pathlib.Path(__file__).resolve().parent / "deploy_class_rules.json"
# Un workflow que construye, publica o despliega imágenes, o usa secretos o entornos, es B como mínimo.
DELIVERY_WORKFLOW = re.compile(
    r"environment:|packages:\s*write|deployments:\s*write|write-all|secrets(?:\.(?!GITHUB_TOKEN\b)|\[|:\s*inherit)"
    r"|docker (?:buildx )?build|build-push-action|build-images\.sh"
)
EXPANSION_OPS = {"AddField", "AddIndex", "AddConstraint", "EnableRLS", "CompositeTenantFK"}
OP_PREFIXES = {
    "django.db.migrations": None,
    "django.contrib.postgres.operations": {"AddIndexConcurrently"},
    "core.db.operations": {"EnableRLS", "CompositeTenantFK", "SecurityDefinerFunction"},
}
INDEXED_FIELDS = {"ForeignKey", "OneToOneField", "ForeignObject", "SlugField"}
MIGRATION_ATTRS = {"dependencies", "operations", "atomic", "initial"}
DECLARED_RE = re.compile(r"^##\s+Clase de despliegue\s*$(.*?)(?=^##\s|\Z)", re.M | re.S)


class Unanalyzable(Exception):
    """A migration the classifier cannot prove safe: class C."""


def glob(pattern: str) -> re.Pattern[str]:
    """`**/` any directories, `**` anything, `*` anything but `/`; always the full path."""
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
        elif pattern.startswith("**", i):
            out, i = out + ".*", i + 2
        elif pattern[i] == "*":
            out, i = out + "[^/]*", i + 1
        else:
            out, i = out + re.escape(pattern[i]), i + 1
    return re.compile(f"^{out}$")


MIGRATIONS = glob("backend/**/migrations/**")
WORKFLOWS = glob(".github/workflows/*")


def load_rules(path: pathlib.Path = RULES_FILE) -> list[tuple[str, str, re.Pattern[str], str]]:
    rules = json.loads(path.read_text(encoding="utf-8"))["rules"]
    if not rules or any(len(r) != 3 or r[0] not in RANK for r in rules):
        raise ValueError("deploy_class_rules.json inválido")
    return [(cls, pat, glob(pat), why) for cls, pat, why in rules]


def top(classes) -> str:
    return max(classes, key=RANK.__getitem__, default="none")


def reason(cls: str, rule: str, why: str) -> dict:
    return {"class": cls, "rule": rule, "why": why}


def app_of(path: str) -> str:
    return path.split("/migrations/", 1)[0].rsplit("/", 1)[-1]


class Git:
    def __init__(self, repo: str) -> None:
        self.repo = repo

    def run(self, *args: str) -> bytes:
        return subprocess.run(["git", "-C", self.repo, *args], check=True, capture_output=True).stdout

    def ok(self, *args: str) -> bool:
        return subprocess.run(["git", "-C", self.repo, *args], capture_output=True).returncode == 0

    def blob(self, sha: str, path: str) -> str:
        return self.run("cat-file", "blob", f"{sha}:{path}").decode("utf-8")

    def changes(self, sha: str) -> list[tuple[str, str, list[str]]]:
        """(path, status letters, modes) per file; a merge against EACH parent (-m: over-classifies, never hides)."""
        out = self.run("diff-tree", "-r", "-m", "--raw", "-z", "--no-renames", "--no-commit-id", "--root", sha)
        items, found = out.decode("utf-8").split("\0"), {}
        for meta, path in zip(items[0::2], items[1::2]):
            fields, (status, modes) = meta.lstrip(":").split(), found.get(path, ("", []))
            found[path] = (status + fields[-1], modes + fields[:2])
        return [(path, status, modes) for path, (status, modes) in found.items()]


def resolve(node: ast.AST, names: dict[str, str], bound: set[str]) -> str | None:
    """Dotted name of a call target through the module imports; None if it cannot be proven."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name) or node.id in bound or node.id not in names:
        return None
    return ".".join([names[node.id], *reversed(parts)])


def has_call(node: ast.AST) -> bool:
    return any(isinstance(n, ast.Call) for n in ast.walk(node))


def parse_migration(source: str) -> tuple[list[tuple[str, ast.Call]], bool]:
    """(operation name, call) of each operation and the `atomic` flag, or Unanalyzable."""
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise Unanalyzable(f"no se puede leer: {exc.msg}") from exc
    names: dict[str, str] = {}
    bound: set[str] = set()
    migration = None
    for i, node in enumerate(tree.body):
        if isinstance(node, ast.Import):
            for a in node.names:
                names[a.asname or a.name.split(".")[0]] = a.name if a.asname else a.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.update({a.asname or a.name: f"{node.module}.{a.name}" for a in node.names})
        elif i == 0 and isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            continue
        elif isinstance(node, (ast.Assign, ast.AnnAssign)) and not has_call(node):
            bound |= {n.id for n in ast.walk(node) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
        elif isinstance(node, ast.FunctionDef) and not node.decorator_list and not has_call(node.args):
            bound.add(node.name)
        elif isinstance(node, ast.ClassDef) and node.name == "Migration" and migration is None:
            migration = node
        else:
            raise Unanalyzable(f"línea {node.lineno}: código de módulo que el clasificador no analiza")
    if migration is None or migration.decorator_list or migration.keywords or len(migration.bases) != 1:
        raise Unanalyzable("sin una clase Migration simple")
    if resolve(migration.bases[0], names, bound) != "django.db.migrations.Migration":
        raise Unanalyzable("Migration no hereda de django.db.migrations.Migration")
    attrs: dict[str, ast.AST] = {}
    for j, node in enumerate(migration.body):
        if j == 0 and isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            continue
        if not (isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id in MIGRATION_ATTRS):
            raise Unanalyzable(f"línea {node.lineno}: Migration con contenido no analizable")
        attrs[node.targets[0].id] = node.value
    atomic = attrs.get("atomic", ast.Constant(True))
    ops = attrs.get("operations")
    if not isinstance(atomic, ast.Constant) or not isinstance(atomic.value, bool):
        raise Unanalyzable("atomic no es un literal")
    if not isinstance(ops, (ast.List, ast.Tuple)) or not all(isinstance(o, ast.Call) for o in ops.elts):
        raise Unanalyzable("operations no es una lista literal de operaciones")
    result = []
    for call in ops.elts:
        prefix, _, name = (resolve(call.func, names, bound) or "?").rpartition(".")
        allowed = OP_PREFIXES.get(prefix, set())  # None: cualquier operación de django.db.migrations
        result.append((name if allowed is None or name in allowed else f"desconocida {prefix}.{name}", call))
    return result, atomic.value


def arg(call: ast.Call, key: str, pos: int) -> ast.AST | None:
    for kw in call.keywords:
        if kw.arg == key:
            return kw.value
    return call.args[pos] if len(call.args) > pos else None


def literal(node: ast.AST | None) -> object:
    try:
        return ast.literal_eval(node) if node is not None else None
    except Exception:  # noqa: BLE001 - no literal: nunca prueba nada
        return Unanalyzable


def text(node: ast.AST | None) -> str:
    value = literal(node)
    if not isinstance(value, str):
        raise Unanalyzable("nombre de modelo no literal")
    return value.lower()


def op_class(op: str, call: ast.Call, app: str, created: set, atomic: bool) -> tuple[str, str]:
    """Class of one operation under §2.2."""
    if op == "CreateModel":
        return "A3", "CreateModel: tabla nueva (§2.2 regla 1)"
    if op == "AddIndexConcurrently":
        if atomic is False:
            return "A3", "AddIndexConcurrently en una migración atomic = False (§2.2 regla 3)"
        return "C", "AddIndexConcurrently sin atomic = False (§2.2 regla 3)"
    if op not in EXPANSION_OPS:
        return "C", f"{op}: no es de expansión; el clasificador no puede probar que sea compatible (§2.2 regla 4)"
    model = text(arg(call, "model_name", 0))
    new = (app, model) in created
    if op == "CompositeTenantFK":
        to = text(arg(call, "to_model", 2))
        if not new:
            return "C", f"CompositeTenantFK en la tabla existente {model} (§2.2 regla 4)"
        if (app, to) not in created:
            return "B", f"CompositeTenantFK puede añadir UNIQUE (organization_id, id) a la tabla existente {to}"
        return "A3", f"CompositeTenantFK entre tablas nuevas {model} → {to} (§2.2 regla 1)"
    if new:
        return "A3", f"{op} en {model}, creada en este release (§2.2 regla 1)"
    if op == "AddIndex":
        return "B", f"AddIndex en la tabla existente {model}: bloquea las escrituras mientras se construye (§2.2 regla 3)"
    if op != "AddField":
        return "C", f"{op} en la tabla existente {model} (§2.2 regla 4)"
    field = arg(call, "field", 2)
    if not isinstance(field, ast.Call) or any(k.arg is None for k in field.keywords):
        raise Unanalyzable("AddField con un campo no literal")
    kw = {k.arg: k.value for k in field.keywords}
    if literal(kw.get("null")) is not True and literal(kw.get("db_default")) is None:
        return "C", f"AddField NOT NULL sin db_default en la tabla existente {model}: la imagen anterior no lo inserta (§2.2 regla 2)"
    kind = field.func.attr if isinstance(field.func, ast.Attribute) else getattr(field.func, "id", "?")
    plain_fk = literal(kw.get("db_index")) is False and literal(kw.get("db_constraint")) is False
    if (kind in INDEXED_FIELDS and not plain_fk) or any(literal(kw.get(k)) is True for k in ("unique", "db_index", "primary_key")):
        return "B", f"AddField construye un índice o una FK en la tabla existente {model}: bloquea escrituras (§2.2 regla 3)"
    return "A3", f"AddField nulo o con db_default en la tabla existente {model} (§2.2 regla 2)"


def migration_reasons(git: Git, sha: str, base: str, path: str, status: str, created: set) -> list[dict]:
    rule = "migración (§2.2)"
    if "D" in status:
        return [reason("C", rule, "borra una migración")]
    if git.ok("cat-file", "-e", f"{base}:{path}"):
        return [reason("C", rule, "cambia una migración que producción ya tiene")]
    if not path.endswith(".py") or "/" in path.split("/migrations/", 1)[1]:
        return [reason("C", rule, "archivo de migraciones que no es una migración Python")]
    source = git.blob(sha, path)
    if path.endswith("/__init__.py"):
        empty = all(not ln.strip() or ln.lstrip().startswith("#") for ln in source.splitlines())
        return [reason("A3" if empty else "C", rule, "paquete de migraciones" + ("" if empty else " con código"))]
    try:
        ops, atomic = parse_migration(source)
        found = [reason(c, f"{rule} línea {call.lineno}", w) for op, call in ops
                 for c, w in [op_class(op, call, app_of(path), created, atomic)]]
        return found or [reason("A3", rule, "migración sin operaciones")]
    except Unanalyzable as exc:
        return [reason("C", rule, f"migración no analizable: {exc}")]


def path_reasons(rules, path: str) -> list[dict]:
    """Every table rule that matches (they can only raise the class); none at all is C."""
    found = [reason(cls, pat, why) for cls, pat, rx, why in rules if rx.match(path)]
    return found or [reason("C", "(ninguna)", "ruta que el clasificador no reconoce")]


def file_reasons(git: Git, rules, sha: str, base: str, path: str, status: str, modes: list[str],
                 created: set) -> list[dict]:
    found = path_reasons(rules, path)
    if set(status) - set("AMD"):
        found.append(reason("C", "estado", f"cambio de tipo o estado {status}"))
    if {"120000", "160000"} & set(modes):
        found.append(reason("C", "modo", "enlace simbólico o submódulo"))
    if MIGRATIONS.match(path):
        found += migration_reasons(git, sha, base, path, status, created)
    if WORKFLOWS.match(path) and "D" not in status and DELIVERY_WORKFLOW.search(git.blob(sha, path)):
        found.append(reason("B", "workflow", "construye, publica o despliega imágenes, o usa secretos o entornos"))
    return found


def declared_class(body: str | None) -> str | None:
    """`Declarada:` of the `## Clase de despliegue` section; None without one, 'invalid' if unreadable."""
    section = DECLARED_RE.search(re.sub(r"<!--.*?-->", "", body or "", flags=re.S))
    m = re.search(r"^Declarada:[ \t]*(.*)$", section.group(1), re.M) if section else None
    value = m.group(1).strip().strip("`*").strip() if m else ""
    return None if not value else (value if value in RANK else "invalid")


def classify(repo: str, base: str, head: str, declared: str | None = None) -> dict:
    report: dict = {"schema": 1, "base": base, "head": head, "commits": [], "errors": []}
    try:
        git, rules = Git(repo), load_rules()
        b, h = (git.run("rev-parse", "--verify", "--end-of-options", f"{r}^{{commit}}").decode().strip()
                for r in (base, head))
        report.update(base=b, head=h)
        if not git.ok("merge-base", "--is-ancestor", b, h):
            raise ValueError("head no desciende de base (vuelta atrás o historia ajena)")
        shas = git.run("rev-list", "--reverse", f"{b}..{h}").decode().split()
        commits = [(sha, git.changes(sha)) for sha in shas]
        created = set()
        for sha, files in commits:
            for path, status, _ in files:
                if (MIGRATIONS.match(path) and path.endswith(".py") and "D" not in status
                        and not git.ok("cat-file", "-e", f"{b}:{path}")):
                    with contextlib.suppress(Unanalyzable):  # la que no se analiza ya es C
                        ops, _ = parse_migration(git.blob(sha, path))
                        created |= {(app_of(path), text(arg(c, "name", 0))) for op, c in ops if op == "CreateModel"}
        for sha, files in commits:
            entries = []
            for path, status, modes in files:
                reasons = file_reasons(git, rules, sha, b, path, status, modes, created)
                entries.append({"path": path, "status": status, "class": top(r["class"] for r in reasons),
                                "reasons": reasons})
            subject = git.run("log", "-1", "--format=%s", sha).decode().strip()
            report["commits"].append({"sha": sha, "subject": subject, "files": entries,
                                      "class": top(e["class"] for e in entries)})
    except Exception as exc:  # falla cerrado: cualquier error es C
        report["errors"].append(f"{type(exc).__name__}: {exc}")
    computed = "C" if report["errors"] else top(c["class"] for c in report["commits"])
    effective = top([computed, "C" if declared == "invalid" else declared or "none"])
    report.update(computed=computed, declared=declared, notes=[], **{"class": effective})
    if declared == "invalid":
        report["notes"].append("clase declarada no válida: se toma C")
    elif declared and RANK[declared] < RANK[computed]:
        report["notes"].append(f"declarada {declared} menor que la calculada {computed}: se ignora (ADR-048 §2)")
    report["decisive"] = [
        {"commit": c["sha"], "path": f["path"], **r}
        for c in report["commits"] for f in c["files"] for r in f["reasons"] if r["class"] == computed
    ]
    return report


def markdown(report: dict) -> str:
    cell = lambda s: html.escape(str(s)).replace("|", "\\|").replace("\n", " ")  # noqa: E731
    lines = [
        f"## Clase de despliegue: **{report['class']}**", "",
        f"Rango `({report['base'][:12]}, {report['head'][:12]}]`: {len(report['commits'])} commits. "
        f"Calculada **{report['computed']}**, declarada {report['declared'] or '—'}.",
        "Solo informativa en la fase 1 (ADR-048): no despliega, no aprueba y no bloquea nada.", "",
    ]
    lines += [f"- ❌ {cell(e)}" for e in report["errors"]] + [f"- ⚠️ {cell(n)}" for n in report["notes"]]
    lines += ["", f"### Por qué es {report['computed']}", ""]
    grouped: dict[tuple[str, str], list[str]] = {}
    for d in report["decisive"]:
        grouped.setdefault((d["commit"][:8], d["path"]), []).append(f"{d['why']} (`{d['rule']}`)")
    lines += [f"- `{cell(path)}` ({sha}): {cell('; '.join(whys))}"
              for (sha, path), whys in grouped.items()] or ["- (sin archivos)"]
    lines += ["", "<details><summary>Todos los archivos</summary>", "",
              "| Commit | Archivo | Estado | Clase | Motivos |", "| --- | --- | --- | --- | --- |"]
    for c in report["commits"]:
        for f in c["files"]:
            why = "; ".join(f"{r['class']}: {r['why']}" for r in f["reasons"])
            lines.append(f"| {c['sha'][:8]} | `{cell(f['path'])}` | {f['status']} | {f['class']} | {cell(why)} |")
    return "\n".join(lines + ["", "</details>", ""])


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--base", required=True, help="REVISION en producción (exclusiva)")
    p.add_argument("--head", required=True, help="SHA candidato (inclusivo)")
    p.add_argument("--repo", default=".")
    p.add_argument("--event", help="evento pull_request de GitHub: lee la clase declarada del cuerpo")
    p.add_argument("--json", help="escribe la evidencia JSON en este archivo")
    p.add_argument("--markdown", action="store_true", help="imprime el resumen legible")
    args = p.parse_args(argv)
    declared = None
    if args.event:
        try:
            with open(args.event, encoding="utf-8") as fh:
                declared = declared_class(((json.load(fh).get("pull_request") or {}).get("body")))
        except (OSError, ValueError, AttributeError):
            declared = "invalid"
    report = classify(args.repo, args.base, args.head, declared)
    if args.json:
        pathlib.Path(args.json).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(markdown(report) if args.markdown else json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
