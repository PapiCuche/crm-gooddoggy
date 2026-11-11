# ADR-030: Límite de tamaño de PR de 1500 líneas relevantes, con revisión adicional en rutas críticas

- **Status:** Proposed
- **Date:** 2026-11-10
- **Deciders:** propuesto por Claude Code (Sonnet 5.5) para la decisión del propietario. No lo decide el programa autónomo: cambia el límite que ADR-009 fija y ADR-015 §3 repite, y una política así no es una decisión técnica ordinaria de ADR-015 §5. Mientras esté `Proposed` no obliga al código ni a los agentes. La única decisión que necesita está en «Decisión requerida del propietario».
- **Related:** [ADR-009](ADR-009-git-strategy.md) (tamaño de los PR), [ADR-015](ADR-015-autonomous-delivery-program.md) (§2 gates y §3 límites del programa), [AGENTS.md](../../AGENTS.md) §4, §7 y §12, [delivery-automation.md](../architecture/delivery-automation.md) §4, `.github/scripts/pr_governance.py`, ADR-001, ADR-002, ADR-003, ADR-022

## Context

### Qué dicen las reglas hoy

- ADR-009 (Tamaño de los PR): objetivo ≤ 400 líneas cambiadas, más de 800 se divide. AGENTS.md §4 repite «≤ 400 relevantes; más de 800 falla en CI».
- `pr_governance.py` lo aplica: 401–800 pasa con aviso, más de 800 falla. Solo el mantenedor puede poner `large-pr-approved`, que convierte el fallo en aviso, y el programa autónomo no lo pone nunca (ADR-015 §3: «un cambio de más de 800 líneas relevantes se divide»).
- Las líneas relevantes son adiciones más borrados, sin `docs/**`, lockfiles, migraciones, `generated/**` ni `frontend/src/lib/api/**`.

### Por qué se propone cambiarlo

El límite de 800 ya no ordena el trabajo: lo **trocea**. Un work item cuyo diseño completo es coherente (una tabla con su RLS, sus servicios, su API y su pantalla) se parte donde cabe, no donde tiene sentido, y cada trozo paga el ciclo completo de entrega.

Casos recientes en `main` y en curso:

- **F7-04a / F7-04b**: la entrada de WhatsApp (contacto, conversación y mensaje en el Inbox) se partió en #608 (799 líneas) y #614 (376 líneas, abierto).
- **F5-07 / F5-07b**: el detalle de oportunidad se partió en #615 (660 líneas, abierto) y el issue #616 (editar y asignar desde el detalle).
- **F7-02a a F7-02e**: la cuenta de WhatsApp, 5 PR (536, 540, 654, 784 y 191 líneas; 2705 en total).
- **F7-03a a F7-03c**: el webhook público, 3 PR de 712, 791 y 731 líneas.
- **F3-48** (promociones): 10 PR; **F5-04** (oportunidades por API): 10 PR.

El coste de cada trozo extra no es el código sino el ciclo: crear y preparar el issue, la rama, la revisión adversarial independiente (gate 3), `make check` (gate 5), el CI del HEAD exacto (gate 6), la revisión del diff final (gate 8), el handoff y el comentario del merge. Es una estimación del programa de unos **15 minutos de gates por PR**; este ADR no la mide. Además, un diseño partido obliga a revisar un trozo sin ver el resto, que es un riesgo de revisión distinto del tamaño (una invariante repartida en dos PR se revisa dos veces a medias).

## Datos medidos

### Método

Muestra: los **150 últimos PR mergeados** (del 2026-10-22 al 2026-11-10, unos 7,5 por día), con

```bash
gh pr list --state merged --limit 150 \
  --json number,title,additions,deletions,changedFiles,mergedAt,files
```

Las líneas relevantes se calculan **con la misma regla que el script** (`is_excluded` de `pr_governance.py`, adiciones más borrados por archivo, sin los archivos excluidos). La lista de archivos de cada PR llegó completa (el máximo es 34). «Trozo» es un PR cuyo identificador de work item acaba en letra (`F7-04a`, `F5-05d`): es una aproximación, porque cuenta también correcciones de seguimiento y no solo cortes planeados.

### Distribución del tamaño

| Líneas relevantes | PR  | Porcentaje |
| ----------------- | --- | ---------- |
| 0 a 400           | 34  | 23 %       |
| 401 a 600         | 35  | 23 %       |
| 601 a 800         | 81  | 54 %       |
| 801 o más         | 0   | 0 %        |

