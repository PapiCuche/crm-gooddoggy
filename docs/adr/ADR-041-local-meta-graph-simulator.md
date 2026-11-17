# ADR-041: Simulador local de la Graph API de Meta: excepción de `core.http` solo en desarrollo

- **Status:** Accepted (2026-11-17, decisión del propietario registrada en #779)
- **Date:** 2026-11-17
- **Deciders:** propietario del producto (#779); redactado por el programa autónomo (ADR-015).
- **Work items:** F7b-06c ([#920](https://github.com/PapiCuche/crm-gooddoggy/issues/920))
- **Related:** [ADR-035](ADR-035-instagram-messenger-channels.md) §6 (lo acota **solo** en el stack de desarrollo: los adaptadores salen por `core.http` con IP pública), §1.4 (sin cambios: nada llama a Meta en desarrollo); [ADR-012](ADR-012-engineering-runtime-baseline.md) (sin cambios: urllib3 solo en `core.http`); [ADR-026](ADR-026-test-accounts.md) §1 (el mismo patrón: production no arranca con la variable); ADR-029 §6 y §10 (Proposed); [storage-http.md](../architecture/storage-http.md) B9.

## Context

La prueba en el navegador de la pantalla de canales (#779) tiene que conectar cuentas de Instagram y Messenger, recibir mensajes y responder en el stack local, sin Meta. El simulador de pruebas (`backend/tests/meta_sim`) solo existe dentro de pytest: sustituye la capa de red de `core.http`. Y `core.http` solo sale por https al puerto 443, a un host de la allowlist que resuelva a IP pública (anti-SSRF), así que no puede llegar a un servidor del stack. F7-01 dejó el servidor local fuera a propósito; el propietario autorizó el 2026-11-17 una excepción **solo de desarrollo**.

## Decision

1. **Un ajuste solo local.** `META_GRAPH_SIMULATOR_URL`, un origen `http://<host>:<puerto>` en minúsculas, sin ruta, consulta ni credenciales. Solo `config.settings.local` lo lee (`env.simulator`); en `base` vale `""`. `production`, `migrate` y `test` **no arrancan** si la variable está en el entorno, aunque venga vacía (`env.refuse_local_only`); el error nombra la variable, nunca el valor.
2. **Una sola excepción en `core.http.request`.** Después de toda la comprobación de la URL (`check_url`: https, 443, allowlist con `graph.facebook.com`, sin credenciales ni ambigüedades), si `DEBUG` es verdadero **y** el ajuste no está vacío **y** el host es exactamente `META_GRAPH_HOST`, la petición va a `<origen><ruta>?<consulta>` por un gestor aparte que solo habla http, con los mismos tiempos, reintentos y sin redirecciones. Cualquier otro destino (otro host, el simulador escrito directamente, una IP privada, la descarga de medios de `download`) sigue la política de siempre. Las dos condiciones son independientes: production fija `DEBUG = False` y nunca lee la variable.
3. **El simulador.** `backend/tests/meta_sim/server.py` sirve por http, con la biblioteca estándar, las Graph API falsas de las pruebas (`FakePageGraph` para Messenger e Instagram, `FakeGraph` para WhatsApp) y dos rutas de inspección (`GET /_sim/state`, `POST /_sim/then`). El servicio `meta-sim` existe solo en `infra/docker/compose.yaml`, detrás del perfil `meta-sim`, con el código montado de solo lectura desde `tests/`, sin privilegios y con el sistema de archivos de solo lectura. Nunca está en `infra/prod` ni en una imagen: `backend/.dockerignore` excluye `tests/` y el `Dockerfile` copia solo `config`, `core` y `apps` (prueba estática).
4. **Webhooks firmados.** `backend/tests/meta_sim/send_webhook.py` envía al webhook local un mensaje `page`, `instagram` o `whatsapp` firmado con un secreto de ejemplo, con un `mid` nuevo y la hora actual.
5. **Valores de ejemplo.** El secreto de la app (`metasimdevappsecretnotreal0000`), el token (`meta-sim-test-access-token-not-real`) y los ids son ficticios y evidentes, pasan gitleaks y solo viven en `tests/`, `infra/env/.env.example` y la documentación.

## Alternatives considered

| Alternativa | Por qué no |
| --- | --- |
| Cambiar `META_GRAPH_HOST` y permitir IP privadas para ese host | Abre la política por nombre de host en el mismo camino que usa producción. |
| Sustituir `_manager` desde una app de desarrollo al arrancar | Escondido y difícil de auditar; no se ve en `core.http`. |
| Simulador con TLS, CA propia y DNS falso | Hay que confiar en una CA en la imagen y saltarse igualmente la comprobación de IP pública. |
| Una app de pruebas real de Meta con un túnel | Necesita credenciales reales; ADR-035 §1.4 prohíbe que el desarrollo llame a Meta. |

## Consequences

- El stack local conecta cuentas, recibe mensajes, lee el perfil de Instagram y responde contra el simulador: la prueba en el navegador de #779 es posible.
- La descarga de medios **no** se simula: `download` sigue con la política normal, así que en el stack local un adjunto entrante no se baja.
- El estado del simulador vive en memoria: reiniciarlo lo vacía.

## Security implications

- La excepción exige a la vez `DEBUG` y el ajuste, que solo existe en `local`; production, el job de migraciones y las pruebas se niegan a arrancar con la variable puesta (pruebas en `test_settings`).
- Solo se redirige el host exacto de Graph y solo después de `check_url`: la política de salida no cambia para ningún otro destino (pruebas con el simulador encendido: http, otro puerto, credenciales, sufijos, la IP del simulador, un host de la allowlist que resuelve a loopback y Graph fuera de la allowlist).
- Con el simulador encendido, una llamada a Graph viaja por http dentro de la red del stack. Por eso **nunca** se pega un token real en un stack con `META_GRAPH_SIMULATOR_URL`: iría por http al simulador, que lo guarda en memoria (la Graph API falsa de WhatsApp anota las cabeceras para las pruebas; `/_sim/state` no las enseña).
- El puerto del simulador se publica solo en `127.0.0.1`.

## Operational implications

- Ninguna acción al desplegar. La variable no se pone nunca en producción: si se pone, el servicio no arranca.
- Cómo usarlo: [local-stack.md](../runbooks/local-stack.md) §8.
