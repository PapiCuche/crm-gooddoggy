# Fase 7 — Canales reales: WhatsApp primero

- **Estado:** diseño propuesto el 2026-11-06 (F7-00). Ningún work item está empezado. [ADR-029](../adr/ADR-029-whatsapp-cloud-api-channel.md) está `Proposed`: hasta que el propietario lo acepte (o lo cambie), este documento es un plan y no un compromiso.
- **Objetivo:** que el Inbox hable con clientes reales por **WhatsApp Business Cloud API** (la API oficial de Meta), y después por Instagram, Messenger, correo, chat web y TikTok según lo que cada plataforma permita de forma oficial. Se construye y se prueba todo contra simulaciones; conectar un número real exige credenciales que solo el propietario puede conseguir.
- **Origen:** [roadmap §S.1](../fase-0/05-backlog-y-roadmap.md) (fila «Fase 7»), historias E07 del [backlog §R](../fase-0/05-backlog-y-roadmap.md), [modelo de datos §E.6 y §E.12](../fase-0/02-modelo-de-datos.md), [04 §Q](../fase-0/04-precios-pipeline-seguridad-infra.md), [phase-6.md](phase-6.md), ADR-002 §3.3, ADR-007, ADR-008, ADR-010, ADR-012, ADR-022 y ADR-029.

Los issues llevan la etiqueta `phase:f7`. Los work items de abajo son un orden y un tamaño; cada issue se crea al empezar su trabajo (ADR-015 §1) y **el issue es la fuente de verdad**.

## Principios

- **Solo vías oficiales.** Nada de scraping, sesiones personales, WhatsApp Web ni APIs no autorizadas, en ningún canal.
- **Simulación primero.** Cada pieza se prueba contra un servidor simulado de Meta que firma sus propios webhooks y responde con cada error conocido. CI y desarrollo no usan la red de Meta. Las credenciales reales solo intervienen en F7-12.
- **El dominio no cambia.** Conversaciones, mensajes, asignación y estados siguen escritos por `apps.conversations.commands`; el canal solo traduce.
- **Lo que viene de fuera es dato no confiable:** firma antes de parsear, tamaños acotados, texto saneado, tenant por la función de enrutado y nunca por el payload.
- **Lo que no se entiende se ve.** Un mensaje de un tipo que el primer corte no soporta aparece en el chat como «mensaje no soportado»; no se descarta en silencio.
- **Cada migración corresponde a funcionalidad real.** No se crean tablas que ningún work item use todavía.
- **Cada work item cabe en 800 líneas relevantes** (ADR-009); si un corte no cabe, se divide antes de empezar.

## Estado de partida (lo que la Fase 6 ya dejó)

El Inbox completo con el canal Sandbox: cuentas de canal, conversaciones, mensajes, asignación, notas, lista y chat, envío en cola con el outbox, política mínima, despachador con reintentos y barredor, y el `ChannelAdapter` que esta fase implementa de verdad. Detalle y lo que falta: ADR-029, «Lo que ya está decidido o construido».

Avisos heredados de las revisiones de la Fase 6 que esta fase tiene que cerrar: el bloqueo de la cuenta serializa los envíos de esa cuenta (con HTTP real hay que reclamar el mensaje y llamar fuera de la transacción, F7-05); el barredor amplifica los reintentos (F7-05); el adaptador debe sanear el texto del proveedor, incluidos los caracteres Tags (F7-04); `contact_consents` no se construyó en la Fase 4 (F7-08); un mensaje encolado 120 minutos queda `FAILED` y la pantalla debe explicarlo.

## Work items

