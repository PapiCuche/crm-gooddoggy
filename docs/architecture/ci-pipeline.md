# Pipeline de CI

**Relacionado:** ADR-009, [repository-structure.md](repository-structure.md)
**Principio:** una herramienta por necesidad; sin solapes.

---

## 1. Workflows

| Workflow | Disparo | Estado |
|---|---|---|
| `security.yml` | PR + push a `main` + semanal | **Activo desde la Fase 0.5** |
| `pr-governance-trusted.yml` (check `PR governance (trusted)`) | `pull_request_target` | **Activo desde A-02**: frontera de governance; código trusted de `main`, solo lectura ([delivery-automation.md](delivery-automation.md) §4–§5) |
| `pr-governance.yml` (check `PR governance`, legacy) | `pull_request` | Solo informativo tras A-02; se retira según OBS-A-02-1 |
| `work-item-state.yml` | `pull_request_target` (abierto, ready, cerrado) | Trusted desde A-02: checkout solo de `main` y `work_item_state.py`; muta los labels `status:*` solo si el work item corresponde exactamente al PR ([delivery-automation.md](delivery-automation.md) §2) |
| `work-item-dependencies.yml` | `issues` (closed, edited, labeled) + `workflow_dispatch` | **Activo desde A-04 (trusted):** desbloquea `status:blocked` → `status:ready` con dependencias CLOSED y gates presentes en el ruleset; comenta en el issue y en el maestro |
| `backend.yml` | **Todos** los PRs (sin `paths`) + push a `main` con cambios en `backend/**` o en el workflow | **Activo desde F1-02; gate estable desde A-03:**
  - `backend changes`: detector;
  - `backend checks`, `backend tests` y `backend docker build`: jobs de implementación, que solo corren si el PR toca el backend. `backend tests` es una matriz de 4 shards (`backend tests (shard N/4)`, CI-PERF-01): cada shard es una VM con su propio PostgreSQL 18.6 (roles `crm_migrator`/`crm_app`, RLS) y su Garage, ejecuta solo sus archivos (reparto determinista por peso, `.github/scripts/shard_backend.py` y `backend_shard_weights.json`) y comprueba por JUnit que ejecutó exactamente lo planeado; la suma de los shards es la suite completa. Si se añaden archivos de test, el peso se estima por número de tests; los pesos se regeneran con las líneas `SECONDS` del log de un run;
  - `backend gate`: siempre presente; es el candidato a check requerido. |
| `frontend.yml` | PR/push con cambios en `frontend/**` | Se crea en la Fase 1 (cuando exista `package.json`) |

Se usan `paths` filtros para no ejecutar el pipeline del backend cuando solo cambia el frontend, y viceversa. Los checks requeridos de `main` se añaden a medida que existen.

## 2. Backend (`backend.yml`, Fase 1)

| Paso | Herramienta | Comando | Motivo |
|---|---|---|---|
| Entorno | `uv` | `uv sync --frozen` | Reproducible con lockfile |
| Formato | **ruff format** | `ruff format --check .` | Un solo tool para formato y lint (sustituye a black + isort + flake8) |
| Lint | **ruff** | `ruff check .` | Incluye reglas de seguridad (`S`, derivadas de bandit) |
| Tipos | **mypy** + django-stubs | `mypy .` | Tipado fuerte requerido |
| Arquitectura | **import-linter** | `lint-imports` | Fronteras de módulos |
| Migraciones | Django | `manage.py makemigrations --check --dry-run` | Modelos y migraciones sincronizados |
| Migraciones aplicables | Django | `manage.py migrate` (con `crm_migrator`) sobre un Postgres 18 limpio | Detecta migraciones rotas |
| Tests | **pytest** + pytest-django | `pytest --cov` con Postgres 18 (service) y Redis; conexión de tests con el rol `crm_app` | Incluye la suite de aislamiento de tenant y los tests de RLS (gate) |
| Dependencias | **pip-audit** | `pip-audit` sobre el lock exportado | Vulnerabilidades conocidas |
| Contrato API | drf-spectacular | `manage.py spectacular --validate --fail-on-warn` + diff contra el schema commiteado | El cliente TS generado no queda desactualizado |

**Estado en F1-02** (jobs `backend checks`, `backend tests` y `backend docker build`):
- **Activos:** entorno, formato, lint, tipos, import-linter, `check` y `check --deploy`, migraciones sincronizadas, pip-audit (lock exportado con hashes, todos los grupos), pytest contra `postgres:18.6`, y docker build con smoke test de `/health/live` y usuario no root.
- **Pendientes:** "migraciones aplicables con `crm_migrator`" y "tests con `crm_app`" llegan en F1-03 (hoy el servicio usa un rol de test); Redis y `--cov`, cuando haya código que lo justifique.
- **Contrato API (F1-08A):** el paso "Contrato OpenAPI sin drift" de `backend checks` ejecuta `spectacular --validate --fail-on-warn` y hace `diff -u` contra `backend/openapi/schema.yaml`. `tests/test_openapi.py` repite la comparación y comprueba el determinismo.
- **Instalación de uv:** binario oficial fijado por versión y verificado por SHA-256, sin acciones de terceros.

