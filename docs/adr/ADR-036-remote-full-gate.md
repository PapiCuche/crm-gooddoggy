# ADR-036: Gate completo en un runner remoto reproducible, con la evidencia atada al SHA exacto

- **Status:** Proposed
- **Date:** 2026-11-13
- **Deciders:** propuesto por el programa autónomo para la decisión del propietario. **No lo decide el programa:** cambia la forma de cumplir el gate 5 de [ADR-015](ADR-015-autonomous-delivery-program.md) §2, y ADR-015 §3 prohíbe al programa cambiar sus propios gates. Mientras esté `Proposed` no obliga a nada: el gate 5 sigue siendo `make check` en local. La única decisión que necesita está en «Decisión requerida del propietario».
- **Work item:** F1-19 (#792). Este PR solo archiva el ADR: no crea ni cambia workflows, rulesets ni ADR-015.
- **Related:** [ADR-015](ADR-015-autonomous-delivery-program.md) (§2 gates 2, 5 y 6; §3), [ADR-030](ADR-030-pr-size-limit.md) (Proposed: tamaño de los PR; **misma familia de gobernanza, no se duplica**), [ADR-009](ADR-009-git-strategy.md) (protección de `main`), [ADR-012](ADR-012-engineering-runtime-baseline.md) (versiones), [ci-pipeline.md](../architecture/ci-pipeline.md), [delivery-automation.md](../architecture/delivery-automation.md), `Makefile` (`make check`), `.github/workflows/*`

## Context

### Qué es hoy el gate 5

ADR-015 §2 exige diez gates antes del squash merge. El gate 5 dice: «La validación completa pasa en local (`make check`)». El gate 6: «El CI del HEAD exacto está en verde».

El gate 5 lo ejecuta hoy un script del programa, fuera del repositorio, en el Mac del mantenedor. Hace, en este orden:

1. **Guardas del equipo:** se niega a correr con menos del 10 % de batería y sin cargador, y si ya hay otro `make check` en marcha (un gate a la vez).
2. **HEAD exacto:** con el SHA esperado como argumento, se niega si el worktree no está en ese commit.
3. **Base de test nueva:** borra y recrea la base de tests del PostgreSQL local con los mismos roles y privilegios que `infra/docker/postgres/init/01-roles.sh` (`crm_migrator` propietario, `crm_app` en tiempo de ejecución), para que no quede deriva de migraciones de gates de otras ramas.
4. **`make check` sin modificar:** `check-repo` (tests de `.github/scripts`, `docker compose config` y **gitleaks** sobre el historial), `check-images` (build de las dos imágenes, usuario no root y prueba de humo HTTP), `check-backend` (ruff, mypy, import-linter, `check` y `check --deploy`, migraciones, OpenAPI sin drift, pip-audit y **pytest completo sin shards**) y `check-frontend` (Prettier, ESLint, tsc, Vitest, cliente API reproducible, `next build`, `pnpm audit`).
5. **Árbol sin cambios:** guarda el árbol del índice (`git write-tree`) antes y después, y marca `TREE-CHANGED` si difieren.
6. **Métricas:** apunta fecha, etiqueta, SHA, código de salida y duración en un registro local, y deja una línea de resumen (pytest, Vitest, «no leaks found»).

La evidencia que llega al PR es una línea que el agente copia en el handoff. GitHub no puede comprobarla: que `make check` corrió sobre ese SHA depende de la disciplina del script y del agente.

### Lo que cuesta, con datos reales

Del registro local de duraciones del programa (39 gates hasta el 2026-11-13):

- **Duración:** los 29 gates en verde tardaron una mediana de **787 s (unos 13 min)**, percentil 90 de 916 s. Un gate tarda de 12 a 14 min.
- **Serie:** corren de uno en uno. Una cola de 6 gates tarda unos **80 min**.
- **Fallos ajenos al código:** de los 6 últimos gates, **4 fallaron por el entorno**, no por el código:
  - lecturas de PyPI que caducaron y conexiones reiniciadas en `pip-audit`;
  - tests del frontend que superaron su tiempo límite con la máquina cargada;
  - un gate tardó **3587 s (unos 60 min)** porque el Mac se durmió a mitad (tiempo de pared muy superior al de CPU).
- **Batería:** el 2026-11-13 se perdieron **unas 4 horas** con el Mac en batería: la guarda del 10 % impide el gate y la cola entera espera.
- Fallos anteriores del mismo tipo: gates bajo carga de otros procesos y worktrees perdidos al vaciarse el directorio temporal en un reinicio.

Cada fallo ambiental cuesta otro gate completo (13 min más) y retrasa la cola.

### Lo que ya corre en GitHub

El CI del PR (gate 6) corre en `ubuntu-latest` y ya cubre casi todo `make check`, pero no todo, y no siempre:

| Parte de `make check` | En CI hoy | Diferencia |
| --- | --- | --- |
| `check-repo`: tests de `.github/scripts` | Parcial: `backend changes` corre 3 módulos, `PR governance` y `PR governance (trusted)` otros 2 | No corre la suite entera de `.github/scripts` |
| `check-repo`: `docker compose config` | `backend checks` | Solo si el PR toca el backend |
| `check-repo`: gitleaks | `secret scanning (gitleaks)`, en todos los PR (requerido) | Igual |
| `check-images`: imagen del backend | `backend docker build` | Solo si el PR toca el backend |
| `check-images`: imagen del frontend | paso final de `frontend checks` | Solo si el PR toca rutas del frontend |
| `check-backend` sin pytest | `backend checks` | Solo si el PR toca el backend |
| `check-backend`: pytest | `backend tests (shard N/4)`: 4 VM, cada una con su PostgreSQL 18.6 y su Garage; JUnit comprueba que cada shard ejecutó exactamente su plan y la suma es la suite completa | Solo si el PR toca el backend |
| `check-frontend` | `frontend checks` | Solo si el PR toca rutas del frontend |

Dos diferencias más:

- **Qué árbol se prueba.** Los workflows con `pull_request` hacen `actions/checkout` del commit de merge de prueba (`refs/pull/N/merge`), no del HEAD del PR; el check se informa sobre el HEAD. El gate local prueba el HEAD exacto (el programa mergea `origin/main` en la rama antes del gate, así que suelen coincidir, pero no es lo mismo).
- **Filtros de rutas.** `make check` lo ejecuta todo siempre. CI salta lo que el PR no toca: es correcto para el gate 6, pero no equivale al gate 5.

## Decision

Esta es la propuesta. Nada de lo que sigue rige hasta que el propietario la acepte, y su implementación es un work item aparte.

### 1. El gate 5 se ejecuta en un runner remoto y efímero, sobre el SHA exacto

Un workflow nuevo (`full-gate.yml`, en el work item de implementación) con un job de **nombre fijo `full gate`**:

- **Checkout del SHA exacto:** `actions/checkout` con `ref: ${{ github.event.pull_request.head.sha }}`, `fetch-depth: 0` (gitleaks escanea el historial) y `persist-credentials: false`. El job comprueba que `git rev-parse HEAD` es ese SHA y lo escribe en el resumen.
- **Mismo `make check`, sin modificar:** prepara las versiones de ADR-012 (uv y gitleaks fijados y verificados por SHA-256 como hoy en CI, Node de `.nvmrc`, pnpm de `packageManager`), genera un `ENV_FILE` desechable a partir de `infra/env/.env.example` con contraseñas aleatorias por ejecución (enmascaradas) y corre `make check ENV_FILE=…`. El PostgreSQL 18.6 y el Garage los levanta el propio `make check` con el compose: **la base de tests es siempre nueva**, porque la VM es nueva (sustituye el paso 3 del gate local).
- **Árbol limpio, más estricto que hoy:** al terminar exige `git status --porcelain` vacío (detecta también cambios no indexados, que `git write-tree` no ve).
- **Tamaño:** el límite de líneas relevantes ya lo aplica `PR governance (trusted)` sobre ese mismo SHA (ADR-009, y ADR-030 si se acepta); `full gate` no lo duplica.
- **Métricas:** duración y resumen (SHA, `HEAD^{tree}`, versiones, hash del `Makefile` y de los lockfiles, totales de pytest y Vitest, «no leaks found») en el resumen del job. Sustituyen al registro local.
- **Límite de tiempo:** `timeout-minutes` (por ejemplo, 45): un job colgado falla, no se queda pendiente.

### 2. Cuándo corre

`pull_request` (nunca `pull_request_target`: ejecuta código del PR), permisos `contents: read`, `concurrency` por PR con cancelación. Se recomienda que corra **solo cuando el PR es candidato a merge**: el programa pone la etiqueta `full-gate` después de la revisión (gates 3 y 4), y el workflow se dispara en `labeled` y `synchronize`. Con la etiqueta puesta, cada push nuevo vuelve a correr el gate sobre el SHA nuevo.

Un job saltado (`skipped`) cuenta como éxito para un check requerido de GitHub. Por eso el programa no se fía del verde del ruleset: exige `conclusion == success` (§3).

### 3. Cómo se ata la evidencia al SHA

Los check runs de GitHub pertenecen a un commit. Un push nuevo es otro SHA sin `full gate`: la evidencia anterior deja de valer sola.

Antes del merge, el programa:

1. Lee `headRefOid` del PR.
2. Pide los check runs de **ese** SHA con el nombre `full gate` (`GET /repos/{owner}/{repo}/commits/{sha}/check-runs?check_name=full%20gate`) y exige:
   - al menos uno, el más reciente con `status == completed` y `conclusion == success` (no `skipped`, `neutral` ni `cancelled`);
   - `head_sha` igual a `headRefOid`;
   - que lo creó la app de GitHub Actions y que pertenece al workflow `full-gate.yml`. Un *commit status* (la API antigua de estados) con el mismo nombre no vale: cualquiera con escritura puede crearlo con cualquier nombre.
3. Comprueba el resto de los gates sobre el mismo `headRefOid` (gate 6 incluido).
4. Mergea con `gh pr merge <N> --squash --match-head-commit <headRefOid>`, como hoy. Si el HEAD cambió entre la lectura y el merge, GitHub rechaza el merge.

El handoff del PR (AGENTS.md §9) enlaza el run de `full gate` en lugar de copiar sus resultados.

Opcional: una atestación de procedencia del log del gate (`actions/attest-build-provenance`), para poder verificarla fuera de GitHub. Necesita `id-token: write` y `attestations: write`; no es imprescindible para atar la evidencia al SHA.

### 4. Si el runner no arranca o falla por el entorno

- **No hay merge.** Sin un `full gate` en `success` sobre el HEAD exacto, el gate 5 no se cumple, sea cual sea la causa: el job no arranca, se queda en cola durante una incidencia de GitHub, agota el tiempo o falla por la red.
- **Nunca `--admin`**, nunca saltar el paso que falla, nunca dar por buena la ejecución de otro SHA.
- Un fallo ambiental (por ejemplo, PyPI en `pip-audit`) se **reejecuta** sobre el mismo SHA (`gh run rerun --failed`). Un fallo que se repite se investiga como un fallo real.
- Mientras tanto el programa sigue con otro work item. Volver al gate local mientras GitHub está caído **solo** si el propietario lo elige (decisión C).

### 5. Secretos

El runner **no necesita ningún secreto de producción** ni de despliegue, igual que el CI actual:

- las contraseñas de PostgreSQL y de Garage, `DJANGO_SECRET_KEY` y `SECRETS_KEKS` se generan por ejecución, con valores aleatorios que se enmascaran y se tiran con la VM;
- sin `secrets.*`; con `persist-credentials: false` el token del job no queda en el checkout, y solo tiene `contents: read`;
- con `pull_request`, un PR desde un fork no recibe secretos (y no hay ninguno que dar).

### 6. Relación con los shards de CI y con los gates 2, 5 y 6

Los shards de `backend tests` ya ejecutan la suite completa del backend contra PostgreSQL 18.6 real, en unos 3 minutos de pared, y comprueban por JUnit que no falta ni sobra ningún test. La pregunta es si bastan con un job más.

- **Gate 2 (tests focales).** Es el ciclo rápido del autor antes de hacer push; los shards ejecutan un superconjunto (la suite entera). Como evidencia, los shards en verde sobre el HEAD lo cubren de sobra **cuando el PR toca el backend**. No se propone quitar el gate 2: sigue siendo la prueba que el autor corre mientras trabaja.
- **Gate 5 (validación completa).** Los shards más un job **no** bastan tal como están, por los filtros de rutas y por el árbol de merge (§ Context). Hay dos maneras de llegar:
  - **A1 (recomendada para empezar):** `full gate` corre `make check` entero en una VM. Equivale literalmente al gate de hoy y no deriva. Contra: el pytest va sin shards. La suite tardaba unos 16–21 min sin shards en CI (ci-pipeline.md §1), así que el job tardará más que el gate local (unos 25–30 min, **estimación sin medir**). Pero no ocupa el Mac y corren varios PR a la vez.
  - **A2 (optimización posterior):** `full gate` reutiliza la matriz de shards, con un workflow reutilizable (`workflow_call`) para no duplicar su definición; los ejecuta siempre, sin filtro de rutas y sobre `head.sha`. Corre aparte los targets de `make check` que no son pytest (`check-repo`, `check-images`, `check-frontend` y la parte de `check-backend` sin pytest) y agrega los resultados con estados exactos, como `backend gate`. Más rápido, pero ya no es un `make check` literal: necesita un test de que los targets y los jobs no divergen. Se propone solo después de medir A1.
- **Gate 6 (CI del HEAD exacto).** No cambia. `full gate` es un check más sobre el mismo SHA. Los gates 5 y 6 siguen siendo dos puntos de la lista de ADR-015, pero su evidencia queda en GitHub, en el mismo commit.

### 7. Lo que no cambia

- Los diez gates de ADR-015 §2, su orden, la revisión adversarial, `--match-head-commit` y la prohibición del auto-merge de GitHub.
- La protección de `main` y el ruleset: **`full gate` no se añade como check requerido.** Cambiar la protección de ramas está prohibido al programa (ADR-015 §3). Si el propietario lo quiere obligatorio para todos los PR, lo añade él (decisión D).
- El límite de tamaño (ADR-009 hoy; ADR-030 si se acepta). Este ADR no lo toca.
- `make check` sigue siendo el comando para desarrollar y la referencia de lo que es «la validación completa».

### 8. Qué enmienda y cómo

Si el propietario lo acepta, este ADR enmienda **solo** el gate 5 de ADR-015 §2: «La validación completa (`make check`) pasa en el job `full gate` sobre el SHA exacto del HEAD» (o «en local o en `full gate`», según la decisión C). ADR-015 no se edita (es inmutable una vez aceptado): el índice indicará que ADR-036 lo enmienda en ese punto, igual que ADR-015 enmienda ADR-009. AGENTS.md §12 y delivery-automation.md §8 se actualizan en el mismo work item de implementación.

## Alternatives considered

| Alternativa | Ventajas | Por qué no es la propuesta |
| --- | --- | --- |
| **Runner de GitHub (`ubuntu-latest`) con `make check`** (§1, A1) | Reproducible (VM nueva por job), verificable por cualquiera, sin batería ni sueño, varios PR a la vez, sin coste en un repositorio público con runners estándar | **Es la propuesta** |
| **Runner autoalojado efímero por job** (`--ephemeral`, una VM limpia por job, misma imagen base versionada) | Más CPU y caché propia: podría ser más rápido que un runner estándar | Repositorio **público**: GitHub desaconseja runners autoalojados porque un PR puede ejecutar código en ellos. Hay que aislar la red, no ejecutar nunca PR de forks, mantener la imagen y pagar el anfitrión. Tiene sentido solo si A1 resulta demasiado lento o si los runners estándar no bastan para Docker |
| **Runner autoalojado persistente** | Caché caliente, el más rápido | Acumula estado entre jobs (la deriva de la base de tests que hoy se evita recreándola) y es un riesgo en un repositorio público. Descartado |
| **Mantener el gate local** (con `caffeinate`, la guarda de batería y uno a la vez) | Sin trabajo nuevo; ya conocido | Ni reproducible ni verificable. Hoy pierde horas (batería, sueño, carga, red) y serializa la cola. Se queda como herramienta de desarrollo, no como evidencia de merge |
| **Gate 6 actual como gate 5, sin job nuevo** | Ningún workflow nuevo | Por los filtros de rutas, CI no ejecuta todo `make check` en cada PR, y prueba el commit de merge, no el HEAD (§6) |
| **Runner de GitHub más grande (de pago)** | Menos tiempo de pared | Se factura por minuto incluso en repositorios públicos; sin medir A1 no está justificado |

## Consequences

- El gate 5 deja de depender de la batería, del sueño y de la carga del Mac. Varios PR pueden pasar el gate a la vez, con el límite de jobs simultáneos del plan de la cuenta. Una cola de 6 PR tardaría lo que tarda un solo job (unos 25–30 min, estimado), no unos 80 min.
- La evidencia es un check sobre el commit, enlazable desde el handoff, en lugar de una línea copiada.
- Más minutos de CI: una ejecución completa más por cada HEAD candidato a merge. En un repositorio público los runners estándar no se facturan; si el repositorio pasara a privado, se pagarían y habría que revisar esta decisión.
- La red de GitHub no es infalible: un fallo de PyPI en `pip-audit` también puede pasar ahí. La diferencia es que se reejecuta sin repetir los 13 min del Mac ni bloquear la cola.
- Un PR puede modificar `full-gate.yml` (corre la versión del PR, como `backend.yml`). Ese cambio necesita una revisión humana específica, como hoy los cambios a `backend.yml` y `backend_gate.py` (ci-pipeline.md §6, «Límite conocido»; misma naturaleza que OBS-A-02-2 en phase-1.md).

## Security implications

- `full gate` ejecuta código del PR. Mismas reglas que el CI actual: `pull_request`, nunca `pull_request_target`; permisos de solo lectura; acciones fijadas por SHA; sin secretos; `persist-credentials: false`.
- Sin secretos de producción ni de despliegue. Las credenciales de la base y del almacenamiento son aleatorias y efímeras.
- La evidencia que vale es un **check run** de la app de GitHub Actions, no un *commit status*. Así nadie fabrica un `full gate` en verde con la API de estados (§3).
- La opción autoalojada exige además: `--ephemeral`, una VM por job, sin acceso a la red interna, nunca PR de forks, y aprobación de los workflows de colaboradores externos.

## Operational implications

- Implementación (work item aparte, después de aceptar): `full-gate.yml` (A1), un test en `.github/scripts` que compruebe que el job llama a `make check` sin cambios y que los nombres son estables, y la actualización de AGENTS.md §12, delivery-automation.md §8 y ci-pipeline.md. El script de merge del programa lee el check como en §3.
- Medir A1 en 5–10 PR reales (duración, fallos ambientales) antes de plantear A2 o un runner autoalojado.
- El gate local se queda para desarrollar. Solo es evidencia de merge si el propietario lo elige como respaldo (decisión C).

## Decisión requerida del propietario

El programa no aprueba cambios de su propia gobernanza (ADR-015 §3). El propietario decide:

- **A) Aprobar con runner de GitHub** (recomendado): `full gate` con `make check` literal (A1) en `ubuntu-latest`, sustituye al gate local como evidencia del gate 5.
- **B) Aprobar con runner autoalojado efímero**, indicando quién lo aloja y lo mantiene.
- **C) Rechazar** y mantener el gate local como hoy; o aprobar A o B con el gate local como **respaldo** cuando GitHub no ejecuta runners (con sus guardas actuales).
- **D) Requerido o no:** si `full gate` debe ser además un check requerido del ruleset de `main`. Lo cambia solo el propietario.
- **E) Disparo:** solo con la etiqueta `full-gate` en los PR candidatos (recomendado) o en todos los PR.