| ID    | Título                                                                                  | Rama                             | Depende de                      | Tipo               |
| ----- | --------------------------------------------------------------------------------------- | -------------------------------- | ------------------------------- | ------------------ |
| F7-01 | Simulador de la API de Meta (webhooks firmados y Graph API falsa)                       | `feature/f7-meta-simulator`      | —                               | backend (pruebas)  |
| F7-02 | Cuenta de canal de WhatsApp y secretos cifrados                                         | `feature/f7-whatsapp-account`    | F7-01                           | backend            |
| F7-03 | Webhook: verificación, firma, ingesta y enrutado del tenant                             | `feature/f7-webhook-ingress`     | F7-01, F7-02                    | backend            |
| F7-04 | Mensajes entrantes de texto y marcador de no soportados                                 | `feature/f7-inbound-text`        | F7-03                           | backend            |
| F7-05 | Adaptador de salida, política de ventana y despacho con reclamo                         | `feature/f7-outbound-adapter`    | F7-02, F7-04                    | backend            |
| F7-06 | Estados de entrega y errores del proveedor                                              | `feature/f7-delivery-status`     | F7-05                           | backend            |
| F7-07 | Pantalla de configuración del canal y estado de salud                                   | `feature/f7-channel-settings-ui` | F7-02, F7-06                    | frontend           |
| F7-08 | Consentimientos y baja (STOP)                                                           | `feature/f7-consent-optout`      | F7-04                           | backend            |
| F7-09 | Medios entrantes y salientes                                                            | `feature/f7-media`               | F7-06                           | full-stack         |
| F7-10 | Plantillas: sincronización, estado y envío fuera de la ventana                          | `feature/f7-templates`           | F7-06, F7-08                    | full-stack         |
| F7-11 | Reintento de webhooks fallidos, métricas y alertas                                      | `feature/f7-ingress-ops`         | F7-06                           | full-stack         |
| F7-12 | Activación en vivo con credenciales del propietario (runbook y prueba de punta a punta) | `feature/f7-go-live`             | F7-07, F7-11 y las credenciales | docs / operaciones |

Orden de entrega recomendado: F7-01 → F7-02 → F7-03 → F7-04 → F7-05 → F7-06; después F7-07, F7-08 y F7-11 en paralelo; luego F7-09 y F7-10; F7-12 cuando haya credenciales. Tras F7-06 el producto ya recibe y responde texto dentro de la ventana contra el simulador.

### F7-01 · Simulador de la API de Meta

- **Incluye:** un módulo de pruebas del repositorio que (a) construye los cuerpos de los webhooks de Meta (texto, estados `sent`, `delivered`, `read` y `failed` con errores, tipos no soportados, eventos mal formados, varios ítems en una entrega) y los firma con un app secret de prueba; (b) una Graph API falsa (en proceso para los tests y como servicio ligero para el entorno local) que responde a `POST …/<phone_number_id>/messages`, a la lectura del número y, después, a plantillas y medios, con respuestas correctas, cada error de ADR-029 §8, lentitud y cortes; (c) fixtures versionadas junto a la versión fijada de la Graph API.
- **No incluye:** el webhook ni el adaptador (los usan después); nada de red real.
- **Criterios:** los tests de F7-02 a F7-06 usan este simulador; cambiar la versión de la Graph API obliga a revisar sus fixtures.
- **Tamaño estimado:** 300–450 líneas.

### F7-02 · Cuenta de canal de WhatsApp y secretos cifrados

- **Incluye:** plataforma `WHATSAPP`; columnas de la cuenta (`external_parent_id`, `credential_id`, `status_detail`, `quality_rating`, `messaging_limit_tier`, `last_healthcheck_at`); tabla `credentials` (tenant, RLS forzada, cifrado con `core.sealed`, propósito `channel.access_token`, `last_four`, `fingerprint`, `key_version`, `expires_at`, `rotated_at`) y su tarea de re-cifrado con la KEK nueva; comandos y rutas (`channels.manage`) para conectar una cuenta (valida el token contra la Graph API simulada, guarda `PENDING`), rotar el token y desconectar; inmutabilidad de `platform` y los ids externos fuera de `PENDING`; auditoría sin valores; ajustes `META_APP_SECRETS`, `META_WEBHOOK_VERIFY_TOKEN`, `META_GRAPH_VERSION` y la allowlist con `graph.facebook.com`; el redactor de registros ampliado con las claves nuevas.
- **No incluye:** el webhook, el envío, la pantalla.
- **Criterios:** ninguna respuesta, registro ni auditoría contiene un token (test que busca el valor en todas las salidas); un token cifrado copiado a otra cuenta no se abre; una cuenta real nunca nace `CONNECTED`; mutaciones en permisos, cifrado y estado.
- **Tamaño estimado:** 600–780 líneas (si no cabe, `credentials` va aparte como F7-02a).

### F7-03 · Webhook: verificación, firma, ingesta y enrutado del tenant