- Mediana **627**, media **559**, percentil 90 **798**, máximo **800**. Suma: 83 789 líneas relevantes en 150 PR.
- **57 PR (38 %)** tienen 700 o más líneas; **43 (29 %)**, 750 o más; **28 (19 %)** están entre 790 y 800.
- Ninguno pasa de 800. La muestra está **censurada por el límite**: la distribución no muestra el tamaño natural del trabajo, solo cómo se recorta para caber. Sin contar las exclusiones, 57 PR superan las 800 líneas brutas y 2 superan las 1500: las exclusiones ya hacen su trabajo y no hace falta tocarlas.

### Trozos

- **79 de 150 PR (53 %)** son trozos (identificador con letra).
- **30 work items** se entregaron en dos o más PR: 103 PR en total, **73 PR extra** sobre un PR por work item.
- Las series enteras son grandes: F3-48 suma 6225 líneas en 10 PR, F5-04 4691 en 10, F5-05 3422 en 5, F5-02 3404 en 5, F7-02 2705 en 5. **Con un límite de 1500 siguen siendo 3 o 4 PR**: la propuesta no convierte una serie en un PR.

### Qué habría cambiado con otro límite

Simulación: se agrupan, en orden, los PR consecutivos de un mismo work item hasta llenar el límite, y se cuenta cuántos PR se ahorran.

| Límite | PR ahorrados de 150 | Gates ahorrados a 15 min |
| ------ | ------------------- | ------------------------ |
| 1200   | 29 (19 %)           | unas 7,3 h               |
| 1500   | 42 (28 %)           | unas 10,5 h              |
| 1800   | 55 (37 %)           | unas 13,8 h              |

Es una cota superior: supone que los trozos son contiguos y fusionables, e incluye correcciones de seguimiento que en la práctica no se fusionan con el PR anterior. Sirve para el orden de magnitud, no como compromiso.

### Rutas críticas

Con las rutas críticas de la propuesta (§ Decisión), **63 de los 150 PR (42 %)** tocan alguna, y **26 de los 57 PR de 700 o más líneas (46 %)**. Es decir, casi la mitad de los PR grandes pasaría por la revisión adicional.

## Decision

Esta es la propuesta. Nada de lo que sigue rige hasta que el propietario la acepte.

### 1. Tramos de tamaño

| Líneas relevantes | Resultado en `PR governance (trusted)`                                                                      |
| ----------------- | ----------------------------------------------------------------------------------------------------------- |
| hasta 800         | pasa, como hoy, sin aviso                                                                                   |
| 801 a 1000        | pasa; exige **plan de división** en el issue y, si toca una ruta crítica, **revisión adicional** (§ 3)      |
| 1001 a 1500       | pasa con **aviso**; mismas exigencias                                                                       |
| 1501 a 1800       | **falla**, salvo con `large-pr-approved` (solo el mantenedor), que lo convierte en aviso; mismas exigencias |
| más de 1800       | **falla siempre**, también con `large-pr-approved`                                                          |

- Los tramos son de línea relevante, con las mismas exclusiones de hoy. **No se añade ni se quita ninguna exclusión.** Nunca se excluye código de aplicación para entrar en el límite.
- El **objetivo de ≤ 400** de ADR-009 y AGENTS.md §4 se mantiene como guía de diseño («un PR, un objetivo verificable»). Deja de producir un aviso mecánico: hoy lo producen 116 de cada 150 PR y por eso nadie lo lee.
- `large-pr-approved` sigue siendo del mantenedor. Hoy permite cualquier tamaño; con esta propuesta tiene un **techo de 1800**. Es la única forma en que la propuesta _reduce_ una capacidad actual, y es deliberada.

### 2. Plan de división declarado en el issue

Cuando un PR supera 800 líneas relevantes, el work item que cierra debe tener un campo **«Plan de división»** con contenido propio: qué haría el siguiente corte si el PR se hubiera partido, y por qué no se parte (qué invariante o prueba se rompería). Va en el issue y no en el PR porque lo escribe quien planea el trabajo, antes de escribirlo. El script comprueba que el encabezado `### Plan de división` existe y no está vacío (ni es `_No response_`); **no juzga si el plan es bueno**. Eso es del revisor.

### 3. Revisión independiente adicional en rutas críticas

Si un PR de más de 800 líneas relevantes toca **cualquier** archivo (aunque esté excluido del conteo) de estas rutas, su cuerpo debe tener la sección `## Revisión adicional`:

| Ámbito                                   | Rutas                                                                                                                                                                          |
| ---------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Tenancy y RLS                            | `backend/core/tenancy/**`, `backend/core/db/**`, `**/migrations/**` (las políticas RLS viven en migraciones), `backend/apps/provisioning/**`                                   |
| Credenciales y secretos                  | `backend/core/sealed.py`, `backend/core/redaction.py`, `backend/apps/conversations/credentials.py`, `backend/apps/conversations/meta_webhook.py`, `backend/config/settings/**` |
| Seguridad (autenticación y autorización) | `backend/apps/accounts/**`, `backend/apps/access/**`, `backend/core/http.py` (cliente saliente con allowlist)                                                                  |
| Dinero                                   | `backend/core/money.py`, `backend/apps/pricing/**`                                                                                                                             |
| Gobernanza de entrega                    | `.github/workflows/**`, `.github/scripts/**`                                                                                                                                   |

La sección registra una **segunda revisión independiente**, distinta del autor _y_ de la revisión adversarial del gate 3 (otro revisor humano o una sesión de agente distinta, con las lentes del gate 3 de ADR-015 §2 restringidas a: aislamiento de tenant y RLS, credenciales y secretos, autorización, dinero), con sus hallazgos y su clasificación. El gate 3 y los otros nueve **no cambian**: la sección es una condición del check `PR governance (trusted)`, que el gate 6 (CI en verde en el HEAD exacto) ya exige. Como el script comprueba la forma y no la calidad, el revisor humano o el mantenedor siguen siendo quien decide si la revisión adicional es real.

La lista de rutas es una constante del script (`CRITICAL_PATTERNS`): ampliarla es un cambio de una línea con su test, sujeto a revisión. No es exhaustiva: si un PR de más de 800 líneas toca algo de seguridad fuera de la lista, el revisor puede exigir la sección igualmente.

### 4. Pruebas de mutación en rutas críticas

