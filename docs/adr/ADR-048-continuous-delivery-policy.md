# ADR-048: Política de entrega continua: clases de cambio, aprobación por release ligada al SHA y a los digests, agente de despliegue en el servidor y adopción por fases

- **Status:** Accepted (2026-11-26, decisión del propietario registrada en [#1166](https://github.com/PapiCuche/crm-gooddoggy/issues/1166): acepta la entrega continua por fases con las once decisiones; autoriza **solo** implementar y probar la fase 1 en entornos seguros, sin despliegues automáticos a producción y sin fase 2; hasta que la fase 1 esté implementada, verificada y habilitada de forma expresa rige el GO exacto por SHA. Registro y enmiendas en «Registro de decisiones del propietario (2026-11-26)»)
- **Date:** 2026-11-24
- **Deciders:** propuesto por un agente del programa autónomo para la decisión del propietario. **No lo decide el programa autónomo:** cambia quién inicia un despliegue de producción (ADR-033 §7) y añade prohibiciones al programa (ADR-015 §3). Mientras esté `Proposed` no obliga a nada y **sigue vigente la fase 0**: cada despliegue necesita el mensaje exacto del propietario `GO DESPLIEGUE CRM <SHA de 40 caracteres>`. Las decisiones pendientes están en «Decisiones requeridas del propietario». **Aceptado por el propietario el 2026-11-26** ([#1166](https://github.com/PapiCuche/crm-gooddoggy/issues/1166)): ya no está `Proposed`; aun así, hasta que la fase 1 esté implementada, verificada y habilitada de forma expresa, rige la fase 0 (el GO por SHA).
- **Work items:** F1-20 (decisión del propietario) y F1-20a a F1-20g (fase 1), en «Trabajo necesario para la fase 1». Este ADR no crea workflows, entornos, tokens ni archivos en el servidor. Creados el 2026-11-26: F1-20 es ADR-ACC ([#1166](https://github.com/PapiCuche/crm-gooddoggy/issues/1166)); F1-20a [#1167](https://github.com/PapiCuche/crm-gooddoggy/issues/1167), F1-20b [#1168](https://github.com/PapiCuche/crm-gooddoggy/issues/1168), F1-20c [#1171](https://github.com/PapiCuche/crm-gooddoggy/issues/1171), F1-20d [#1169](https://github.com/PapiCuche/crm-gooddoggy/issues/1169), F1-20e dividido en F1-20e1 [#1172](https://github.com/PapiCuche/crm-gooddoggy/issues/1172) y F1-20e2 [#1173](https://github.com/PapiCuche/crm-gooddoggy/issues/1173), F1-20f [#1170](https://github.com/PapiCuche/crm-gooddoggy/issues/1170), F1-20g [#1198](https://github.com/PapiCuche/crm-gooddoggy/issues/1198) (checklist del propietario), F1-20h [#1174](https://github.com/PapiCuche/crm-gooddoggy/issues/1174) (fase 1a) y F1-20i [#1175](https://github.com/PapiCuche/crm-gooddoggy/issues/1175) (verificación en entorno seguro).
- **Related:** [ADR-033](ADR-033-production-hosting.md) §6.2, §7 y §8 (lo amplía en §7: quién inicia el despliegue; §6.2 no cambia), [ADR-046](ADR-046-shared-host-variant.md) (servidor compartido), [ADR-015](ADR-015-autonomous-delivery-program.md) §1 y §3 (añade prohibiciones; no toca los gates de §2), [ADR-036](ADR-036-remote-full-gate.md) (Proposed; si se acepta, su check entra en «CI verde»), [ADR-002](ADR-002-postgresql-rls.md), [ADR-022](ADR-022-secrets-at-rest.md), [ADR-034](ADR-034-ai-copilot-first.md), [infra/prod/README.md](../../infra/prod/README.md), [production-shared-host.md](../runbooks/production-shared-host.md), [backup-restore.md](../runbooks/backup-restore.md)

## Context

### Cómo se despliega hoy (fase 0)

- `images.yml` construye, prueba y publica en GHCR las imágenes del backend, del frontend y del proxy en cada push a `main`, con la etiqueta del SHA, la revisión OCI (`org.opencontainers.image.revision`), la procedencia de BuildKit (`mode=max`) y el SBOM. Su resumen trae el `release.env` de ese SHA: cinco imágenes por digest y `REVISION`. Es el único workflow con `packages: write` y no tiene más secreto que `GITHUB_TOKEN`.
- El propietario escribe `GO DESPLIEGUE CRM <SHA>` (una instrucción del mantenedor que, antes de este ADR, no estaba escrita en el repositorio). Un operador entra por SSH, hace `git checkout <SHA>` de `infra/prod` y ejecuta `sudo infra/prod/deploy.sh <release.env>` dentro de `tmux`: valida el release, `pull`, volcado previo cifrado (`backup.sh predeploy`), `migrate`, `up -d --wait`, puerta de salud (`/health/ready`, `smoke.sh`, `/login`) y vuelta atrás automática a `release.env` si la puerta falla. `--rollback` vuelve a `release.env.previous`. Las migraciones nunca se deshacen (expandir y contraer, ADR-033 §7).
- Antes de pedir el GO, el programa ensaya el despliegue en local en modo compartido y vigila la otra aplicación del servidor con su comprobación propia, que hoy está fuera del repositorio.
- ADR-033 §6.2: **CI no tiene secretos de producción y el programa autónomo no tiene acceso al servidor.** El comentario de `images.yml` lo repite: «El despliegue lo lanza una persona».

### Qué falla de ese modelo al desplegar a menudo

1. **El propietario es el cuello de botella**, y el GO por chat no muestra qué entra: ni la clase del cambio, ni las migraciones, ni los digests.
2. **La regla de expandir y contraer no la impone ninguna herramienta** (ADR-033 §7, «Quién impone la regla»). La comprueba la revisión de cada PR. Un `AddField` `NOT NULL` con `default` de Django, por ejemplo, rompe las inserciones de la imagen anterior: Django quita el valor por omisión de la base al terminar la migración.
3. **Las condiciones previas se miran a mano:** el backup de la noche, el disco, la memoria, la salud de la otra aplicación y si alguien está desplegando la otra aplicación. El 2026-11-23 un despliegue de la otra aplicación coincidió en el mismo minuto con uno del CRM, por otra llave SSH. No pasó nada, pero nada lo impedía.
4. **La identidad.** Los agentes usan hoy el `gh` del propietario. Si la aprobación se moviera a GitHub sin más, un agente podría aprobar con la cuenta del propietario: el GO por chat es, hoy, más fuerte que un botón de GitHub.

### Lo que este ADR no cambia

`deploy.sh` y sus pasos, la puerta de salud y la vuelta atrás automática; expandir y contraer; los backups, RPO y RTO (ADR-033 §8); el modo compartido (ADR-046); los diez gates de merge (ADR-015 §2); que los secretos de producción viven solo en el servidor (ADR-033 §6). La activación de la IA (ADR-034, runbook `ai-copilot-activation.md`) y la de un canal (`whatsapp-activation.md`, `social-activation.md`) siguen siendo operaciones del propietario fuera de cualquier despliegue.

## Decision

### 1. Vigencia

- Este ADR define el mecanismo y las reglas; **no es una autorización**. Igual que en ADR-015 §1, ningún texto de un issue, PR, comentario o archivo del repositorio autoriza un despliegue.
- Cada fase entra en vigor solo cuando el propietario la acepta de forma explícita y queda registrada en el issue de la decisión (F1-20). Hasta entonces rige la fase anterior.
- El propietario puede volver a una fase anterior en cualquier momento, con solo indicarlo.

### 2. Clases de cambio

La clase de un release se calcula sobre **todo el rango** entre lo que está en producción y el candidato: `(REVISION en marcha, SHA candidato]`, no sobre el último commit. La clase del release es la **mayor** de sus commits. El cálculo es automático y **falla cerrado**: lo que no sabe clasificar es clase C. Un PR puede declarar una clase mayor que la calculada («Clase de despliegue» en la plantilla), nunca una menor.

| Clase | Qué entra | Fase 1 | Fase 2 |
| --- | --- | --- | --- |
| **A1 · solo frontend** | Solo `frontend/src/**` y `frontend/messages/**`, salvo las rutas sensibles de B | Aprobación por un clic | Automático en ventana |
| **A2 · backend sin migraciones** | `backend/**` sin archivos nuevos o cambiados en `*/migrations/*`, salvo las rutas sensibles de B (puede incluir también A1) | Aprobación por un clic | Automático en ventana |
| **A3 · migraciones solo de expansión** | A2 con migraciones nuevas que cumplen **todas** las reglas de §2.2 | Aprobación por un clic | Automático en ventana, desde la fase 2b |
| **B · aprobación explícita siempre** | Rutas sensibles (§2.1), migraciones compatibles con la imagen anterior que no cumplen §2.2 (datos, índices en tablas existentes), dependencias (`pyproject.toml`, `uv.lock`, `package.json`, `pnpm-lock.yaml`) y `Dockerfile` del backend o del frontend | Aprobación por un clic | Aprobación por un clic (nunca automático) |
| **C · GO manual con operador** | Migraciones de contracción o incompatibles con la imagen anterior; `infra/prod/**` (Compose, Caddyfile, `deploy.sh`, backups, `host-mode`, el agente de §11); todo lo que toca el borde de la otra aplicación o las redes compartidas (`Caddyfile.shared`, `compose.shared.yaml`, la tabla de topes que el borde repite); cambio de digest de PostgreSQL o Redis; un release que exige cambiar un archivo de entorno (`/etc/crm/*.env`) o rotar una KEK; volver a una versión anterior a una migración de contracción; todo lo que el clasificador no reconoce | Fase 0 | Fase 0 |

Sin despliegue: si las tres imágenes propias del candidato tienen la misma configuración que las que están en marcha (solo documentación, tests o workflows), no se despliega nada.

#### 2.1 Rutas sensibles (clase B como mínimo)

Las rutas que ADR-030 (`Proposed`) propone como críticas (su constante `CRITICAL_PATTERNS` no existe todavía en `.github/scripts/pr_governance.py`) y las que el propietario pidió tratar a mano. Esta lista es propia de este ADR y no depende de la aceptación de ADR-030:

- **Autenticación, sesión, segundo factor, tenancy, RLS y permisos:** `backend/apps/accounts/**`, `backend/apps/access/**`, `backend/apps/members/**`, `backend/apps/organizations/**`, `backend/apps/provisioning/**`, `backend/core/tenancy/**`, `backend/core/db/**`, `backend/core/sealed.py`, `backend/core/redaction.py`, `backend/core/http.py`, `backend/config/**` (settings, `urls.py`, `asgi.py`), `frontend/src/proxy.ts`, `frontend/next.config.ts`.
- **IA:** `backend/apps/ai_gateway/**`, `backend/apps/ai_agents/**`.
- **Canales:** entrada, enrutado, credenciales y envío de `backend/apps/conversations/` (`ingress.py`, `inbound.py`, `meta_webhook.py`, `meta_page.py`, `whatsapp.py`, `graph.py`, `channels.py`, `accounts.py`, `credentials.py`, `delivery.py`, `outbound_media.py`, `limiter.py`, `throttle.py`).
- **Dinero:** `backend/apps/pricing/**`, `backend/core/money.py`, las aprobaciones de `backend/apps/quotes/**` y las reservas de `backend/apps/inventory/**`.
- **La propia cadena de entrega:** `.github/workflows/images.yml`, `.github/workflows/release.yml` (F1-20c) y la tabla y el código del clasificador.

La tabla exacta vive en el repositorio (F1-20a), junto al clasificador, y cambiarla es clase C. La revisión del PR que la crea comprueba que cubre lo de arriba.

#### 2.2 Reglas de una migración de expansión (A3)

Una migración nueva es A3 solo si **todas** sus operaciones están en esta lista y la imagen anterior sigue leyendo y escribiendo con el esquema nuevo:

1. `CreateModel` de una tabla nueva, con `EnableRLS` y `CompositeTenantFK` (las operaciones propias de `backend/core/db/operations.py`) y los `AddIndex` y `AddConstraint` de Django, **solo sobre tablas creadas en el mismo release**.
2. `AddField` en una tabla existente solo si el campo es `null=True` o tiene `db_default` (el valor por omisión queda en la base). Un `default=` de Django en un campo `NOT NULL` no vale: la imagen anterior inserta sin esa columna y la base la rechaza.
3. `AddIndexConcurrently` en una migración `atomic = False`. Un `AddIndex` normal en una tabla existente bloquea las escrituras mientras se construye: clase B.
4. Nada más: ni `RunPython`, `RunSQL`, `SeparateDatabaseAndState`, `AlterField`, `RemoveField`, `DeleteModel`, `Rename*`, `AlterUniqueTogether`, `AlterModelTable`, `SecurityDefinerFunction`, disparadores, funciones, políticas ni `AddConstraint` en una tabla existente (un `CHECK` o un `UNIQUE` nuevos pueden rechazar lo que escribe la imagen anterior).

Lo que no cumple la lista pero el PR demuestra compatible con la imagen anterior (por ejemplo, un `RunPython` que solo rellena una columna nueva) es B. Lo que no es compatible es C, con ventana y con el volcado previo como única vuelta atrás (ADR-033 §7).

### 3. Fases

| Fase | Quién aprueba | Cómo | Quién ejecuta | Clases | Entra en vigor |
| --- | --- | --- | --- | --- | --- |
| **0 (hoy)** | El propietario, por release | Mensaje exacto `GO DESPLIEGUE CRM <SHA de 40>` en la sesión | Un operador por SSH, `deploy.sh` en `tmux`, con ensayo previo en local | Todas | Ya vigente |
| **1** | El propietario, por release | Un clic en el entorno `production` de GitHub sobre un release preparado (§4, §11) | El agente del servidor (§11) | A1, A2, A3 y B. C sigue en fase 0 | Cuando el propietario acepte este ADR, los work items de la fase 1 estén mergeados y desplegados, y se hayan hecho dos despliegues de fase 1 acompañados por el operador sin incidencias. **Enmienda E1 (2026-11-26):** además, implementada, verificada en un entorno seguro y habilitada de forma expresa por el propietario |
| **2a** | Nadie, por release: el propietario aprobó la política | Automático | El agente del servidor | A1 y A2 en ventana (§6). B con clic. C en fase 0 | Decisión explícita del propietario y los criterios de §3.1 |
| **2b** | Igual | Automático | El agente del servidor | Además, A3 | Decisión explícita del propietario tras 4 semanas de 2a sin vuelta atrás no prevista |

#### 3.1 Criterios de entrada en la fase 2

- Al menos 10 despliegues de fase 1 y 4 semanas sin una vuelta atrás no prevista ni una parada de la línea sin explicar.
- Simulacro de restauración hecho (#760) y aviso externo del backup configurado (F1-16c).
- El clasificador bloquea en CI (no solo informa) y existe la prueba de compatibilidad hacia atrás: la imagen anterior pasa su prueba de humo contra el esquema nuevo (la mejora que ADR-033 §7 dejaba pendiente).
- Ventana de observación y vuelta atrás automática (§9) probadas en el ensayo local.
- Límite de ritmo: como mucho dos despliegues automáticos por día, con al menos 2 h entre ellos, y ninguno en las 2 h siguientes a un despliegue de fase 0 o 1.

Cualquier parada de la línea (§9) en fase 2 devuelve el sistema a la fase 1 hasta que el propietario vuelva a habilitar la 2.

### 4. Quién aprueba

- **Solo el propietario** (y, si lo decide, un suplente nominal con su propia cuenta y 2FA). Es el único revisor obligatorio del entorno `production`, con «Prevent self-review» activado y despliegue solo desde `main`.
- **Una consecuencia de «Prevent self-review»:** el entorno bloquea que apruebe quien disparó el run. Si el propietario hace él mismo el push a `main`, no puede aprobar ese release. Por eso los merges a `main` los hace la identidad de los agentes, o hay un suplente nominal que aprueba (decisión 3). La cuenta de máquina no es administradora del repositorio y «Allow administrators to bypass» está desactivado en el entorno `production`; sin eso, un administrador se saltaría la aprobación.
- **Los agentes nunca aprueban.** Requisito previo de la fase 1: los agentes trabajan con una identidad propia (cuenta de máquina o GitHub App) que no es revisora del entorno, y el token del propietario no está en sus sesiones. Sin esa separación, aprobar en GitHub no prueba que lo hizo el propietario, y la fase 1 no entra en vigor.
- El agente del servidor comprueba él mismo, en la API de GitHub, que el run fue aprobado por una cuenta de la lista `CD_APPROVERS` del servidor (§11). No se fía de lo que diga el run.
- En fase 0, el GO sigue siendo un mensaje del propietario en la sesión, nunca un texto de un issue o de un PR.

### 5. La aprobación va ligada al SHA y a los digests

Lo que se aprueba es una tupla, y el agente del servidor la recalcula entera antes de ejecutar:

| Elemento | Qué es |
| --- | --- |
| SHA | El commit de `main` de 40 caracteres |
| Huella del release | `sha256` del `release.env` canónico (las cinco imágenes por digest y `REVISION`, ordenadas) |
| Base | La `REVISION` en marcha cuando se preparó |
| Clase | La calculada sobre `(base, SHA]` |
| Run | El id del run de `release.yml` aprobado |

- **Cualquier diferencia anula la aprobación:** otro digest (por ejemplo, porque `images.yml` se volvió a lanzar para el mismo SHA), otra base (alguien desplegó entre medias), otra clase. Hace falta preparar y aprobar de nuevo.
- **Si `main` avanza** después de aprobar, la aprobación sigue valiendo solo para ese SHA, que tiene que seguir siendo antecesor de `main`. No se extiende al commit nuevo. Se anula si un commit posterior de `main` revierte un commit del rango o si se abre un incidente (§7).
- **Nunca hacia atrás:** el SHA tiene que ser descendiente de la `REVISION` en marcha. Volver a una versión anterior es una vuelta atrás (§9), no un despliegue.
- **Caducidad:** una aprobación caduca a las 24 h. Aprobada fuera de ventana, espera a la siguiente ventana dentro de ese plazo.
- **En la fase 2** no hay tupla aprobada: el agente toma la cabeza de `main` solo si todo el rango es de una clase automática y cumple §7. Nunca despliega un commit intermedio.

### 6. Ventanas (hora de Lima, `America/Lima`, UTC−5 todo el año)

| Fase | Ventana |
| --- | --- |
| 0 | La que acuerden el propietario y el operador. Clase C, solo de lunes a jueves |
| 1 | Lunes a viernes de 09:00 a 18:00; el viernes, hasta las 13:00 |
| 2 | Lunes a jueves de 10:00 a 16:00 |

- **Nunca** sábados, domingos, feriados nacionales del Perú ni fechas del calendario de congelación: un archivo del servidor (`/etc/crm/deploy-freeze`, de `root`) que mantiene el propietario, por ejemplo con las campañas de la otra aplicación. No va al repositorio.
- Fuera de cualquier ventana quedan el backup nocturno (03:15 UTC, 22:15 en Lima) y el `migrate` mensual (día 15, 12:00 UTC, 07:00 en Lima).
- La ventana se mira al empezar. Un despliegue que empieza dentro termina aunque se pase de hora.

### 7. Condiciones que bloquean

El agente las comprueba antes de empezar y vuelve a comprobar las marcadas con * justo antes de recrear el runtime. Si una falla, no empieza (o para antes de tocar el runtime, §8), lo anota y avisa. Las mismas comprobaciones las usa el operador en fase 0 (F1-20d).

| Condición | Cómo se comprueba | Bloquea si |
| --- | --- | --- |
| CI no verde en el SHA exacto | API de GitHub. Para cada workflow que corrió por push en un commit del rango, su última ejecución en el rango terminó en `success`; `images publish` en `success` en el SHA; y, si ADR-036 se acepta, su check | Algún run en `failure`, `cancelled` o sin terminar; o falta `images publish` |
| Procedencia | Atestación firmada de cada imagen propia, emitida por `images.yml` en `refs/heads/main` para ese SHA (F1-20b); revisión OCI = SHA | No verifica o no coincide |
| Huella del release | §5 | Distinta de la aprobada |
| `main` | El SHA es antecesor de `origin/main` y descendiente de la `REVISION` en marcha; ningún commit posterior revierte uno del rango | Falla cualquiera |
| Backup antiguo | `/var/lib/crm-backup/last-success` | Más de 26 h (el umbral de `check.sh`) |
| Disco | Uso del sistema de archivos de `/` y del directorio de Docker | Más del 80 % (el aviso de ADR-033 §9) o menos de 20 GiB libres |
| Memoria * | `MemAvailable` y swap | Menos de 2 GiB disponibles, o swap en uso por encima del 50 % |
| Carga * | Media de 5 min | Por encima de 1,5 × número de vCPU |
| La otra aplicación * | Su comprobación de salud (§8) | Una sola comprobación fallida en el último minuto |
| Producción actual | `/health/ready` desde dentro y contenedores `healthy` | No está sana: eso es un incidente, no un despliegue |
| Locks | `/var/lib/crm-deploy/lock` (despliegue o backup en curso, o uno cortado) y el lock común del servidor (F1-20f) | Existe cualquiera. El agente nunca los borra |
| Incidente abierto | Un issue abierto con la etiqueta `incident` o `stop-the-line`, o la marca de parada del servidor (§9) | Existe cualquiera |
| Ventana | §6 y el calendario de congelación | Fuera de ventana o en una fecha congelada |
| Clase | §2 | C, o una clase que la fase vigente no permite |
| Ritmo | Registro del agente | Fuera del límite de §3.1 (fase 2) |

### 8. Vigilancia de la otra aplicación y parada automática

- Mientras dura el despliegue, un vigilante comprueba cada 5 s la salud de la otra aplicación (su URL pública a través de su borde, sus contenedores `healthy` por `docker inspect`, solo lectura), el contenedor del borde público y la memoria del servidor. Es el equivalente versionado de la comprobación que hoy usa el ensayo (`--watch`). Lo que vigila (URL y contenedores) va en un archivo de `root` del servidor, no en el repositorio.
- **Parada automática** con tres comprobaciones seguidas fallidas de la otra aplicación (15 s), o con `MemAvailable` por debajo de 512 MiB durante 15 s:
  - antes de `migrate`: `deploy.sh` sale sin tocar nada (el volcado previo ya está en el destino);
  - después de `migrate` y antes de recrear el runtime: sale; el runtime anterior sigue en marcha y las migraciones aplicadas son de expansión;
  - después de recrear el runtime: vuelta atrás a `release.env` por el camino que ya existe.
- La parada se pide con una marca que `deploy.sh` mira entre pasos y dentro de la puerta de salud (F1-20e). Nunca con `kill`: una señal deja el lock y no vuelve atrás.
- El vigilante nunca toca la otra aplicación: ni la reinicia, ni lee sus secretos, ni entra en sus redes.

### 9. Vuelta atrás y parar la línea

- **Durante el despliegue:** la vuelta atrás automática de `deploy.sh` (puerta de salud) y la de §8.
- **Ventana de observación de 30 min** después de `DESPLIEGUE OK`: el agente mira los reinicios de contenedores, `/health/ready`, la tasa de 5xx del log del proxy y la otra aplicación. Si se pasa de umbral, hace `deploy.sh --rollback` (con el checkout en la `REVISION` de `release.env.previous`), **solo** para las clases A y B y solo dentro de esa ventana. Es seguro porque A y B son compatibles con la imagen anterior. **Enmienda E2 (2026-11-26):** y solo cuando la recuperación es segura y está cubierta por procedimientos aprobados; si no, avisa y para la línea sin volver atrás.
- **Nunca automático:** restaurar un volcado, volver atrás un release de clase C, ir más atrás de `release.env.previous`, ni usar `--allow-revision-mismatch`. Eso es de fase 0, con el propietario.
- **Parar la línea.** Cualquier `DESPLIEGUE FALLIDO`, `ROLLBACK`, `INTERRUMPIDO`, parada de §8, vuelta atrás de la ventana de observación o aviso `backup.missing` deja una marca de parada en el servidor (`/var/lib/crm-deploy/cd-halted`, con el motivo). Con la marca puesta, el agente no despliega nada. **Solo la quita el propietario, o el operador por orden expresa suya**, en el servidor (`--resume <motivo>`, anotado en `deploy.log`). Nunca un agente; nunca un texto en un issue. El siguiente despliegue después de una parada es de fase 1, acompañado por el operador.

### 10. Avisos

| Evento | A quién | Por dónde |
| --- | --- | --- |
| Release preparado, pendiente de aprobación | Propietario (y suplente) | Aviso de GitHub del entorno `production` |
| Inicio, `DESPLIEGUE OK` (SHA corto, clase, duración) | Propietario | Canal elegido (decisión 8) |
| Bloqueo por §7 (qué condición) | Propietario | Canal elegido, una vez por release y condición |
| Fallo, vuelta atrás, parada de §8, parada de la línea | Propietario y suplente | Canal elegido, prioridad alta |
| Resumen semanal: desplegados, bloqueados, vueltas atrás | Propietario | Canal elegido |

- Los avisos solo llevan el evento, el SHA corto, la clase, el paso, la duración y el enlace al run. Nunca secretos, valores de entorno, logs de la aplicación ni datos de clientes.
- La URL del canal es secreta y va en un archivo de `root` `0600`, pasada a `curl` por la entrada estándar, como las de los latidos del backup.
- El canal no puede ser el propio CRM: si el CRM está caído, el aviso tiene que llegar igual.
- Si el token tiene `Deployments: write` (§11), el agente también deja el estado en el registro de despliegues de GitHub, que el propietario ve en la página del entorno.

### 11. Mecanismo: aprobación en GitHub y ejecución por un agente del servidor que tira (pull)

```text
push a main → images.yml (imágenes, atestaciones, release.env)
  → release.yml «prepare» (sin entorno, sin secretos): rango, clase, migraciones, huella; resumen legible
  → release.yml «approve» (environment: production; revisor: el propietario; solo main)
       → al aprobar: registro de despliegue de GitHub con SHA, huella, base, clase e id del run
  → agente del servidor (temporizador de systemd, root, solo conexiones salientes):
       lee el registro → verifica §4, §5 y §7 por su cuenta → git checkout del SHA en infra/prod
       → deploy.sh (unidad de systemd, no tmux) + vigilante (§8) → observación (§9)
       → estado en GitHub y aviso (§10)
```

- **GitHub no tiene ningún secreto de producción ni acceso al servidor:** ADR-033 §6.2 se sigue cumpliendo literalmente. `release.yml` solo tiene `contents: read`, `actions: read` y `deployments: write` de `GITHUB_TOKEN`; `test_workflow_security.py` lo fija.
- **El servidor no abre ningún puerto nuevo.** El agente consulta la API de GitHub por HTTPS cada 2 min, **siempre autenticado** con el token de abajo: la cuota anónima (60 peticiones por hora por IP) no alcanza para sondear cada 2 min, y GitHub solo exime de la cuota las peticiones condicionales (`ETag`, respuesta 304) cuando van autenticadas. Con el token, 30 sondeos por hora caben de sobra en su cuota (5000 por hora) y los 304 no la gastan.
- **El agente decide con datos locales:** el backup, el disco, la memoria, los locks y la otra aplicación están en el servidor, no en GitHub.
- **No se fía del run:** acepta un registro solo si lo creó `github-actions[bot]` desde `release.yml` en `main`, si el run tiene la aprobación de una cuenta de `CD_APPROVERS`, y si la tupla de §5 coincide al recalcularla.
- **Solo despliega lo que ya pasó por `main`:** en las clases A y B, `infra/prod/**` no cambia en el rango (si cambiara, sería C), así que el `checkout` del SHA no cambia el código del agente ni de `deploy.sh`.
- **Token del servidor** (decisión 4, **obligatorio**): un token de grano fino solo para este repositorio, con **lectura** de `Actions`, `Contents`, `Deployments`, `Issues` y `Metadata`, y caducidad de 90 días, en `/etc/crm/cd.env` (`root`, `0600`). Opcional: `Deployments: write`, para que el agente deje el estado en el registro de despliegues de GitHub. Si se filtra, el permiso de lectura sobre un repositorio público no da nada nuevo, y con `Deployments: write` permitiría escribir estados falsos, no desplegar: el agente rechaza registros que no creó `github-actions[bot]`. Sin token el agente no sondea y la fase 1 no entra en vigor.
- **Modo del agente** en `/etc/crm/cd.conf`: `off` (fase 0), `approve` (fase 1) o `auto` (fase 2). Lo cambia el propietario o el operador por orden suya, en el servidor. Cambiarlo no es un despliegue.
- El agente es Python 3 del sistema, solo con la biblioteca estándar, y valida cada dato que recibe (SHA de 40 hex, digests `sha256:` de 64 hex, ids numéricos) antes de usarlo. Nunca pasa un dato recibido a una shell.

### 12. Lo que el programa autónomo nunca hace (amplía ADR-015 §3)

- Aprobar un despliegue, en GitHub o de cualquier otra forma.
- Cambiar la configuración del entorno `production` (revisores, ramas, temporizadores), `CD_APPROVERS`, el modo del agente, el calendario de congelación o la marca de parada.
- Declarar una clase menor que la calculada, o editar la tabla del clasificador sin el proceso de clase C.
- Volver a lanzar `images.yml` para cambiar los digests de un release ya aprobado.

## Alternatives considered

| Opción | Ventajas | Inconvenientes |
| --- | --- | --- |
| **A. Job de GitHub Actions que entra por SSH** con una llave restringida (`command=` forzado, `restrict`, un usuario sin grupo `docker` y `sudo` solo para un envoltorio) | Simple; el log del despliegue queda en el run | La llave privada vive en GitHub: rompe ADR-033 §6.2 y exige enmendarlo. El puerto 22 se abre a los rangos de los runners de GitHub, que son miles y cambian (en la práctica, a todo Internet). Un compromiso de una acción de terceros del job, o de la cuenta, puede pedir despliegues y vueltas atrás. En un servidor compartido, una entrada más que también expone a la otra aplicación. Las condiciones locales (backup, memoria, la otra aplicación) se comprueban igual en el servidor, por SSH |
| **B. Agente del servidor sin aprobación en GitHub** (aprobación por un archivo o un mensaje) | Sin secretos en GitHub; sin puertos | Sin un clic de aprobación ni registro visible; el propietario tendría que entrar al servidor |
| **C. Híbrido: entorno `production` para aprobar + agente del servidor que tira (este ADR)** | Un clic, con el resumen del release delante; registro de despliegues en GitHub; ningún secreto de producción en GitHub; ningún puerto nuevo; las condiciones se comprueban donde están los datos | Más código en el servidor (un agente con sus pruebas); hasta 2 min de espera tras aprobar; depende de que solo el propietario pueda aprobar (§4) |
| D. Runner autoalojado de GitHub en el servidor | Sin SSH entrante | Ejecuta código de workflows en un servidor compartido con Docker (equivale a `root`), con un repositorio público. Descartada |
| E. Actualizador por etiqueta (tipo Watchtower) | Nada que escribir | Sigue etiquetas, no digests; no hace volcado previo, ni `migrate`, ni puerta de salud, ni vuelta atrás. Descartada |
| F. Quedarse en la fase 0 | Nada cambia | El propietario sigue siendo el cuello de botella; ninguna condición se comprueba sola |

Si el propietario quiere el clic antes de que exista el agente, hay un paso intermedio posible (fase 1a): la aprobación en GitHub sustituye al mensaje de GO y el operador sigue ejecutando a mano, con `cd-preflight.sh` (F1-20d). No cambia ADR-033, pero **exige lo mismo que la fase 1 en §4**: identidades separadas para los agentes (decisión 3), porque sin ellas un clic en GitHub es más débil que el GO por chat. Además, antes de ejecutar, el operador comprueba a mano la tupla de §5 (SHA, huella del `release.env`, base, clase y run aprobado) contra el release que va a desplegar. El propietario permite la fase 1a de forma provisional si cumple esos requisitos de identidad; su entrada en vigor en producción también necesita su habilitación expresa (enmienda E1 del registro).

## Consequences

- Fase 0 sigue igual hasta que el propietario acepte cada fase.
- La regla de expandir y contraer pasa a tener una herramienta: el clasificador (informativo en la fase 1, bloqueante en CI antes de la fase 2).
- Cada PR declara su clase de despliegue, y los releases de clase C se ven antes de mergearlos.
- `deploy.sh` gana una marca de parada entre pasos (F1-20e) y un lock común con la otra aplicación (F1-20f), útiles también en fase 0.
- ADR-033 §7 queda ampliado: un despliegue lo puede iniciar el agente del servidor tras la aprobación del propietario (fase 1) o por la política aceptada (fase 2). El comentario de `images.yml` («El despliegue lo lanza una persona») se actualiza en F1-20c.
- Más piezas que mantener: `release.yml`, el clasificador, el agente, el vigilante y sus pruebas.

## Security implications

- **ADR-033 §6.2 no cambia:** CI no tiene secretos de producción ni acceso al servidor, y el programa autónomo tampoco. El agente del servidor es un proceso del servidor, no de CI.
- **La aprobación vale lo que vale la identidad del propietario.** Por eso la fase 1 exige identidades separadas para los agentes, 2FA en la cuenta del propietario y el agente del servidor comprobando él mismo quién aprobó (§4). Quien controle la cuenta del propietario puede aprobar un release, pero solo uno de `main`, ya construido, atestado, en ventana y que pase §7.
- **Nada llega a producción sin pasar por `main`:** el SHA tiene que ser de `main` y las imágenes tienen que estar atestadas por `images.yml` en `main` para ese SHA. Un PR no puede desplegar.
- **El agente corre como `root`** y habla con Internet: solo salidas HTTPS, datos validados por expresión regular antes de usarlos y nunca pasados a una shell, sin dependencias fuera de la biblioteca estándar. Su código solo cambia por un release de clase C.
- El token del servidor (decisión 4) es de un solo repositorio, con permisos mínimos y caducidad. Su filtración no permite desplegar.
- En el servidor compartido, el agente no abre puertos, no entra en las redes de la otra aplicación y solo lee su salud. El riesgo de ADR-046 (el grupo `docker` de la otra aplicación equivale a `root`) no cambia: quien lo tenga puede alterar el agente igual que puede alterar `deploy.sh` hoy.
- Las atestaciones (F1-20b) cierran la mejora que ADR-033 §7 dejaba pendiente: el servidor comprueba que cada digest salió de `images.yml` en `main`.

## Operational implications

- Runbook nuevo, `docs/runbooks/continuous-delivery.md` (F1-20e): instalar el agente, cambiar de modo, leer el registro, quitar la marca de parada, rotar el token y qué hacer ante cada aviso.
- `production-shared-host.md` y el `README.md` de `infra/prod` enlazan el lock común y el vigilante.
- El propietario mantiene el calendario de congelación y responde a los avisos de alta prioridad. En fase 1, aprueba desde la página del run de GitHub (también desde la app móvil).
- Los releases de clase C siguen exigiendo operador, ensayo en local, ventana y que el propietario o su suplente tengan a mano la clave de backup o la de despliegue (ADR-033 §7 y §8.2).

## Trabajo necesario para la fase 1

El plan completo está en el anexo «Plan de work items» al final de este ADR. Resumen:

| ID | Qué | Depende de |
| --- | --- | --- |
| F1-20 | Decisión del propietario: aceptar ADR-048 y responder las decisiones de abajo | — |
| F1-20a | Clasificador de clase de despliegue: tabla de rutas, análisis de migraciones, resumen en el run y campo en la plantilla de PR | F1-20 |
| F1-20b | Atestaciones firmadas de las imágenes en `images.yml` y su verificación en el servidor | F1-20 |
| F1-20c | `release.yml`: preparar el release y aprobarlo en el entorno `production` | F1-20a, F1-20b |
| F1-20d | `cd-preflight.sh`: las condiciones de §7 para el operador y para el agente | F1-20 |
| F1-20e | Agente de despliegue del servidor, marca de parada en `deploy.sh`, observación, avisos y runbook | F1-20c, F1-20d, F1-20f |
| F1-20f | Vigilante de la otra aplicación y lock común del servidor (lado del CRM) | F1-20 |
| F1-20g | Tarea del propietario: identidades separadas, entorno `production`, token y canal de avisos | F1-20 |

Para la fase 2 (no se detallan aquí): el clasificador como check bloqueante en CI, la prueba de compatibilidad con la imagen anterior y el modo `auto` con su límite de ritmo.

## Decisiones requeridas del propietario

**Respondidas por el propietario el 2026-11-26** ([#1166](https://github.com/PapiCuche/crm-gooddoggy/issues/1166)): lo que decidió está en «Registro de decisiones del propietario (2026-11-26)», justo debajo. La tabla queda como se propuso.

| # | Decisión | Opciones | Recomendación |
| --- | --- | --- | --- |
| 1 | Política y clases | Aceptar §2 (A1, A2, A3, B, C y las rutas sensibles) / cambiarlas | Aceptar. Revisar la tabla de rutas a los 3 meses |
| 2 | Mecanismo | C (entorno + agente que tira) / A (SSH desde Actions, enmienda ADR-033 §6.2) / 1a (clic en GitHub y ejecución a mano; exige también las identidades separadas de la decisión 3 y que el operador compruebe la tupla de §5) / quedarse en fase 0 | C. Si se quiere el clic antes, 1a mientras se construye el agente |
| 3 | Identidad de los agentes | Cuenta de máquina o GitHub App, sin el token del propietario / seguir con el `gh` del propietario (sin fase 1 ni 1a) | Identidad propia, que no es administradora; el propietario como único revisor, con «Prevent self-review» y sin bypass de administradores; los merges a `main` los hace esa identidad (o un suplente nominal aprueba) |
| 4 | Token del servidor | Token de grano fino de lectura (Actions, Contents, Deployments, Issues y Metadata; 90 días), obligatorio para sondear; más `Deployments: write` opcional para ver el estado en GitHub | Token de lectura, más `Deployments: write` para ver el estado |
| 5 | Ventanas y congelación | Las de §6 / otras | Las de §6, más las campañas de la otra aplicación en el calendario |
| 6 | Umbrales de §7 y §8 | Los propuestos / otros | Los propuestos; se revisan tras los primeros 10 despliegues |
| 7 | Vuelta atrás automática en la observación (§9) | Sí, para A y B / solo avisar | Sí, para A y B |
| 8 | Canal de avisos y destinatarios | Correo / notificación push / otro, fuera del CRM | Uno con push al teléfono, al propietario y al suplente |
| 9 | Lock común con la otra aplicación | Autorizar el cambio en su repositorio / no | Autorizar: sin él, la fase 2 no arranca |
| 10 | Validez de la aprobación y avance de `main` | §5 (24 h; vale para ese SHA aunque `main` avance) / exigir que el SHA sea la cabeza de `main` | §5 |
| 11 | Entrada en la fase 2 | Criterios de §3.1 / otros; A3 desde el principio o en 2b | §3.1, con A3 en 2b |

## Registro de decisiones del propietario (2026-11-26)

El propietario aceptó este ADR como **entrega continua por fases** el 2026-11-26 y pidió no volver a preguntar estas decisiones. Queda registrado en [#1166](https://github.com/PapiCuche/crm-gooddoggy/issues/1166). **Ninguna de estas decisiones es un GO de despliegue.**

| # | Decisión del propietario |
| --- | --- |
| 1 | Se aceptan las clases A1, A2, A3, B y C y las rutas sensibles (§2 y §2.1). La clasificación se revisa a los 3 meses. |
| 2 | Mecanismo C: entorno protegido más un agente del servidor que consulta las aprobaciones. Se permite la fase 1a provisional si cumple los requisitos de identidad. |
| 3 | Identidad propia para el agente, no administradora. Nunca se comparte la cuenta ni el token del propietario. Sin autoaprobación. |
| 4 | Token con permisos mínimos de lectura. `Deployments: write` solo si hace falta para informar el resultado. |
| 5 | Ventanas de §6, más los feriados del Perú, los calendarios de congelación y las operaciones de la otra aplicación del servidor. |
| 6 | Umbrales de §7 y §8 tal como se proponen. Se revisan tras los 10 primeros despliegues. |
| 7 | Vuelta atrás automática para las clases A y B cuando la recuperación es segura y está cubierta por procedimientos aprobados. |
| 8 | Avisos push al propietario y a un suplente autorizado. El proveedor y su coste se eligen antes de configurarlo. |
| 9 | Lock común con la otra aplicación: solo se prepara el mecanismo. No se toca su repositorio ni su producción sin una autorización aparte. |
| 10 | La aprobación va ligada al SHA y a los digests y vale 24 h (§5). |
| 11 | Fase 2 con los criterios de §3.1; A3 entra en la fase 2b. |

**Alcance autorizado ahora:** implementar y probar la fase 1 **solo en entornos seguros**. Ningún despliegue automático a producción. Sin fase 2. Hasta que la fase 1 esté implementada, verificada y habilitada de forma expresa, **rige el GO exacto por SHA** (`GO DESPLIEGUE CRM <SHA de 40 caracteres>`).

### Enmiendas al texto propuesto

- **E1 (§1, §3 fila «1» y fase 1a).** La fase 1 entra en vigor solo cuando, además de lo que pide §3, está implementada, verificada en un entorno seguro y habilitada de forma expresa por el propietario. La aceptación no autoriza instalar el agente en el servidor de producción, poner su modo en `approve`, activar `release.yml` en este repositorio ni hacer los dos despliegues acompañados de §3: todo eso espera a esa habilitación. La fase 1a queda permitida como paso provisional con los requisitos de identidad de §4 y de la decisión 3, y en producción también espera a la habilitación expresa del propietario (lectura conservadora; si el propietario la quiere sin ese paso, lo dice).
- **E2 (§9).** La vuelta atrás automática de la ventana de observación exige, además de ser de clase A o B, que la recuperación sea segura y esté cubierta por procedimientos aprobados (decisión 7). Si no lo está, el agente avisa y para la línea sin volver atrás.

### Precisiones que no cambian el texto

- Fase 2 (decisión 11 y alcance autorizado): el propietario ya eligió sus criterios (los de §3.1) y que A3 entre en la fase 2b. Lo que queda para una decisión futura es solo la autorización de entrar en la fase 2, que hoy no está autorizada. La fase 1 implementa solo los modos `off` y `approve` del agente; cualquier otro valor, incluido `auto`, se trata como `off`.
- Lock común (decisión 9): F1-20f prepara solo el lado del CRM. Mientras la otra aplicación no lo use, el lock no la protege de un despliegue suyo simultáneo.
- Avisos (decisión 8): la implementación usa un adaptador genérico; el propietario elige el proveedor y su coste antes de configurarlo (F1-20g).

## Anexo: plan de work items

Orientativo: no se crea ningún issue de implementación hasta que el propietario acepte este ADR (F1-20). **Creados el 2026-11-26** tras la aceptación, con los números de «Work items» (arriba). F1-20e se dividió en F1-20e1 (agente y verificación) y F1-20e2 (ejecución, observación, avisos y runbook), como preveía su fila; F1-20h (fase 1a) y F1-20i (verificación en entorno seguro) se añadieron por la decisión 2 y por la condición «implementada, verificada y habilitada de forma expresa». F1-20g es un checklist del propietario, no un work item. Al crearlos se aplica la plantilla `.github/ISSUE_TEMPLATE/work-item.yml`, con la etiqueta `status:blocked` mientras no se cumpla su dependencia. El tamaño es una estimación del redactor en líneas relevantes (tope de `.github/scripts/pr_governance.py`: 800) y se confirma al crear cada issue.

| ID | Objetivo en una línea | Depende de | Tamaño estimado |
| --- | --- | --- | --- |
| F1-20 | El propietario responde las once decisiones y el ADR pasa a `Accepted` (PR que solo toca el ADR y su fila del índice) | — | Solo documentación |
| F1-20a | Un script calcula la clase de despliegue (§2) de un rango `(base, SHA]` leyendo solo el repositorio y falla cerrado; la clase entra en el resumen del run y en la plantilla de PR | F1-20 | ~500 |
| F1-20b | `images.yml` emite atestaciones de procedencia firmadas por imagen propia y el servidor las verifica sin credenciales | F1-20 | ~300 |
| F1-20c | `release.yml` prepara el release (rango, clase, migraciones, digests, huella) y, tras la aprobación en el entorno `production`, deja un registro de despliegue con la tupla de §5 | F1-20a, F1-20b | ~500 |
| F1-20d | `infra/prod/cd-preflight.sh`: las condiciones locales de §7, de solo lectura, para el operador (fase 0) y el agente | F1-20 | ~450 |
| F1-20e | Agente de despliegue del servidor (modo `approve`), marca de parada entre pasos en `deploy.sh`, observación de 30 min, avisos y runbook | F1-20c, F1-20d, F1-20f | ~800; si no cabe, dividir en agente y verificación / `deploy.sh`, observación y avisos |
| F1-20f | Vigilante de la otra aplicación (§8) y lock común del servidor compartido, lado del CRM | F1-20 | ~500 |
| F1-20g | Tarea del propietario, sin cambios en el repositorio: identidades separadas para los agentes, entorno `production`, token del servidor y canal de avisos | F1-20 | Checklist del propietario |

Camino crítico de la fase 1: F1-20 → (F1-20a y F1-20b) → F1-20c, junto con F1-20d y F1-20f → F1-20e. F1-20g puede hacerse en paralelo desde F1-20. La fase 2 no tiene work items todavía: se planifican cuando el propietario cumpla los criterios de §3.1.