- **Incluye:** ruta pública `/webhooks/meta/` (GET y POST) fuera del espacio de tenant, sin sesión ni CSRF; verificación del GET en tiempo constante; POST con lectura cruda acotada, firma `X-Hub-Signature-256` contra la lista de app secrets, rechazo uniforme sin detalle, validación del esquema, división en una fila por ítem lógico; `webhook_ingress` y `webhook_failures` (de plataforma, privilegios mínimos, purga a 90 días); `process_webhook_ingress` (`@platform_task`) con la función `SECURITY DEFINER` `resolve_channel_account` (con las garantías de ADR-002 §3.3 y su prueba de `pg_temp`), contraste con el WABA, estados `RECEIVED/PROCESSING/PROCESSED/IGNORED/FAILED/DEAD`, espera creciente; el procesador de esta pieza solo enruta y marca (un ítem enrutado queda `PROCESSED` sin efecto hasta F7-04).
- **No incluye:** crear mensajes ni contactos (F7-04), estados de entrega (F7-06), el panel de reintento (F7-11).
- **Criterios:** firma inválida, ausente o mal formada = 401 uniforme; cuerpo mayor al tope = 413; el mismo evento dos veces = una fila; un payload con `organization_id` falso se ignora (prueba T11); un número desconocido queda `IGNORED`; el verify token y el app secret no aparecen en ninguna salida; mutaciones en firma, comparación, enrutado y deduplicación. Reglas de la ruta en el auditor de URL.
- **Tamaño estimado:** 650–800 líneas (si no cabe: F7-03a verificación y firma, F7-03b ingesta y enrutado).

### F7-04 · Mensajes entrantes de texto y marcador de no soportados

- **Incluye:** el procesador convierte un ítem `message` en contacto (por teléfono E.164), conversación y mensaje con los comandos de F6-01c, idempotente; saneado del texto del proveedor (controles, formato, dirección, sustitutos, Tags) y recorte a 4096; `message_type = UNSUPPORTED` con etiqueta fija para imagen, audio, video, documento, sticker, ubicación, contactos, interactivos y desconocidos, y las reacciones ignoradas con métrica; `provider_timestamp` y el ancla de la ventana de 24 h; un evento antiguo no reabre una conversación cerrada; cuentas `DISCONNECTED`/`ERROR` registran y `PENDING` ignora; evento `message.received` al outbox solo con ids.
- **No incluye:** medios (F7-09), consentimiento (F7-08), estados (F7-06).
- **Criterios:** dos entregas del mismo mensaje = un mensaje; dos mensajes simultáneos de un contacto nuevo = un contacto y una conversación; texto hostil saneado o `UNSUPPORTED`; otra organización no se toca; la pantalla del chat ya muestra el marcador.
- **Tamaño estimado:** 500–700 líneas.

### F7-05 · Adaptador de salida, política de ventana y despacho con reclamo

- **Incluye:** `WhatsAppAdapter` por `core.http.request` (versión fijada, token descifrado solo en la llamada, `biz_opaque_callback_data`); cambio del despachador a «reclamar en `SENDING` y llamar fuera de la transacción» con bloqueo compartido de la cuenta; resultado ambiguo = reconciliación por el webhook o `FAILED` con `PROVIDER_UNKNOWN` tras el plazo; limitador por cuenta (Redis); barredor que no amplifica reintentos; `MessagingPolicyService` mínimo (ADR-010: nota interna, cuenta desconectada, contacto bloqueado y **ventana** recalculada desde `last_inbound_at` con las capacidades del adaptador) con `PolicyDecision` guardada en el mensaje y re-evaluada justo antes de enviar; `ChannelCapabilities`; ruta de consulta de la política para la pantalla.
- **No incluye:** plantillas (F7-10), medios (F7-09), estados que llegan por webhook (F7-06).
- **Criterios:** una respuesta dentro de la ventana sale por el simulador y queda `SENT`; fuera de la ventana queda `FAILED` con `WINDOW_CLOSED`; un tiempo agotado no reenvía a ciegas; dos despachadores a la vez envían una vez (`race`); un token inválido pone la cuenta en `ERROR`; el token no aparece en ninguna salida; mutaciones en el reclamo, la ventana y el plazo.
- **Tamaño estimado:** 700–800 líneas (si no cabe: F7-05a política y capacidades, F7-05b adaptador y reclamo).

### F7-06 · Estados de entrega y errores del proveedor

- **Incluye:** ítems `status` (`sent`, `delivered`, `read`, `failed`) → `advance_delivery` por `external_message_id` o por `biz_opaque_callback_data`; reintento transitorio si el estado llega antes de la confirmación del envío; `provider_error_code` con la tabla cerrada de ADR-029 §8 y los textos en español; `message_status_events` solo si una pantalla lo pide; calidad de la cuenta (`DEGRADED`) y aviso; la pantalla del chat muestra el motivo de un `FAILED`.
- **No incluye:** plantillas ni medios.
- **Criterios:** estados fuera de orden no retroceden; `failed` es terminal; un estado de otra organización no existe; cada código del simulador produce su efecto; mutaciones en el avance y el mapeo.
- **Tamaño estimado:** 450–650 líneas.