El repositorio ya practica la mutación a mano (p. ej. el issue F7-03c, #600, pide «tests … con mutación»: se rompe a propósito el código para comprobar que un test falla). La propuesta pide que, en la sección `## Revisión adicional`, el autor declare qué mutaciones probó sobre el código crítico del PR y que cada una hizo fallar un test. Una herramienta automática de mutación (p. ej. `mutmut`) sería una dependencia nueva fuera de ADR-012 y **no forma parte de este ADR**: si se quiere, es un work item propio con su ADR.

### 5. Lo que no cambia

- Los **diez gates** de ADR-015 §2: ni su texto ni su orden ni su umbral.
- Las protecciones de `main`, el ruleset y los checks requeridos.
- 1 issue = 1 rama = 1 PR, las secciones obligatorias del PR y la exclusión de `dependabot[bot]`.
- Las exclusiones del conteo.
- Que el programa autónomo **nunca** aplica `large-pr-approved`: dentro del programa el límite efectivo es 1500, no 1800.
- La vida de una rama (≤ 3 días hábiles) y los feature flags para integrar funcionalidad incompleta.

### 6. Qué enmienda y cómo

Siguiendo el precedente de ADR-015, ADR-009 y ADR-015 **no se editan**: este ADR, una vez aceptado, los enmienda en un punto cada uno y el índice de `docs/adr/README.md` lo indica.

- **ADR-009, «Tamaño de los PR»:** «Más de 800 → dividir» pasa a «más de 1500 → dividir; hasta 1800 solo con `large-pr-approved`». El objetivo de 400 y el resto de esa sección siguen.
- **ADR-015 §3:** «un cambio de más de 800 líneas relevantes se divide» pasa a «de más de 1500». El programa sigue sin poder aplicar la etiqueta.

## Cambio exacto propuesto (NO aplicado en este PR)

Este PR es solo documentación. Si el propietario aprueba (opción A o B), un work item posterior aplica lo que sigue. El script lo evalúa la versión de `main`, no la del PR (workflow `pr-governance-trusted`): el PR que lo aplique se evalúa todavía con el límite de 800 y, al ser de menos de 250 líneas relevantes, pasa.

### `.github/scripts/pr_governance.py`

El diff se generó contra el archivo de `main` (138dfad) y se ejecutó con sus pruebas en una copia fuera del repositorio.

```diff
--- a/.github/scripts/pr_governance.py
+++ b/.github/scripts/pr_governance.py
@@ -1,4 +1,4 @@
-"""PR governance checks (ADR-009, docs/architecture/delivery-automation.md).
+"""PR governance checks (ADR-009, ADR-030, docs/architecture/delivery-automation.md).

 Validates structure (title, branch, linked issue, required sections, size) and that the
 linked issue is a real work item in a workable state whose declared branch matches the PR.
@@ -47,9 +47,35 @@
     "generated/**",
     "frontend/src/lib/api/**",
 )
-WARN_LINES = 400
-FAIL_LINES = 800
+# Tamaño (ADR-030): aviso desde WARN_LINES; falla por encima de FAIL_LINES salvo con el label del
+# mantenedor, que solo vale hasta EXCEPTION_LINES; por encima de EXCEPTION_LINES siempre falla.
+WARN_LINES = 1000
+FAIL_LINES = 1500
+EXCEPTION_LINES = 1800
 OVERRIDE_LABEL = "large-pr-approved"
+# Por encima de EXTENDED_LINES (el antiguo límite de ADR-009) el work item declara cómo se dividiría
+# y, si el PR toca una ruta crítica, el PR registra una revisión independiente adicional.
+EXTENDED_LINES = 800
+SPLIT_PLAN_HEADING = "Plan de división"
+EXTRA_REVIEW_SECTION = "Revisión adicional"
+CRITICAL_PATTERNS = (
+    "backend/core/tenancy/**",
+    "backend/core/db/**",
+    "**/migrations/**",
+    "backend/core/sealed.py",
+    "backend/core/money.py",
+    "backend/core/redaction.py",
+    "backend/core/http.py",
+    "backend/config/settings/**",
+    "backend/apps/access/**",
+    "backend/apps/accounts/**",
+    "backend/apps/pricing/**",
+    "backend/apps/provisioning/**",
+    "backend/apps/conversations/credentials.py",
+    "backend/apps/conversations/meta_webhook.py",
+    ".github/workflows/**",
+    ".github/scripts/**",
+)
 EXEMPT_AUTHORS = ("dependabot[bot]",)


@@ -69,6 +95,7 @@


 _EXCLUDED = [(("/" in p), _glob_to_regex(p)) for p in EXCLUDED_PATTERNS]
+_CRITICAL = [_glob_to_regex(p) for p in CRITICAL_PATTERNS]


 def is_excluded(path: str) -> bool:
@@ -77,6 +104,18 @@
     return any(rx.match(path if has_slash else name) for has_slash, rx in _EXCLUDED)


+def is_critical(path: str) -> bool:
+    """Ruta de seguridad, tenancy, RLS, credenciales o dinero (ADR-030). Cuenta también lo excluido del tamaño."""
+    return any(rx.match(path) for rx in _CRITICAL)
+
+
+def has_section(body: str, heading: str, level: int) -> bool:
+    """Hay un encabezado de ese nivel con contenido propio (no vacío, no "_No response_", no solo comentarios)."""
+    text = re.sub(r"<!--.*?-->", "", body or "", flags=re.DOTALL)
+    m = re.search(rf"^{'#' * level}\s+{re.escape(heading)}\s*$\s*^(?!#)(\S.*?)\s*$", text, re.MULTILINE)
+    return bool(m) and m.group(1) not in {"_No response_", "N/A", "-", "—"}
+
+
 @dataclass
 class Result:
     errors: list[str] = field(default_factory=list)
@@ -156,14 +195,30 @@
             res.relevant_lines += lines

     n = res.relevant_lines
-    if n > FAIL_LINES:
-        msg = f"PR de {n} líneas relevantes (> {FAIL_LINES}): dividirlo (ADR-009)"
+    if n > EXCEPTION_LINES:
+        res.errors.append(f"PR de {n} líneas relevantes (> {EXCEPTION_LINES}): dividirlo; ni el label lo permite (ADR-030)")
+    elif n > FAIL_LINES:
+        msg = f"PR de {n} líneas relevantes (> {FAIL_LINES}): dividirlo (ADR-030)"
         if OVERRIDE_LABEL in labels:
-            res.warnings.append(msg + f" — permitido por el label '{OVERRIDE_LABEL}'")
+            res.warnings.append(msg + f" — permitido por el label '{OVERRIDE_LABEL}' hasta {EXCEPTION_LINES}")
         else:
             res.errors.append(msg)
     elif n > WARN_LINES:
-        res.warnings.append(f"PR de {n} líneas relevantes (> {WARN_LINES}): objetivo de ADR-009 superado")
+        res.warnings.append(f"PR de {n} líneas relevantes (> {WARN_LINES}): cerca del límite de {FAIL_LINES} (ADR-030)")
+
+    if n > EXTENDED_LINES:
+        issue = issues.get(numbers[0]) if len(numbers) == 1 else None
+        if issue is not None and not has_section(issue.get("body") or "", SPLIT_PLAN_HEADING, 3):
+            res.errors.append(
+                f"PR de {n} líneas relevantes (> {EXTENDED_LINES}): el issue #{numbers[0]} debe declarar "
+                f"'### {SPLIT_PLAN_HEADING}' (ADR-030)"
+            )
+        critical = sorted(f["filename"] for f in files if is_critical(f.get("filename", "")))
+        if critical and not has_section(body, EXTRA_REVIEW_SECTION, 2):
+            res.errors.append(
+                f"PR de {n} líneas relevantes (> {EXTENDED_LINES}) que toca rutas críticas ({critical[0]}"
+                f"{' y otras' if len(critical) > 1 else ''}): falta la sección '## {EXTRA_REVIEW_SECTION}' (ADR-030)"
+            )
     return res


```

### `.github/scripts/test_pr_governance.py`

38 pruebas, las 31 de hoy más las nuevas; las dos de tamaño que existían se reescriben porque el límite cambia. Cinco mutaciones del script (límite 1500 vuelto a 800, techo 1800 quitado, comprobación de rutas críticas apagada, aviso vuelto a 400, plan de división apagado) hacen fallar la suite.

```diff
--- a/.github/scripts/test_pr_governance.py
+++ b/.github/scripts/test_pr_governance.py
@@ -1,13 +1,26 @@
 import unittest

-from pr_governance import REQUIRED_SECTIONS, declared_branch, evaluate, is_excluded, linked_issues
+from pr_governance import (
+    REQUIRED_SECTIONS,
+    declared_branch,
+    evaluate,
+    has_section,
+    is_critical,
+    is_excluded,
+    linked_issues,
+)

 BRANCH = "feature/f1-backend-skeleton"
 BODY = "Closes #4\n\n" + "\n".join(f"## {s}\n\ntexto\n" for s in REQUIRED_SECTIONS)


-def issue(labels=("work-item", "status:ready"), branch=BRANCH):
-    body = f"### ID / Fase\n\nF1-02\n\n### Rama\n\n`{branch}`\n\n### Objetivo\n\nx\n"
+PLAN = "### Plan de división\n\nF1-02b añadiría la pantalla.\n\n"
+EXTRA = "\n## Revisión adicional\n\nRevisor distinto, lentes de tenancy y RLS: sin hallazgos.\n"
+
+
+def issue(labels=("work-item", "status:ready"), branch=BRANCH, plan=False):
+    body = (f"### ID / Fase\n\nF1-02\n\n### Rama\n\n`{branch}`\n\n{PLAN if plan else ''}"
+            "### Objetivo\n\nx\n")
     return {"number": 4, "body": body, "labels": [{"name": n} for n in labels]}


@@ -66,16 +79,75 @@
         self.assertIn("Falta la sección obligatoria '## No incluye'", res.errors)

     def test_size_thresholds(self):
-        self.assertEqual(run(f=files(400)).warnings, [])
-        res = run(f=files(401))
+        planned = {4: issue(plan=True)}
+        self.assertEqual(run(f=files(800)).warnings, [])
+        res = run(f=files(801), issues=planned)  # ya no falla: 801 es solo tamaño ampliado
+        self.assertEqual((res.errors, res.warnings), ([], []))
+        self.assertEqual(run(f=files(1000), issues=planned).warnings, [])
+        res = run(f=files(1001), issues=planned)
         self.assertEqual((len(res.errors), len(res.warnings)), (0, 1))
-        res = run(f=files(801))
+        res = run(f=files(1500), issues=planned)
+        self.assertEqual((len(res.errors), len(res.warnings)), (0, 1))
+        res = run(f=files(1501), issues=planned)
         self.assertEqual((len(res.errors), len(res.warnings)), (1, 0))

-    def test_override_label_turns_fail_into_warning(self):
-        res = run(pr(labels=("large-pr-approved",)), files(900))
+    def test_override_label_turns_fail_into_warning_up_to_exception_ceiling(self):
+        planned = {4: issue(plan=True)}
+        res = run(pr(labels=("large-pr-approved",)), files(1501), planned)
         self.assertEqual((len(res.errors), len(res.warnings)), (0, 1))
+        res = run(pr(labels=("large-pr-approved",)), files(1800), planned)
+        self.assertEqual((len(res.errors), len(res.warnings)), (0, 1))
+        res = run(pr(labels=("large-pr-approved",)), files(1801), planned)
+        self.assertEqual((len(res.errors), len(res.warnings)), (1, 0))
+        self.assertIn("ni el label lo permite", res.errors[0])

+    def test_extended_size_needs_split_plan_in_the_issue(self):
+        self.assertEqual(run(f=files(800)).errors, [])
+        res = run(f=files(801))
+        self.assertTrue(any("'### Plan de división'" in e for e in res.errors), res.errors)
+        self.assertEqual(run(f=files(801), issues={4: issue(plan=True)}).errors, [])
+
+    def test_extended_size_with_missing_issue_does_not_duplicate_the_error(self):
+        res = run(f=files(900), issues={4: None})
+        self.assertEqual(len(res.errors), 1)
+        self.assertIn("no existe", res.errors[0])
+
+    def test_critical_paths_in_extended_pr_need_extra_review_section(self):
+        planned = {4: issue(plan=True)}
+        crit = files(900, "backend/core/tenancy/scope.py")
+        res = run(f=crit, issues=planned)
+        self.assertTrue(any("## Revisión adicional" in e for e in res.errors), res.errors)
+        self.assertEqual(run(pr(body=BODY + EXTRA), crit, planned).errors, [])
+        self.assertEqual(run(f=files(900), issues=planned).errors, [])  # sin ruta crítica
+
+    def test_critical_paths_below_extended_size_need_nothing_extra(self):
+        self.assertEqual(run(f=files(800, "backend/core/tenancy/scope.py")).errors, [])
+
+    def test_migration_counts_as_critical_even_if_excluded_from_size(self):
+        f = files(900) + files(30, "backend/apps/deals/migrations/0007_rls.py")
+        res = run(f=f, issues={4: issue(plan=True)})
+        self.assertEqual(res.relevant_lines, 900)
+        self.assertTrue(any("## Revisión adicional" in e for e in res.errors), res.errors)
+
+    def test_empty_placeholder_or_comment_sections_do_not_count(self):
+        self.assertFalse(has_section("### Plan de división\n\n_No response_\n", "Plan de división", 3))
+        self.assertFalse(has_section("### Plan de división\n\n### Objetivo\nx", "Plan de división", 3))
+        self.assertFalse(has_section("## Revisión adicional\n\n<!-- pendiente -->\n", "Revisión adicional", 2))
+        self.assertFalse(has_section("## Revisión adicional\n", "Revisión adicional", 2))
+        self.assertTrue(has_section("### Plan de división\r\n\r\nF1-02b\r\n", "Plan de división", 3))
+        self.assertTrue(has_section("## Revisión adicional\n\n<!-- x -->\nHecha.\n", "Revisión adicional", 2))
+
+    def test_critical_paths(self):
+        for p in ("backend/core/tenancy/scope.py", "backend/core/db/guards.py", "backend/core/sealed.py",
+                  "backend/core/money.py", "backend/apps/pricing/services.py", "backend/apps/access/catalog.py",
+                  "backend/apps/accounts/mfa.py", "backend/apps/deals/migrations/0007_rls.py",
+                  "backend/apps/conversations/credentials.py", ".github/workflows/backend.yml",
+                  ".github/scripts/pr_governance.py"):
+            self.assertTrue(is_critical(p), p)
+        for p in ("backend/apps/deals/services.py", "frontend/src/app/page.tsx", "docs/adr/ADR-030.md",
+                  "backend/apps/conversations/inbound.py", "AGENTS.md"):
+            self.assertFalse(is_critical(p), p)
+
     def test_excluded_paths(self):
         for p in ("docs/adr/ADR-013.md", "backend/uv.lock", "frontend/pnpm-lock.yaml", "package-lock.json",
                   "backend/apps/contacts/migrations/0001_initial.py", "generated/schema.ts",
```

### Otros cambios para el mismo work item (sin texto exacto aquí)

- `.github/ISSUE_TEMPLATE/work-item.yml`: campo opcional `Plan de división` (`textarea`, `required: false`), que GitHub vuelca como `### Plan de división`.
- `.github/pull_request_template.md`: comentario en el encabezado de tamaño que recuerda `## Revisión adicional` para PR de más de 800 líneas que tocan rutas críticas (sin hacerla obligatoria para todos: el check solo la exige cuando corresponde).
- `AGENTS.md` §4 y §12, y `docs/architecture/delivery-automation.md` §4 (la fila «Tamaño»): los números nuevos y la mención de este ADR. En §12, «un cambio de más de 800 líneas se divide» pasa a «de más de 1500».
- `docs/adr/README.md`: ADR-009 y ADR-015 pasan a «Accepted (enmendado en parte por ADR-030)»; ADR-030, a `Accepted`.

## Alternatives considered

| Opción                                                    | A favor                                                                                                                                                                                | En contra                                                                                                                                                                   |
| --------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **1. Mantener 800**                                       | Cero cambios; revisión más corta por PR                                                                                                                                                | 54 % de los PR en 601–800 y 19 % pegados al techo: el límite ya recorta el diseño. Trozos del 53 %; 73 PR extra, unas 18 h de gates en 20 días a 15 min cada uno            |
| **2. Subir a 1200**                                       | Cambio moderado; la mayoría de los PR de ~700 se fusiona con el siguiente                                                                                                              | Ahorra 29 PR (19 %): poco. Deja fuera el caso típico de un slice vertical con tabla, RLS, API y pantalla (series de 2000–3400 líneas); el techo vuelve a ser un muro a 1200 |
| **3. 1500 normal, excepción hasta 1800 (esta propuesta)** | Ahorra hasta 42 PR (28 %) con 1500 y 55 con 1800; cabe un slice completo pequeño; el techo de 1800 no deja PR ilimitados; el aviso a 1000 y la revisión adicional frenan lo arriesgado | Más líneas por revisión: riesgo de fatiga (ver Riesgos). Más código en el script (de 235 a 290 líneas)                                                                      |
| 4. Sin límite, solo `large-pr-approved`                   | Máxima flexibilidad                                                                                                                                                                    | Quita la regla estructural y deja el tamaño al criterio de quien pone la etiqueta; el programa no podría usarla                                                             |
| 5. Límite por número de archivos                          | Mide la dispersión, no el volumen                                                                                                                                                      | Cuenta mal los generados, las pruebas y los renombrados; no se parece a lo que revisa una persona                                                                           |

## Recomendación

La **opción 3**: 1500 líneas relevantes como límite normal, aviso en 1000, excepción justificada del mantenedor hasta 1800, plan de división en el issue desde 800, y revisión independiente adicional en las rutas críticas desde 800. Es la que más reduce el coste de entrega sin quitar ningún control: los diez gates y las exclusiones no cambian, y el único gasto nuevo de revisión se paga donde hay más riesgo (el 46 % de los PR grandes).

Si el propietario prefiere ir más despacio, la alternativa razonable no es la opción 2 sino la 3 con otros números (opción B): por ejemplo 1200 normal y 1500 de excepción, con las mismas exigencias.

## Riesgos y mitigaciones

| Riesgo                                                                        | Mitigación                                                                                                                                                                                               |
| ----------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Fatiga del revisor**: más líneas por revisión, más hallazgos que se escapan | Revisión adicional obligatoria en rutas críticas desde 800 (§ 3); plan de división declarado en el issue (§ 2); aviso desde 1000; techo duro de 1800; el programa nunca usa la excepción (efectivo 1500) |
| Un PR grande esconde un cambio de seguridad entre pruebas y pantallas         | La revisión adicional se pide por **ruta**, no por el parecer del autor; incluye migraciones aunque estén excluidas del conteo                                                                           |
| El plan de división o la sección de revisión se rellenan por cumplir          | El script solo mira la forma, y esto es una limitación declarada; el gate 3, el gate 8 y el mantenedor juzgan el fondo. Se mide en la revisión posterior (§ Reversión)                                   |
| Menos mutación real al crecer el PR                                           | El autor declara qué mutaciones probó en la zona crítica (§ 4); una herramienta automática es un work item aparte                                                                                        |
| PR grandes más difíciles de revertir                                          | Squash merge, un PR = un commit: revertir sigue siendo un solo `git revert`. Las migraciones siguen yendo con el código que las usa                                                                      |
| CI más lento por PR (el job de tests del backend ya tuvo que subir a 30 min)  | No cambia el coste total: el mismo código se prueba una vez en lugar de dos. Se vigila con CI-PERF-01 (#617)                                                                                             |
| Se reabre la tentación de «meter todo en un PR»                               | El techo de 1800 es duro; 1 issue = 1 rama = 1 PR sigue; el objetivo de ≤ 400 sigue como guía                                                                                                            |

## Plan de reversión

1. **Reversión técnica**: un PR que revierta el work item de aplicación. Devuelve `pr_governance.py` y sus pruebas a 400/800 (aviso/fallo) y quita las exigencias de plan de división y revisión adicional. Como el script lo evalúa la versión de `main`, tiene efecto en cuanto se mergea. Un PR abierto en ese momento con más de 800 líneas pasa a fallar y se divide, o el mantenedor le pone `large-pr-approved` (sin techo, como hoy).
2. **Reversión de los textos**: el mismo PR deja AGENTS.md, delivery-automation.md y el índice como antes, y marca ADR-030 como `Deprecated` con el motivo. Si el ADR ya era `Accepted`, ADR-009 manda: no se edita, se escribe un ADR nuevo que lo sustituye.
3. **Cuándo revertir** (revisión programada a los 30 días o a los 30 PR de más de 800 líneas, lo que ocurra antes; el programa la prepara y el propietario la decide): se mide el porcentaje de PR de más de 800 líneas que necesitaron un PR `fix` sobre los mismos archivos en los 7 días siguientes, y los hallazgos `FIX NOW` de la revisión adversarial por cada 1000 líneas. Propuesta de umbral, que el propietario puede cambiar: **revertir si el primer porcentaje es el doble que el de los PR de 800 o menos**, o ante **cualquier incidente de seguridad o de tenancy** atribuible a un PR de más de 800 líneas.
4. Revertir no cuesta datos: el cambio es solo de proceso y de un script. Ningún dato ni esquema depende de él.

## Consequences

- Los work items con un diseño coherente de hasta 1500 líneas se entregan en un PR. Según la simulación, hasta el 28 % de los PR dejaría de existir.
- ADR-009 y ADR-015 siguen sin editarse; el índice indica la enmienda.
- Los PR de más de 800 líneas llevan más trabajo de preparación (plan de división) y, en rutas críticas, de revisión. Es un coste que hoy no existe porque esos PR no existen.
- El script de gobernanza crece (de 235 a 290 líneas) y sus pruebas, de 163 a 235. Sigue siendo solo biblioteca estándar y sigue comprobando forma y no calidad.
- Se pierde una capacidad actual del mantenedor: `large-pr-approved` ya no sirve por encima de 1800.

## Security implications

- No se debilita ningún control de ADR-001, ADR-002, ADR-003, ADR-013 o ADR-014 (ADR-015 §3 exige que un cambio así sea un ADR `Proposed` que espere al mantenedor: este lo es). No cambian la protección de `main`, el ruleset, los checks requeridos ni los diez gates.
- La propuesta **añade** una revisión independiente en las rutas donde está la superficie sensible (tenancy, RLS, credenciales, autorización, dinero) justo en los PR donde más difícil es revisar.
- Riesgo residual: el check verifica la presencia de la revisión adicional, no su contenido. Hay que decirlo claro: es una barrera de proceso, no una prueba.
- El script se evalúa desde `main` (ADR-009 / A-02): un PR no puede relajar su propia gobernanza, y este cambio tampoco, hasta que se mergee tras revisión. `.github/scripts/**` y `.github/workflows/**` son rutas críticas de la propia propuesta.

## Operational implications

- Se necesita un work item posterior (§ Cambio exacto) de menos de 250 líneas relevantes para aplicar la propuesta; no es parte de este PR.
- La revisión de los 30 días necesita un conteo reproducible; el comando de «Método» y el script lo dan.
- Los issues en curso que acaben en un PR de más de 800 líneas deben ganar el campo «Plan de división» antes del PR; los de 800 o menos no cambian.

## Decisión requerida del propietario

Se pide **una sola decisión**. Mientras no se dé, el ADR sigue `Proposed` y rige 800, con el mismo comportamiento de hoy.

| Opción | Decisión                                                                                                                                        | Qué ocurre después                                                                                                                                                                                    |
| ------ | ----------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **A**  | Aprobar **tal como está propuesto**: aviso 1000, límite 1500, excepción hasta 1800, plan de división y revisión adicional desde 800             | ADR-030 pasa a `Accepted` (lo hace el propietario o un agente por su indicación explícita en la sesión). Se crea el work item que aplica el diff de este ADR. Hasta que se mergee, sigue rigiendo 800 |
| **B**  | Aprobar **con otros números**: el propietario indica aviso, límite normal, techo de excepción y desde qué tamaño se exige la revisión adicional | Se reescribe este ADR con esos números (un commit en este PR, o un PR nuevo), se acepta y se aplica como en A. Nada se mergea con números no confirmados                                              |
| **C**  | **Rechazar**: se mantiene 800                                                                                                                   | ADR-030 pasa a `Deprecated` con el motivo, sin cambios en el script ni en los demás textos. Los agentes siguen dividiendo a 800                                                                       |

Respuesta esperada, en una línea: `A`, `B` con los cuatro números, o `C`.

## Cómo se reprodujo el dato

1. `gh pr list --state merged --limit 150 --json number,title,additions,deletions,changedFiles,mergedAt,files` (el 2026-11-10, con `main` en 138dfad).
2. Para cada PR: suma de `additions + deletions` de sus archivos que no cumplen `is_excluded` de `.github/scripts/pr_governance.py`.
3. Trozos: identificador de work item del final del título, con letra (`F7-04a`); se agrupan por identificador sin la letra.
4. Rutas críticas: `is_critical` del diff propuesto, sobre las rutas de todos los archivos del PR.
5. Simulación: dentro de cada work item de dos o más PR, se llena un PR hasta el límite en el orden en que se mergearon.