## 3. Frontend (`frontend.yml`, Fase 1)

| Paso | Herramienta | Comando |
|---|---|---|
| Entorno | pnpm | `pnpm install --frozen-lockfile` |
| Formato | **Prettier** | `pnpm format:check` |
| Lint | **ESLint** (config de Next + reglas propias) | `pnpm lint` |
| Tipos | **tsc** | `pnpm typecheck` (`tsc --noEmit`) |
| Tests | **Vitest** + Testing Library | `pnpm test --run` |
| Build | Next.js | `pnpm build` |
| Cliente API | orval | `pnpm api:generate` + `git diff --exit-code` (el cliente commiteado coincide con el schema) |
| Dependencias | pnpm | `pnpm audit --prod --audit-level=high` |

*Se valoró Biome (formato + lint en una herramienta), pero todavía no cubre las reglas específicas de Next.js y React Hooks con la misma madurez que ESLint. Se reevaluará.*

## 4. Seguridad (`security.yml`, activo)

| Control | Herramienta | Nota |
|---|---|---|
| Secret scanning | **gitleaks** (binario fijado por versión y verificado por SHA-256) | Escanea el historial completo en cada PR y push; `.gitleaks.toml` usa las reglas por defecto **sin allowlists**. Una excepción futura debe ser por regla y patrón exacto, nunca por archivo |
| Dependencias | **Dependabot** (alertas + PRs de actualización) | `github-actions` desde ya; `uv`/`pip` y `npm` al crear los manifiestos. En los pipelines: `pip-audit` y `pnpm audit` |
| Acciones de terceros | Fijadas por **SHA de commit** | Evita ataques a la cadena de suministro vía tags movidos |
| Permisos del token | `permissions: contents: read` por defecto | Mínimo privilegio |

**Política de triggers:** los workflows que ejecutan código del PR usan `pull_request`. Los workflows trusted de metadata/governance, que no hacen checkout ni ejecutan código del PR, pueden usar `pull_request_target` con revisión específica y permisos mínimos ([delivery-automation.md](delivery-automation.md) §5; endurecimiento en A-02, #15).

**No se añaden (redundantes o no aplicables ahora):** CodeQL (requiere GitHub Advanced Security en repositorios privados), escaneo de contenedores (cuando haya imágenes publicadas), SAST adicional (ruff `S` cubre lo básico en Python).

## 5. Local

- `pre-commit` (opcional, recomendado): ruff, prettier, gitleaks `protect --staged`. Mismas versiones que CI.
- `make check` (Fase 1) ejecuta localmente lo mismo que CI.

## 6. Checks requeridos para merge

Configurados en el ruleset `main-protection` (ver ADR-009):

- **Requeridos hoy:** `secret scanning (gitleaks)` y `PR governance (trusted)`. El legacy `PR governance` ya no es requerido y solo informa (A-02).
- **Candidato a requerido:** **`backend gate`** (A-03). Se añade al ruleset solo después del merge de A-03, tras comprobar un check real con ese nombre y leer su integration ID.
- **No se requieren individualmente:** `backend checks`, `backend tests` y `backend docker build` son jobs de implementación. Con filtros de rutas no siempre existen, y el gate ya los agrega.
- **Futuro:** un gate equivalente para el frontend (F1-09).

**Lógica de `backend gate`** (`.github/scripts/backend_gate.py`, con tests):
- **Detector (`backend changes`):**
  - en PRs, lista los archivos por API (`pull-requests: read`), considerando también el nombre anterior de los renombrados;
  - las rutas relevantes son `backend/**` y `.github/workflows/backend.yml`;
  - si la lista puede venir truncada (3000 archivos o más), asume que hay cambios (fail-safe);
  - en push a `main`, asume que hay cambios, porque el trigger ya filtra por rutas.
- **Evaluación con estados exactos:**
  - el detector debe estar en `success`; si no → FAIL;
  - con `backend_changed=true`, los tres jobs deben estar en `success`; `failure`, `cancelled` o `skipped` → FAIL;
  - con `backend_changed=false`, los tres deben estar en `skipped` → PASS (no-op);
  - cualquier otro valor → FAIL.
- **Límite conocido:** con `pull_request`, un PR puede modificar `backend.yml` o `backend_gate.py`. Esos cambios requieren revisión humana específica (misma naturaleza que OBS-A-02-2).