### F7-07 · Pantalla de configuración del canal y estado de salud

- **Incluye:** en Ajustes, la cuenta de WhatsApp: formulario para conectar (WABA, `phone_number_id`, token), el estado, el checklist de lo que falta (webhook visto, token válido, calidad), la rotación del token, la desconexión y la salud (calidad, límite de mensajería, último chequeo) con la tarea periódica `HEALTHCHECK`; ninguna pantalla muestra un secreto (solo los últimos cuatro caracteres); permisos con `channels.manage`; recuadro de «prueba de webhook».
- **No incluye:** plantillas ni medios.
- **Criterios:** pruebas del formulario y de los estados; 390 y 320 px; pruebas en el navegador contra el simulador.
- **Tamaño estimado:** 500–700 líneas.

### F7-08 · Consentimientos y baja (STOP)

- **Incluye:** `contact_consents` y su historial inmutable con el comando que los escribe (ADR-010, `02` §E.5); palabras de baja y de alta configurables por organización; el entrante que es una palabra de baja registra la revocación y envía una confirmación fija; el servicio de política bloquea lo proactivo a quien se dio de baja y permite las respuestas dentro de la ventana; `contacts.is_blocked`; vista del consentimiento en la ficha 360°.
- **No incluye:** marketing masivo.
- **Criterios:** una baja bloquea un seguimiento y no una respuesta dentro de la ventana; el historial no se edita; mutaciones.
- **Tamaño estimado:** 600–780 líneas (si no cabe, tabla y comandos aparte de la política).

### F7-09 · Medios entrantes y salientes

- **Incluye:** `message_attachments`; descarga acotada y en flujo con extensión de `core.http`, hosts de media en la allowlist, tipo real por magic bytes, `sha256`, almacén de objetos (`message-media`), `scan_status`; estados `PENDING/DONE/FAILED/EXPIRED`; el chat muestra imagen, audio y documento con descarga controlada (nunca la URL de Meta); envío de imagen y documento por el adaptador; retención configurable (D-CH-4).
- **No incluye:** transcripción de audio (Fase 8), antivirus real si aún no existe (queda `PENDING` y la pantalla lo respeta, D-F7-04).
- **Criterios:** un medio que caduca antes de bajarse queda `EXPIRED`; un archivo mayor al tope se rechaza; un tipo no permitido no se sirve; mutaciones.
- **Tamaño estimado:** 700–800 líneas (dividir en entrantes y salientes si no cabe).

### F7-10 · Plantillas: sincronización, estado y envío fuera de la ventana

- **Incluye:** `message_templates`, sincronización con la Graph API (`sync_jobs`), estados y alerta de plantilla pausada o rechazada; `TEMPLATE_REQUIRED` en la política con la plantilla aprobada por propósito e idioma; envío de plantilla con variables validadas; pantalla de plantillas y selector en el cuadro de redacción cuando la ventana está cerrada.
- **No incluye:** crear plantillas en Meta desde el CRM (se piden en el Business Manager); marketing.
- **Criterios:** fuera de la ventana con plantilla aprobada = sale como plantilla; sin plantilla = `BLOCKED`; mutaciones.
- **Tamaño estimado:** 700–800 líneas (dividir sincronización y envío si no cabe).

### F7-11 · Reintento de webhooks fallidos, métricas y alertas

- **Incluye:** panel de ingesta (`integrations.manage`): listar, ver y reintentar ítems `FAILED` y `DEAD` (auditado); métricas de ADR-029 (firmas inválidas, lag, `FAILED/DEAD`, envíos por código, calidad); alertas mínimas antes de producción (ADR-011): lag, `DEAD`, firmas inválidas en ráfaga, token inválido.
- **Criterios:** un reintento reprocesa una vez y es idempotente; permisos; sin datos personales en las métricas.
- **Tamaño estimado:** 500–700 líneas.

### F7-12 · Activación en vivo con credenciales del propietario

- **Incluye:** el runbook de activación y de incidentes en `docs/runbooks/`; la lista de variables de producción; la prueba de punta a punta con el **número de prueba de Meta** (sin tocar clientes) y después con el número del piloto; revisión de la política de privacidad y del aviso al cliente.
- **No incluye:** código nuevo, salvo ajustes que la prueba real descubra.
- **Criterios:** un mensaje entra, se responde dentro de la ventana, se ven `sent/delivered/read`, una baja funciona, el webhook sobrevive a un reinicio sin perder ni duplicar eventos; el propietario firma el checklist.

