# ADR-047: Simulador local del proveedor de IA: solo en desarrollo y CI, nunca en producción

- **Status:** Proposed
- **Date:** 2026-11-22
- **Deciders:** propietario del producto (pendiente); redactado por el programa autónomo (ADR-015) en F8-14 ([#1072](https://github.com/PapiCuche/crm-gooddoggy/issues/1072)).
- **Work items:** F8-09s ([#1061](https://github.com/PapiCuche/crm-gooddoggy/issues/1061)) lo implementaría **solo si el propietario lo acepta**.
- **Related:** [ADR-041](ADR-041-local-meta-graph-simulator.md) (Accepted; este ADR, si se acepta, lo **enmienda solo** en §2, «una sola excepción en `core.http.request`», y no lo edita); [ADR-034](ADR-034-ai-copilot-first.md) §3 («su simulador reproduce cada uno» de los errores) y «Consequences» («CI y desarrollo nunca llaman a un proveedor real»); [ADR-012](ADR-012-engineering-runtime-baseline.md) (sin cambios: urllib3 solo en `core.http`); [ADR-026](ADR-026-test-accounts.md) §1 (el mismo patrón: production no arranca con la variable); [storage-http.md](../architecture/storage-http.md) B9.

> **Mientras este ADR esté `Proposed` no hay ninguna excepción de red nueva:** `core.http` solo admite la de ADR-041 (el host de Graph), y nada de lo que sigue se implementa. Es una propuesta para que el propietario decida.

## Context

- **El hueco.** La prueba en el navegador del panel del copiloto (PR #1060, #1055) necesitó un proveedor de IA simulado en el stack local y usó una herramienta **fuera del repositorio**. No se puede repetir ni revisar desde el código. El runbook de activación lo dice en su §5 ([ai-copilot-activation.md](../runbooks/ai-copilot-activation.md)).
- **Lo que ya existe y no cambia.** Las pruebas de pytest usan `backend/tests/ai_sim.py` (`FakeAnthropic`): una API falsa de Anthropic **sin red** que sustituye solo la capa de red de `core.http` dentro del proceso de pruebas y reproduce cada error documentado (401, 402, 403, el 400 del límite de gasto, el 429 del tope de nivel, 5xx, 529). CI corre esas pruebas con los settings `test`, que se niegan a arrancar con cualquier variable de `LOCAL_ONLY` (`config.env.refuse_local_only`).
- **Por qué no basta.** En el stack local (Compose, settings `local`) el backend llama al proveedor por `core.http.request`, que solo sale por https al puerto 443, a un host de `HTTP_ALLOWED_HOSTS` que resuelva a IP pública (anti-SSRF). Un servidor del stack no cumple nada de eso. ADR-041 resolvió lo mismo para Meta con **una única** excepción, solo para `META_GRAPH_HOST`, y descartó cambiar `_manager` al arrancar (escondido y difícil de auditar).
- **El riesgo de hoy.** El runbook pide no guardar una clave inventada con presupuesto en el stack local, porque la siguiente sugerencia saldría al host **real** del proveedor (`AI_PROVIDER_HOSTS`). Sin simulador, probar el panel de punta a punta empuja a usar una clave real en desarrollo, que ADR-034 prohíbe.

## Decision (propuesta)

1. **Un ajuste solo local.** `AI_PROVIDER_SIMULATOR_URL`: un origen `http://<host>:<puerto>` en minúsculas, sin ruta, consulta ni credenciales, con la misma forma y validación que el de Graph (`config.env.simulator`). Solo `config.settings.local` lo lee; en `base` vale `""`. Se añade a `config.env.LOCAL_ONLY`: **`production`, `migrate` y `test` no arrancan** si la variable está en el entorno, aunque venga vacía; el error nombra la variable, nunca el valor.
2. **La excepción de ADR-041, ampliada a un segundo host exacto.** En `core.http.request`, después de toda la comprobación de la URL (`check_url`: https, 443, allowlist, sin credenciales ni ambigüedades), si `DEBUG` es verdadero **y** el ajuste no está vacío **y** el host es exactamente uno de `AI_PROVIDER_HOSTS` (hoy solo `api.anthropic.com`), la petición va a `<origen><ruta>?<consulta>` por el gestor aparte que ya existe para Graph (solo http, los mismos tiempos y reintentos, sin redirecciones). La tabla host → ajuste es fija en `core.http` (dos entradas: Graph y el proveedor de IA); cualquier otro destino sigue la política de siempre. Las dos condiciones son independientes: production fija `DEBUG = False` y nunca lee la variable.
3. **El simulador.** Un servidor http con la biblioteca estándar en `backend/tests/` que sirve `GET /v1/models` y `POST /v1/messages` reutilizando `FakeAnthropic` de `tests/ai_sim.py`: respuestas de texto, turnos con `tool_use`, la salida estructurada del copiloto y cada error documentado, elegidos por guion. El servicio `ai-sim` existe solo en `infra/docker/compose.yaml`, detrás del perfil `ai-sim`, con el código montado de solo lectura desde `tests/`, sin privilegios, con el sistema de archivos de solo lectura y el puerto publicado solo en `127.0.0.1`. **Nunca** está en `infra/prod` ni en una imagen: `backend/.dockerignore` ya excluye `tests/` y el `Dockerfile` copia solo `config`, `core` y `apps` (prueba estática, como la de ADR-041).
4. **El simulador no guarda la clave ni el contenido.** Descarta la cabecera `x-api-key` al recibirla (a diferencia de la Graph API falsa de WhatsApp, que guarda cabeceras), no escribe en un log el cuerpo de las peticiones y su estado de inspección solo cuenta peticiones por ruta y el guion en curso.
5. **Valores de ejemplo.** La clave del stack local es ficticia y evidente (como `TEST_KEY` de `tests/ai_sim.py`), pasa gitleaks y solo vive en `tests/`, `infra/env/.env.example` y la documentación.
6. **Lo que pasa por el simulador.** Todo lo que el backend manda al host del proveedor: la prueba de conexión (`apps.ai_gateway.probe`, `GET /v1/models`), sugerir y resumir (`apps.ai_gateway.anthropic`, `POST /v1/messages`). Lo demás del copiloto (presupuesto, interruptores, `ai_runs`, auditoría, exclusión) corre igual que en producción: el simulador solo sustituye al proveedor.
7. **CI.** Las pruebas de pytest siguen con `tests/ai_sim.py`, sin red y sin esta excepción (los settings `test` no arrancan con la variable). Hoy no existe un job de CI que levante el stack de Compose; si el propietario lo quiere (pregunta 2), ese job usaría los settings `local` y este mismo perfil, nunca los de producción.

## Alternatives considered

| Alternativa                                                                         | Por qué no (o por qué se pregunta)                                                                                                                                                                                                                                         |
| ----------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Dejarlo como hoy**: la herramienta externa del PR #1060                           | No es reproducible ni revisable desde el repositorio; cada E2E del copiloto depende de algo que nadie ve.                                                                                                                                                                  |
| **Un adaptador falso elegido por un ajuste** (`AI_ADAPTER = "sim"` solo en `local`) | No toca la red: es la alternativa más fuerte y se pregunta al propietario (pregunta 1). Pero deja en el código de producción un camino que elige un adaptador falso, no prueba `core.http` (allowlist, plazos, reintentos) y se ve menos que una excepción en `core.http`. |
| Cambiar `AI_PROVIDER_HOSTS` en `local` y permitir IP privadas para ese host         | Abre la política por nombre de host en el mismo camino que usa producción (descartada por lo mismo en ADR-041).                                                                                                                                                            |
| Sustituir `_manager` desde una app de desarrollo al arrancar                        | Escondido y difícil de auditar; ADR-041 ya lo descartó.                                                                                                                                                                                                                    |
| Simulador con TLS, CA propia y DNS falso                                            | Hay que confiar en una CA en la imagen y saltarse igualmente la comprobación de IP pública.                                                                                                                                                                                |
| Llamar al proveedor real con una clave de pruebas                                   | ADR-034 lo prohíbe en CI y en desarrollo; además cuesta dinero y saca datos del stack.                                                                                                                                                                                     |

## Consequences

- El stack local recorre el copiloto de punta a punta sin proveedor real: «Probar conexión», sugerir, insertar, resumir, el traspaso, los errores del proveedor y el presupuesto agotado. La E2E del panel deja de depender de una herramienta externa.
- `core.http` pasa de una excepción a dos, en la misma función y con las mismas condiciones; ADR-041 sigue `Accepted` y el índice dice que este ADR lo enmienda en §2.
- Un segundo proveedor (OpenAI, F8-10) añadiría su host a `AI_PROVIDER_HOSTS`; si entra en esta excepción sin otro ADR es la pregunta 5.

## Security implications

- **Fuera de producción por dos condiciones independientes:** `DEBUG` y un ajuste que solo existe en `local`. `production`, `migrate` y `test` se niegan a arrancar con la variable puesta (las pruebas de `test_settings` se amplían a la nueva variable). La prueba que exige #1061: production nunca apunta al simulador.
- **Solo el host exacto del proveedor y solo después de `check_url`.** Pruebas con el simulador encendido, como las de ADR-041: http, otro puerto, credenciales, sufijos, la IP del simulador escrita directamente, un host de la allowlist que resuelve a loopback y el proveedor fuera de la allowlist siguen rechazados.
- **La clave del stack viaja por http dentro de la red de Compose.** Por eso: el simulador la descarta (punto 4) y **nunca** se enciende contra una base que ya guarde una clave real de un proveedor (la prueba de conexión y el copiloto la usarían), ni se pega una real en ese stack. El texto de las conversaciones de desarrollo (ya enmascarado por `context.mask`) también viaja al simulador; por eso no se escribe en ningún log.
- **El nombre del servicio.** Con la variable puesta y el perfil `ai-sim` apagado, el nombre `ai-sim` lo resuelve el resolvedor del host, incluidos sus dominios de búsqueda: otra máquina con ese nombre recibiría las peticiones. Encender la variable sin el perfil es un error de configuración (el runbook lo dirá, como dice el de Graph).
- No debilita ADR-001, ADR-002, ADR-022 ni ADR-034: la clave sigue sellada, el gateway sigue comprobando interruptores, exclusión y presupuesto antes de llamar.

## Operational implications

- Ninguna acción al desplegar. La variable no se pone nunca en producción: si se pone, el servicio no arranca.
- Si se acepta: [local-stack.md](../runbooks/local-stack.md) añade la sección del perfil `ai-sim` y [ai-copilot-activation.md](../runbooks/ai-copilot-activation.md) §5 deja de decir que no hay simulador.

## Preguntas para el propietario

1. **¿Se acepta ampliar la excepción de red de ADR-041 al host del proveedor de IA** (recomendación de este ADR), **o se prefiere el adaptador falso elegido por un ajuste**, que no toca la red pero deja ese camino en el código de producción?
2. **¿Solo el stack de desarrollo, o también un job de CI** que levante el stack de Compose con el simulador para la E2E del copiloto? Hoy CI no levanta el stack; el job sería nuevo y con los settings `local`.
3. **¿Guiones fijos o control desde la prueba?** Recomendación: guiones fijos más una ruta de inspección `POST /_sim/then` para elegir la siguiente respuesta (como el simulador de Graph), solo en el perfil `ai-sim`.
4. **¿«Probar conexión» también va al simulador?** Recomendación: sí; es el mismo host y así la pantalla de la IA se prueba entera.
5. **¿Un proveedor nuevo entra en esta excepción sin otro ADR?** Recomendación: no; cada host nuevo de `AI_PROVIDER_HOSTS` en la excepción pide su propia decisión.