## Qué necesita el propietario (sin ningún valor real)

Nada de esto bloquea el desarrollo: F7-01 a F7-11 se construyen y prueban con el simulador. Hace falta para **F7-12**, y los trámites con Meta tardan, por lo que conviene empezarlos ya.

| #   | Qué                                                                                                                                                                                                            | Para qué                                                                                   | Cuándo se necesita                             |
| --- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------ | ---------------------------------------------- |
| 1   | Cuenta de **Meta Business** (Business Manager / Business Suite) del negocio, con la **verificación del negocio** (documentos legales del negocio; el plazo lo fija Meta)                                       | Subir los límites de mensajería, mostrar el nombre del negocio y usar la API en producción | Antes de F7-12; empezar ya                     |
| 2   | **App de Meta** (tipo Business) con el producto **WhatsApp**, en modo **Live**, con la política de privacidad y los datos de la app                                                                            | Es la app que recibe los webhooks y firma con su app secret                                | Antes de F7-12                                 |
| 3   | **WABA** (cuenta de WhatsApp Business) y un **número de teléfono**: nuevo, o migrado, o en coexistencia con la app de WhatsApp Business (D-F7-02), con su **nombre visible aprobado** y el registro del número | El canal en sí                                                                             | Antes de F7-12                                 |
| 4   | **Usuario del sistema** del Business Manager, con los activos asignados, y su **token permanente** con los permisos de mensajería y de administración de WhatsApp                                              | Es el token de acceso que se guarda cifrado por cuenta                                     | F7-02 (con el simulador no hace falta) y F7-12 |
| 5   | **App secret** de la app (Configuración de la app → Básica)                                                                                                                                                    | Verificar la firma de los webhooks (`META_APP_SECRETS`)                                    | F7-12                                          |
| 6   | **Verify token**: una cadena aleatoria larga que elige quien despliega y que se escribe en Meta y en el entorno (`META_WEBHOOK_VERIFY_TOKEN`)                                                                  | Verificar la suscripción del webhook                                                       | F7-12                                          |
| 7   | **URL pública HTTPS** con certificado válido (no autofirmado) hacia `/webhooks/meta/`, del hosting elegido (D10-H)                                                                                             | Meta solo entrega webhooks a una URL pública HTTPS                                         | F7-12 (y un túnel de pruebas antes)            |
| 8   | **Suscripción** de la app al campo `messages` del WABA                                                                                                                                                         | Recibir mensajes y estados                                                                 | F7-12                                          |
| 9   | **Método de pago** en el WABA                                                                                                                                                                                  | Plantillas y mensajes que Meta cobra                                                       | F7-10 en vivo                                  |
| 10  | **Plantillas** que se quieren usar (seguimiento, cotización, reenganche), pedidas y aprobadas en Meta                                                                                                          | Escribir fuera de la ventana de 24 h                                                       | F7-10 en vivo                                  |
| 11  | **Aviso de privacidad y términos** para los clientes que escriben por WhatsApp, y la lista de palabras de baja                                                                                                 | Base legal y baja                                                                          | Antes de F7-12                                 |
| 12  | El **número de prueba** que Meta ofrece y la lista de destinatarios de prueba                                                                                                                                  | Probar de punta a punta sin clientes                                                       | F7-12                                          |

Los valores (tokens, secretos, ids) se entregan **solo** por el gestor de secretos o las variables de entorno del despliegue; nunca en el repositorio, en un issue ni en un chat.

## Decisiones abiertas para el propietario

Cada una trae una recomendación; no se programa nada que dependa de ella antes de la respuesta.

| ID      | Pregunta                                                                                                                                        | Recomendación                                                                                                   | Bloquea      |
| ------- | ----------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------- | ------------ |
| D-F7-01 | ¿Una app de Meta por despliegue (la del propietario) o apps propias por organización (proveedor de tecnología)?                                 | Una app por despliegue: el app secret va en el entorno y la firma se verifica antes de conocer la organización  | F7-02, F7-03 |
| D-F7-02 | Número del piloto (D-CH-3): ¿uno nuevo, migrar el actual, o coexistir con la app de WhatsApp Business si Meta lo permite para ese número?       | Un número nuevo para el piloto; decidir la migración con calma                                                  | F7-12        |
| D-F7-03 | Hosting y URL pública HTTPS (D10-H)                                                                                                             | Decidir antes de F7-12; mientras, un túnel solo para pruebas                                                    | F7-12        |
| D-F7-04 | ¿Se muestran medios cuyo antivirus sigue `PENDING`?                                                                                             | No: solo `CLEAN`, salvo decisión expresa; el antivirus (ClamAV) se hace antes de abrir medios a clientes reales | F7-09        |
| D-F7-05 | Palabras de baja y de alta, y qué se confirma al cliente                                                                                        | `STOP`, `BAJA`, `CANCELAR` y `ALTA`; confirmación fija en español                                               | F7-08        |
| D-F7-06 | Retención de `webhook_ingress` y de la media (D-CH-4)                                                                                           | 90 días para la ingesta; la media, según la política del negocio                                                | F7-03, F7-09 |
| D-F7-07 | ¿Qué hacer con un número desconocido que escribe?                                                                                               | Crear el contacto con el nombre del perfil y origen WhatsApp, y asignarlo a la cola                             | F7-04        |
| D-F7-08 | Política de la versión de la Graph API                                                                                                          | Fijada en el código; revisión trimestral; se sube en un cambio revisado                                         | F7-05        |
| D-F7-09 | Orden y calendario de las demás redes (ver abajo) frente al roadmap de la fase 0, que pone Instagram y Facebook en la Fase 12 y TikTok en la 13 | Mantener WhatsApp primero y decidir el orden de las demás con lo que cada plataforma apruebe                    | Fase 7b      |
| D-F7-10 | ¿Varios números por organización desde el primer corte (D-CH-2)?                                                                                | El modelo lo admite; la pantalla maneja uno                                                                     | F7-07        |

## Las demás redes (Fase 7b en adelante)

Resumen de capacidades **oficiales** conocidas a la fecha de este diseño. Cada plataforma cambia sus reglas, planes y requisitos: **antes de empezar cada una hay que confirmar** la documentación oficial vigente y, si no se puede cumplir, el canal se aplaza o se descarta, nunca se sustituye por una vía no autorizada.

| Canal                             | Qué ofrece la vía oficial                                                                                                                           | Reglas que importan                                                                                                                                        | Bloqueos probables                                                                                                                                                               |
| --------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Instagram (mensajes directos)** | API de mensajería de Instagram para cuentas profesionales, con el mismo mecanismo de webhooks de Meta                                               | Ventana de 24 h para responder, ampliable a una semana con la etiqueta de agente humano solo para personas; identidad por id de Instagram, no por teléfono | Revisión de la app por Meta y permisos de mensajería avanzados; cuenta profesional vinculada a una página; contactos sin teléfono → `contact_identities` y fusión de identidades |
| **Messenger**                     | Plataforma de Messenger para páginas de Facebook, mismos webhooks y firma                                                                           | Ventana de 24 h y etiqueta de agente humano; identidad por id de página                                                                                    | Revisión de la app y permisos; página administrada por el negocio                                                                                                                |
| **Correo**                        | Envío ya resuelto por SMTP (ADR-019); recepción por un buzón que el propietario autorice (IMAP) o por el reenvío entrante de un proveedor de correo | Sin ventana; hilos por cabeceras; SPF, DKIM y DMARC del dominio                                                                                            | Un dominio y un buzón del negocio; decisión sobre el proveedor de correo; adjuntos con antivirus                                                                                 |
| **Chat web**                      | Un widget propio del CRM en la tienda, sin terceros                                                                                                 | Sin ventana; identidad del visitante por una cookie propia hasta que deje datos                                                                            | Protección contra abuso (límite de tasa, captcha), CORS y política de privacidad; solo depende de nosotros                                                                       |
| **TikTok**                        | Mensajería empresarial de TikTok donde exista acceso oficial                                                                                        | Reglas y disponibilidad propias, a menudo por región o por aprobación de socio                                                                             | Disponibilidad y aprobación en Perú sin confirmar (E19-01 del backlog es la investigación); sin confirmación, no se construye                                                    |

Para cada una, el patrón es el de WhatsApp: adaptador detrás de `ChannelAdapter`, webhook firmado con `webhook_ingress`, enrutado por la cuenta, capacidades en `MessagingPolicyService`, simulador para las pruebas y activación por cuenta con un checklist. El orden que pide el propietario es Instagram, Messenger, correo, chat web y TikTok; D-F7-09 lo contrasta con el roadmap.
