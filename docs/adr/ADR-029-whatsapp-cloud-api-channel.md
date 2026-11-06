# ADR-029: Canal de WhatsApp con la API oficial de Meta (WhatsApp Business Cloud API)

- **Status:** Proposed
- **Date:** 2026-11-06
- **Deciders:** propuesto por el programa autónomo (ADR-015 §5) para la decisión del propietario. Mientras esté `Proposed` no obliga al código; las preguntas que necesitan respuesta del propietario están en §14 y, con su número, en [phase-7.md](../phases/phase-7.md).
- **Related:** ADR-001, ADR-002 §3.3, ADR-003 §3, ADR-007, ADR-008, ADR-010, ADR-011, ADR-012, ADR-016, ADR-022, `docs/architecture/tenancy-context.md` §5, `docs/architecture/outbox-audit.md`, `docs/architecture/storage-http.md`, `docs/fase-0/02-modelo-de-datos.md` §E.5, §E.6 y §E.12, `docs/fase-0/03-tenancy-rbac-inbox-ia.md` §I y §J, `docs/fase-0/04-precios-pipeline-seguridad-infra.md` §Q, `docs/fase-0/06-riesgos-y-decisiones.md` (D-CH-1 a D-CH-4, D10-H), [phase-6.md](../phases/phase-6.md)

## Context

El propietario quiere que el Inbox hable con clientes reales, **primero por WhatsApp y con la API oficial**, y después por Instagram, Messenger, correo, chat web y TikTok según lo que cada plataforma permita oficialmente. Quedan fuera, sin excepción: el scraping, las sesiones personales (WhatsApp Web, cuentas personales automatizadas), las bibliotecas no oficiales y cualquier API no autorizada. Todavía no hay credenciales de Meta: el canal se construye y se prueba entero contra simulaciones, y activarlo con un número real exigirá lo que el propietario consiga (§13).

La Fase 6 dejó un Inbox completo con el canal Sandbox. Antes de proponer nada se revisó lo que ya existe, para no duplicarlo ni contradecirlo.

### Lo que ya está decidido o construido, y este ADR no cambia

| Tema                                                                                                                                                                                                                                         | Dónde                                                                           | Estado                                                                                               |
| -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------- |
| Cloud API directa, sin proveedor intermedio (BSP)                                                                                                                                                                                            | D-CH-1 en `06-riesgos-y-decisiones.md`                                          | Recomendación de la fase 0; este ADR la adopta                                                       |
| Cuentas de canal: `external_account_id` único entre las vivas de toda la plataforma, para enrutar el webhook                                                                                                                                 | `02` §E.6; `channel_accounts` y su índice único en `apps.conversations` (F6-01) | Construido                                                                                           |
| Credenciales cifradas en una tabla `credentials` (`owner_type`, `ciphertext`, `key_version`, `last_four`, `fingerprint`, `expires_at`)                                                                                                       | `02` §E.12; ADR-022                                                             | Diseñado; la tabla **no existe todavía**                                                             |
| Cifrado de secretos: AES-256-GCM en un único módulo, KEK por entorno con versión, propósito y dueño atados                                                                                                                                   | ADR-022; `core.sealed`                                                          | Construido (hoy solo lo usa MFA)                                                                     |
| Webhook: firma HMAC sobre el cuerpo crudo, ACK rápido, una fila de ingesta por ítem lógico, clave de deduplicación, reintentos con espera creciente, retención de 90 días                                                                    | `04` §Q; `tenancy-context.md` §5                                                | Diseñado; no hay ruta, ni `webhook_ingress`, ni procesador                                           |
| Enrutado del tenant del webhook por una función `SECURITY DEFINER` mínima, nunca por el payload                                                                                                                                              | ADR-002 §3.3; `tenancy-context.md` §5 y prueba T11                              | Diseñado; la función no existe                                                                       |
| Política de mensajería (ventana de 24 h, plantillas, consentimientos, opt-out `STOP`) y capacidades por adaptador                                                                                                                            | ADR-010                                                                         | Aceptado; la política mínima de F6-04a solo bloquea una conversación cerrada o un canal sin conectar |
| Adaptador de canal, cola de salida, `advance_delivery`, reintentos con espera acotada y barredor                                                                                                                                             | `apps.conversations.channels`, `commands`, `tasks` (F6-04b); outbox (F1-06)     | Construido con `SandboxAdapter`                                                                      |
| HTTP saliente anti-SSRF: `core.http.request` (urllib3), solo `https`, puerto 443, allowlist de hosts, IP pública validada al conectar, sin redirecciones; `httpx`, `requests`, `urllib.request` y `http.client` están prohibidos por un test | ADR-012 §6–7; `storage-http.md` B9                                              | Construido                                                                                           |
| Almacén de archivos con tipo real, tamaño y estado de antivirus; la media de WhatsApp debe guardarse porque sus URLs caducan                                                                                                                 | ADR-008; `files` (`purpose = message-media`)                                    | Construido; el antivirus sigue planificado                                                           |
| Permiso `channels.manage` (sin alcance), que el Owner y el Administrador reciben                                                                                                                                                             | `apps.access.catalog`                                                           | Construido                                                                                           |
| Hosting de producción y URL pública HTTPS para los webhooks                                                                                                                                                                                  | D10-H                                                                           | **Abierta**; bloquea la activación, no el desarrollo                                                 |

### Lo que NO existe y esta fase tiene que crear

La plataforma `WHATSAPP`; `credentials`; las columnas de la cuenta (WABA, calidad, límite, salud); la ruta pública del webhook; `webhook_ingress` y `webhook_failures`; el enrutado; el procesamiento de mensajes entrantes y de estados; los tipos de mensaje distintos de texto (hoy solo `TEXT` y `NOTE`) y `message_attachments`; el adaptador de WhatsApp; el estado intermedio de envío; el servicio de política con la ventana real; `contact_consents` (ADR-010 los asignó a la Fase 4 pero no se construyeron); las plantillas.

## Decision

### 1. Alcance y principios

1. **Solo vías oficiales.** WhatsApp Business Platform, Cloud API, alojada por Meta, con una app de Meta y un WABA (cuenta de WhatsApp Business) del negocio. Sin el cliente WhatsApp Web, sin el protocolo reverso, sin proveedores que lo usen.
2. **Cloud API directa (D-CH-1).** Sin intermediario (BSP). Si el propietario prefiriera uno más adelante, es otro adaptador detrás de la misma interfaz y otro ADR.
3. **Todo se prueba contra un servidor simulado de Meta** (§11). Ningún test, CI ni desarrollo local toca la red de Meta.
4. **El canal no cambia el dominio.** Conversaciones, mensajes, asignación, notas y estados siguen escritos por `apps.conversations.commands`. El canal solo traduce: webhooks a comandos (`open_conversation`, `record_inbound`, `advance_delivery`) y mensajes en cola a llamadas a la API.
5. **Primer corte: texto.** Texto entrante y saliente, estados de entrega y errores. Medios, plantillas, ubicación, contactos, reacciones e interactivos llegan después (§5, §8, `phase-7.md`). Lo que el primer corte no entiende **se ve** como «mensaje no soportado» en el chat: nunca se pierde en silencio.
6. **Todo lo que viene de Meta es dato no confiable**, incluso con la firma válida: se valida el esquema, se acotan los tamaños y se limpia el texto antes de llegar a un comando.

### 2. La cuenta de canal de WhatsApp

Una `channel_account` con `platform = WHATSAPP` representa **un número** (`phone_number_id`). Una organización puede tener varias en el modelo (D-CH-2); la pantalla del primer corte maneja una.

| Campo                                                                    | Contenido                                                                                                                            |
| ------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------ |
| `external_account_id`                                                    | `phone_number_id` (el identificador del número en la Graph API; no es el teléfono)                                                   |
| `external_parent_id` (nuevo)                                             | Id del WABA                                                                                                                          |
| `display_identifier`                                                     | El número visible, `+51 9…`                                                                                                          |
| `name`                                                                   | El nombre que el administrador le da                                                                                                 |
| `credential_id` (nuevo)                                                  | Fila de `credentials` con el token de acceso                                                                                         |
| `status`                                                                 | `PENDING` → `CONNECTED`; `DEGRADED`, `DISCONNECTED` y `ERROR` (§12)                                                                  |
| `status_detail` (nuevo)                                                  | Un **código corto** de un conjunto cerrado (`TOKEN_INVALID`, `WEBHOOK_NOT_SEEN`, `QUALITY_LOW`…); nunca el texto de un error de Meta |
| `quality_rating`, `messaging_limit_tier`, `last_healthcheck_at` (nuevos) | Datos de salud que Meta informa (§12)                                                                                                |

`platform`, `external_account_id` y `external_parent_id` son **inmutables** una vez la cuenta salió de `PENDING` (el webhook enruta con ellos). Desde F7-02b el rol de la aplicación tampoco puede escribirlos en `PENDING` (`UPDATE` por columnas en `channel_accounts`): corregir un número se hace creando otra cuenta. Cambiar de número es crear otra cuenta. La unicidad global del número es el índice único de F6-01: su violación dice en la base que el número ya existe en alguna organización, así que el comando de conectar la convierte en el mismo error genérico que un número mal formado, sin nombrar el índice ni la otra organización. Con el canal apagado (`META_ENABLED`, apagado por omisión) la aplicación no exige ningún ajuste de Meta; encendido, `production` y `local` validan al arrancar (el job de migraciones, no).

**Qué secretos hay y dónde viven**

| Secreto                                                  | Dónde                                                                                                                                                                                      | Por qué                                                                                                         |
| -------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------- |
| **Token de acceso** del usuario del sistema (permanente) | `credentials`, cifrado con `core.sealed` (propósito `channel.access_token`, dueño `channel_account:<id>`), con `key_version`, `last_four`, `fingerprint` y `expires_at` si el token caduca | Es por cuenta y el worker lo tiene que leer para enviar. ADR-022                                                |
| **App secret** de la app de Meta                         | Entorno: `META_APP_SECRETS`, una lista (el vigente primero, el anterior mientras se rota)                                                                                                  | La firma del webhook se verifica **antes** de saber de qué organización es: no puede salir de una tabla con RLS |
| **Verify token** del webhook                             | Entorno: `META_WEBHOOK_VERIFY_TOKEN`                                                                                                                                                       | Mismo motivo; solo se compara en el GET de suscripción                                                          |

Esto supone **una app de Meta por despliegue** (la del propietario), que es lo que pide el primer corte. Si el producto llegara a atender a organizaciones con apps propias (modelo de proveedor de tecnología), el app secret pasaría a una tabla de plataforma y la verificación probaría la firma contra cada candidato; es otra decisión y otro ADR (D-F7-01).

**Reglas de secreto (todas se prueban):**

- Ninguna respuesta de la API, ningún registro de aplicación, ningún evento del outbox, ninguna auditoría ni ninguna métrica lleva un token, un app secret, un verify token ni un valor cifrado. La API de la cuenta solo muestra `last_four`, el estado, `expires_at` y las fechas de rotación. El redactor de registros y de auditoría ya oculta claves como `token`, `secret` y `authorization`; se añaden `access_token`, `app_secret`, `verify_token` y `hub.verify_token`.
- El token se descifra **solo** en el momento de la llamada, en memoria, en el proceso que envía; no se guarda en caché ni en la tarea ni en la cola.
- La huella (`fingerprint`) de la credencial es hoy un SHA-256 sin clave del token, mientras ADR-022 §7 habla de un HMAC: nada la usa todavía y F7-02c decide si se borra o pasa a HMAC con una clave derivada.
- Rotación del token: el administrador pega uno nuevo; se verifica contra la API (§12) antes de guardarlo; se sustituye la fila (`rotated_at`, nuevo `key_version`) y se audita `channel.credentials.rotated` con ids, nunca con el valor. Rotación de la KEK: el job de `reseal` de ADR-022 también recorre `credentials`.
- Rotación del app secret: se añade el nuevo al entorno **delante** del anterior, se despliega y se retira el anterior al terminar; durante el cambio la firma vale con cualquiera de los dos.
- Un token inválido (la API responde que caducó o se revocó) pone la cuenta en `ERROR` con `TOKEN_INVALID` y avisa a quien tenga `channels.manage`; los mensajes en cola fallan con un motivo claro en vez de reintentarse sin fin.

**Conexión (`PENDING` → `CONNECTED`).** Un administrador con `channels.manage` crea la cuenta con el WABA, el `phone_number_id` y el token. El comando, en un solo paso: (1) valida el formato; (2) llama a la Graph API con ese token para leer el número (nombre verificado, calidad, límite) y comprobar que el `phone_number_id` pertenece a ese WABA; (3) guarda la cuenta como `PENDING` con la credencial cifrada; (4) la cuenta pasa a `CONNECTED` solo cuando **además** llega un primer evento firmado del número por el webhook (o una prueba de «webhook visto» que el administrador dispara desde la pantalla), porque sin él la cuenta enviaría pero nunca recibiría. Una plataforma real **nunca** nace `CONNECTED` (nota de revisión de F6-01c). Una cuenta `PENDING` rechaza envíos y aperturas.

### 3. El webhook público

Una ruta pública, **fuera** del espacio `/api/v1/o/<slug>/`, sin sesión, sin CSRF y sin CORS (ADR-003 §3 exime las rutas firmadas): `GET` y `POST /webhooks/meta/`. El proxy local ya enruta `/webhooks/*` a Django (`infra/docker/proxy/Caddyfile`); los límites de cuerpo y de tasa del proxy de producción se fijan con el hosting (D10-H), y la vista acota por su cuenta sin fiarse de ellos.

**`GET` (verificación de la suscripción).** Meta envía `hub.mode=subscribe`, `hub.verify_token` y `hub.challenge`. Si `hub.mode` es `subscribe` y el token coincide con `META_WEBHOOK_VERIFY_TOKEN` por comparación en tiempo constante (`hmac.compare_digest` sobre bytes), la respuesta es `200` con **solo** el `hub.challenge`, como texto plano (acotado en longitud y limitado a dígitos y letras). En cualquier otro caso, `403` con cuerpo vacío y sin distinguir la causa.

**`POST` (eventos).** En este orden y sin atajos:

1. Leer el **cuerpo crudo** con un tope de 1 MiB (si `Content-Length` lo supera, o el flujo lo supera, `413`). No se parsea nada todavía.
2. Exigir `X-Hub-Signature-256` con la forma `sha256=<64 hex>`. Calcular `HMAC-SHA256(app_secret, cuerpo crudo)` y comparar con `compare_digest` contra cada secreto de `META_APP_SECRETS`. Cualquier fallo (cabecera ausente o mal formada, firma que no coincide) es `401` con **cuerpo vacío**, sin distinguir la causa, sin guardar el cuerpo y con un contador de métricas (`webhook_signature_invalid`).
3. Solo ahora, parsear el JSON y validar el esquema: `object == "whatsapp_business_account"`, listas y longitudes acotadas, textos acotados. Un cuerpo firmado pero mal formado es `200` (para que Meta no desactive el webhook), se cuenta y no se procesa.
4. Dividir el evento (Meta agrupa varios) en **una fila de `webhook_ingress` por ítem lógico**, con clave de deduplicación: mensaje `wa:msg:{id}`, estado `wa:status:{id}:{estado}`, y las demás con su propio identificador. `INSERT … ON CONFLICT (provider, event_key) DO NOTHING`.
5. Encolar `process_webhook_ingress` para lo insertado, al confirmar la transacción (`on_commit`).
6. Responder `200` en menos de un segundo. Meta reintenta durante días lo que no recibe con `200`, y puede desactivar un webhook que falla o tarda, así que **con firma válida siempre se responde `200`**, incluso si el procesamiento falla después.

**Replay.** La firma de Meta no lleva marca de tiempo, así que no hay ventana anti-replay posible: un evento válido reenviado es exactamente un duplicado de Meta. La defensa es la **idempotencia** en dos capas (la ingesta por `event_key` y el mensaje por `external_message_id`, que ya existe) y la regla de que un evento antiguo no reabre una conversación cerrada.

**Qué guarda la ingesta, y cómo se accede (decisión: solo por funciones mínimas).** `webhook_ingress` es una tabla de plataforma que guarda, por ítem, el payload (para poder reprocesarlo), cabeceras sanitizadas (nunca `X-Hub-Signature-256` ni cookies), el estado y los intentos. El payload tiene datos personales (el teléfono y el nombre del cliente, el texto), así que se elige **una** forma de acceso, la más estricta:

- La tabla es propiedad de `crm_migrator`, con RLS activada y **sin ninguna política**, y `crm_app` **no tiene privilegios directos** sobre ella (ni `SELECT`, ni `INSERT`, ni `UPDATE`, ni `DELETE`).
- La aplicación solo entra por funciones `SECURITY DEFINER` mínimas, con las garantías de ADR-002 §3.3 (propietario explícito, `search_path` fijo con `pg_temp`, argumentos tipados, `REVOKE ALL … FROM PUBLIC`, `GRANT EXECUTE … TO crm_app`): `webhook_ingress_insert` (inserta el ítem y devuelve su id, o nada si la clave de deduplicación ya existía), `webhook_ingress_claim` (reclama un lote con `FOR UPDATE SKIP LOCKED` y devuelve lo necesario para procesarlo), `webhook_ingress_mark` (cambia el estado, los intentos y, una vez enrutado, la organización y la cuenta) y `webhook_ingress_list` (metadatos sin payload, para el panel de F7-11). Ninguna devuelve más filas ni columnas de las que el llamador necesita.
- **El payload se vacía al pasar a `PROCESSED` o `IGNORED`**: se conserva el hash del contenido (para deduplicar y para el rastro) y se borra el cuerpo. Solo los ítems `FAILED` y `DEAD` conservan el payload, porque hay que poder reintentarlos.
- **La purga de 90 días es el tope, no el plazo normal.** Ningún ítem vive 90 días con su payload salvo que siga sin resolverse.

_Documentos de la fase 0 que dicen cosas distintas._ `tenancy-context.md` §5 describe la tabla «sin RLS de tenant», `04` §Q.3 dice que «tiene RLS y acceso restringido», y `02` §E.12 la llama de plataforma. Esta propuesta las reconcilia (de plataforma, RLS sin política para el rol de la aplicación y acceso solo por funciones). **No se reescribe ningún documento aceptado:** la corrección propuesta queda anotada junto a esos textos como pendiente de la decisión del propietario, y F7-03 la fija en la migración cuando se acepte.

### 4. Enrutado del tenant

El procesador es una `@platform_task` (no hay tenant al empezar):

1. Lee la fila con `FOR UPDATE SKIP LOCKED`; si ya está procesada o ignorada, termina.
2. Extrae **solo** el `phone_number_id` (`entry[].changes[].value.metadata.phone_number_id`) y llama a `resolve_channel_account('WHATSAPP', phone_number_id)`, una función `SECURITY DEFINER` mínima (ADR-002 §3.3: propietario explícito, `search_path` fijo con `pg_temp` al final, argumentos tipados, retorno `(organization_id, channel_account_id)` y nada más, sin SQL dinámico, `REVOKE ALL … FROM PUBLIC` y `GRANT EXECUTE … TO crm_app`) que mira solo cuentas vivas.
3. Si no devuelve nada, el ítem queda `IGNORED` con una métrica «número desconocido o desconectado». No hay tenant, no se escribe nada de negocio.
4. Si devuelve una cuenta, abre `tenant_scope(TenantContext(organization_id, "webhook", actor_type=INTEGRATION))` y procesa dentro. El `organization_id` **sale de la función, nunca del payload** (prueba T11: un payload con un `organization_id` falso se ignora).
5. **Obligatorio:** compara el `entry.id` del evento con el `external_parent_id` (WABA) de la cuenta; si no coincide, el ítem queda `IGNORED` y se alerta (una cuenta mal configurada o un evento ajeno). Tiene su propia prueba (§ «Consecuencias del modelo de app única»).
6. Marca la fila `PROCESSED` en la plataforma.

El procesamiento de un ítem usa solo comandos del dominio, dentro del scope de la organización, con el actor `INTEGRATION`. Un error transitorio deja la fila `FAILED` con `next_attempt_at` (30 s, 2 min, 10 min, 1 h, 6 h) y una fila en `webhook_failures`; tras 8 intentos queda `DEAD`, se notifica y se puede reintentar desde el panel (`integrations.manage`, auditado).

### Consecuencias del modelo de app única

Una sola app de Meta por despliegue (D-F7-01) simplifica la verificación de la firma, pero concentra el riesgo, y conviene decirlo con claridad:

- **Quien tenga el app secret puede firmar eventos para el `phone_number_id` de cualquier organización.** La firma prueba que el evento salió de «nuestra» app, no de qué número ni de qué organización es; el número lo dice el cuerpo, y el cuerpo lo escribe quien firma. Es el mismo punto único de confianza que tiene cualquier receptor de webhooks de una app compartida.
- **Revocar o rotar el secreto afecta a todas las organizaciones a la vez.** Meta genera el secreto nuevo al restablecerlo y el anterior deja de valer de inmediato, así que durante el cambio Meta firma con el nuevo mientras alguna instancia aún solo conoce el anterior. Eso produce 401 por un rato; Meta reintenta durante días lo que no recibe con 200 y la ingesta es idempotente, así que no se pierde nada si el despliegue termina pronto. La lista `META_APP_SECRETS` (el vigente primero, el anterior detrás) existe para ese solape entre instancias, no para tener dos secretos válidos en Meta. El **runbook de rotación** (F7-12) fija el orden: restablecer en Meta, añadir el nuevo secreto delante en el entorno, desplegar, comprobar que entran eventos, retirar el anterior; y la persona responsable avisa a quien opere el despliegue antes de empezar.
- **El secreto vive solo en el entorno** de los procesos que lo usan, nunca en el repositorio, en la base de datos, en un registro, en la salida de CI ni en una respuesta.
- **Defensa adicional en el proxy:** permitir `/webhooks/meta/` solo desde los rangos de IP que Meta publica para sus webhooks, si el hosting lo admite. No sustituye a la firma (los rangos cambian y hay que mantenerlos); la reduce a un segundo candado, y su lista se verifica contra la documentación de Meta al activar.
- **Esquema JSON estricto:** el primer corte valida tipos, longitudes y cantidades de cada campo que usa, y descarta lo demás.
- **Qué logra un evento falsificado** (con el secreto comprometido): inyectar mensajes entrantes de un cliente falso; falsear estados de entrega; y, lo más dañino, **forjar una baja** (`STOP`) para dejar de poder contactar a un cliente real. Defensas: (1) el cruce `entry.id == external_parent_id` (WABA de la cuenta enrutada) es **obligatorio**: un evento firmado para un número cuyo WABA no coincide con el de la cuenta resuelta queda `IGNORED`, se alerta y no escribe nada, con una prueba específica («evento firmado para un número de otra organización»); (2) la baja solo se aplica al contacto de la **cuenta resuelta**, nunca a otra organización, y se comprueba que el remitente sea el contacto de esa conversación; (3) toda baja queda en `contact_consent_events` con su evidencia (el id de ingesta y del mensaje), es visible en la ficha del cliente y una persona con permiso puede revertirla; (4) más de unas pocas bajas por minuto en una cuenta disparan una alerta; (5) un estado de entrega solo se aplica a un mensaje que pertenece a la cuenta resuelta.

### 5. Mensajes entrantes

| Tipo de Meta                                        | Primer corte                                                                                                      | Después                                                          |
| --------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------- |
| `text`                                              | Mensaje `TEXT` (límite de 4096 caracteres de Meta = el de `BODY_MAX`)                                             | —                                                                |
| `image`, `audio`, `video`, `document`, `sticker`    | Mensaje `UNSUPPORTED` con una etiqueta fija del código («Imagen recibida», sin contenido del proveedor) y el tipo | Medios: se descargan al almacén de objetos con antivirus (F7-09) |
| `location`, `contacts`                              | `UNSUPPORTED` con la etiqueta                                                                                     | Ubicación y contactos compartidos como contenido estructurado    |
| `reaction`                                          | Se registra el evento y se ignora (`IGNORED`, métrica)                                                            | Reacción asociada al mensaje                                     |
| `interactive`, `button`                             | `UNSUPPORTED` con la etiqueta                                                                                     | Botones y listas con plantillas                                  |
| `order`, `system`, `unsupported` y los desconocidos | `UNSUPPORTED` genérico                                                                                            | Según haga falta                                                 |

`UNSUPPORTED` es un valor nuevo de `message_type` (hoy el `CHECK` solo admite `TEXT` y `NOTE`; F7-04 lo amplía). Su `body` es una frase **fija** que genera el código, nunca texto del proveedor, para que no haya nada que sanear ni nada que la IA lea. La lista y la vista previa la muestran como cualquier mensaje.

**Texto.** Antes de llamar a `record_inbound`, el adaptador **sanea** el texto del proveedor: quita los sustitutos sueltos, los controles, los caracteres de formato invisibles y los controles de dirección (los comandos rechazan todo eso, F6-02d, y no limpian a escondidas), normaliza saltos de línea y recorta a 4096. Si no queda nada visible, el mensaje se registra como `UNSUPPORTED`. Los Tags (U+E0000–E007F) se quitan también: son un canal de inyección de instrucciones cuando la IA lea los mensajes (FIX LATER de F6-02d).

**Contacto.** El `wa_id` de Meta es el teléfono sin el signo `+`. Se normaliza a E.164 (con las rarezas de algunos países, como el 9 de Argentina o el 1 de México, probadas) y se busca el contacto de la organización por `primary_phone_e164`.

- **Ese campo no es único** (`apps.contacts`, por diseño: familias que comparten un número; los duplicados se gestionan, no se impiden). Dos primeros mensajes simultáneos de un número nuevo crearían dos contactos. El mecanismo contra eso, en F7-04 y sin migración: dentro de la transacción de la organización se toma `pg_advisory_xact_lock` sobre una clave derivada de (organización, teléfono normalizado), y **después** se busca y, si no hay, se crea. El segundo proceso espera, encuentra el contacto del primero y lo reutiliza. No se añade un índice único: contradiría el diseño de contactos. Si ya existen varios contactos con ese número, se elige el que tiene una conversación abierta en esa cuenta o, si no, el más antiguo; **nunca se fusionan** a escondidas.
- **Origen.** `Contact.Source` no tiene `WHATSAPP`, pero sí `INBOUND_MESSAGE`: se usa ese valor y el canal de origen se lee de la primera conversación (su cuenta de canal), sin añadir un valor ni un campo.
- **El nombre del perfil de WhatsApp es dato no confiable.** Lo escribe el cliente: se sanea como el texto, se acota (100 caracteres), se muestra aislado (`<bdi>`) y **nunca se usa para buscar ni fusionar contactos**.
- **Tope de creación:** un máximo de contactos nuevos por minuto y por cuenta (F7-04); pasado el tope, los mensajes se encolan para reintento y se alerta, para que un abuso no llene la base de contactos vacíos.
- Un contacto bloqueado o sin consentimiento sigue el §9.

**Conversación y orden.** `open_conversation` y `record_inbound` ya son idempotentes por `external_message_id` (índice único por cuenta). El **orden del chat es el de llegada al servidor** (03 §I.7); el instante del proveedor se conserva en `provider_timestamp` (columna nueva) para auditoría y, sobre todo, para la **ventana de 24 h**: el ancla de la ventana es el instante del proveedor (acotado para que nunca esté en el futuro), porque Meta la cuenta desde su reloj; así un evento que llega horas tarde tras una caída no alarga la ventana. Un evento con más de 24 h de antigüedad no reabre una conversación `RESOLVED` ni `CLOSED`.

**Cuentas que no están conectadas.** Una cuenta `DISCONNECTED` o `ERROR` registra el entrante (el cliente escribió de verdad) pero no responde; una `PENDING` lo ignora con métrica.

### 6. Mensajes salientes: el adaptador

`WhatsAppAdapter` implementa `ChannelAdapter` (F6-04b) y es lo único que cambia respecto del Sandbox.

- **Llamada.** `POST https://graph.facebook.com/<versión>/<phone_number_id>/messages` con `Authorization: Bearer <token>` y un cuerpo JSON `{messaging_product: "whatsapp", to: <wa_id>, type: "text", text: {body: <texto>}}`, por **`core.http.request`**: urllib3, allowlist (`graph.facebook.com` se añade a `HTTP_ALLOWED_HOSTS`), IP pública validada al conectar, sin redirecciones, tiempos de espera acotados. _Contradicción con el encargo inicial:_ se pidió `httpx`, pero ADR-012 y un test estático prohíben `httpx` y `requests` fuera de `core.http`; este ADR lo respeta y propone extender `core.http` si hace falta (cuerpo en flujo para descargar medios, §7).
- **Versión de la Graph API fijada** en un ajuste (`META_GRAPH_VERSION`), nunca «la última»; subirla es un cambio de código revisado, con las respuestas simuladas de esa versión como contrato (§11). Se revisa al menos cada trimestre porque Meta retira versiones con aviso.
- **Respuesta.** Se lee `messages[0].id` como `external_message_id`. Los errores traen `error.code` y `error.error_subcode`; se mapean a un conjunto cerrado propio (tabla de §8) y **nunca** se guarda ni se muestra el texto del error de Meta (puede traer datos del cliente).
- **Sin clave de idempotencia en la API.** Meta no deduplica envíos. Reintentar a ciegas tras un tiempo agotado puede duplicar un mensaje al cliente. Por eso F7-05 cambia la forma de despachar (FIX LATER de F6-04b): el despachador **reclama** el mensaje en un estado intermedio `SENDING` (con la conversación y la cuenta leídas con bloqueo compartido, no exclusivo, para no serializar todos los envíos de una cuenta) y llama al adaptador **fuera** de la transacción. Se envía `biz_opaque_callback_data` con el id de nuestro mensaje (la API lo devuelve en los estados), de modo que un resultado ambiguo (se envió la petición y no llegó la respuesta) se **reconcilia** cuando el estado llega por el webhook en vez de reenviarse. Si no llega nada en el plazo (15 minutos, ajustable), el mensaje queda `FAILED` con `PROVIDER_UNKNOWN`, visible, y la persona decide si reenviarlo sabiendo el riesgo de duplicado. _(El nombre y el comportamiento exactos de ese campo se confirman contra la documentación vigente antes de implementar.)_
- **Límites de tasa.** Meta limita el rendimiento por número, los envíos por par (número, cliente) y los mensajes iniciados por la empresa por día según el nivel de calidad. La tarea de salida aplica un limitador por cuenta (Redis, ya presente para Celery), muy por debajo del tope, y trata un «demasiado rápido» como fallo pasajero con espera creciente. Los niveles y la calidad se leen en el chequeo de salud (§12), no se suponen.
- **Contrato de at-least-once** (ADR-010/F6-04b): el adaptador puede llamarse más de una vez por mensaje; con el estado `SENDING` y la reconciliación, la repetición solo ocurre cuando el proveedor no recibió el envío.

### 7. Medios (fuera del primer corte; el diseño queda fijado)

Una URL de media de Meta caduca en minutos y exige el token. El flujo: el entrante de tipo media crea el mensaje y una fila de `message_attachments` (`download_status = PENDING`, `external_media_id`); una tarea consulta el id en la Graph API, descarga el binario con un **tope de tamaño y en flujo** (hoy `core.http` carga el cuerpo entero en memoria: F7-09 añade una descarga acotada, con los hosts de media en la allowlist), valida el tipo real por magic bytes, calcula el `sha256`, lo guarda con `ObjectStorageService` (`files.purpose = message-media`, ADR-008) y deja `scan_status = PENDING` hasta que haya antivirus; la pantalla no sirve un archivo con `scan_status` distinto de `CLEAN` salvo decisión expresa del propietario (D-F7-04). La URL de Meta nunca se entrega al navegador. Un archivo que no se pudo bajar a tiempo queda `EXPIRED` y visible.

### 8. Estados de entrega y errores

Los `statuses[]` del webhook (`sent`, `delivered`, `read`, `failed`) se localizan por `external_message_id` (o por `biz_opaque_callback_data`, §6) y llaman a `advance_delivery`, que solo avanza (un `delivered` tardío tras `read` no retrocede; `failed` es terminal y no se alcanza desde lo entregado). Si el estado llega **antes** de que el envío confirme su `external_message_id`, la fila de ingesta falla de forma transitoria y se reintenta con la espera de §4; superados los intentos queda `IGNORED` con métrica.

Los errores de Meta se normalizan a códigos propios guardados en el mensaje (columna nueva `provider_error_code`, un código corto; el texto en español sale de una tabla del código). Ejemplos a **verificar contra la documentación vigente** al implementar (los números cambian):

| Situación de Meta                               | Código interno          | Efecto                                                |
| ----------------------------------------------- | ----------------------- | ----------------------------------------------------- |
| Fuera de la ventana de 24 h (re-engagement)     | `WINDOW_CLOSED`         | `FAILED`; la pantalla ofrece plantilla cuando exista  |
| Número del cliente sin WhatsApp / no entregable | `RECIPIENT_UNREACHABLE` | `FAILED`                                              |
| Token vencido o revocado                        | `TOKEN_INVALID`         | Cuenta en `ERROR`; los mensajes en cola fallan; aviso |
| Demasiados mensajes (rendimiento o par)         | `RATE_LIMITED`          | Fallo pasajero: reintento con espera                  |
| Calidad o spam                                  | `QUALITY_BLOCKED`       | `FAILED`; la cuenta pasa a `DEGRADED`; aviso          |
| Error de Meta (5xx) o tiempo agotado            | `PROVIDER_ERROR`        | Pasajero, salvo que sea ambiguo (§6)                  |
| Plantilla pausada, rechazada o inexistente      | `TEMPLATE_UNAVAILABLE`  | `FAILED` (con plantillas)                             |
| Cualquier otro                                  | `PROVIDER_REJECTED`     | `FAILED`                                              |

### 9. Ventana de 24 h, plantillas, consentimiento y baja

**Contrato de «ventana cerrada» para el despachador (se define ahora, se completa después).** El despachador deja de consultar solo `check_send`: pide una decisión a `MessagingPolicyService.evaluate` (ADR-010) con la intención del envío. En el primer corte el servicio evalúa, en el orden de ADR-010, la nota interna, la cuenta desconectada, el contacto bloqueado y la **ventana**, recalculada siempre desde `last_inbound_at` con las capacidades vigentes del adaptador (`window_hours = 24` para WhatsApp). El resultado es `FREE_FORM` o `BLOCKED(reason)`; `TEMPLATE_REQUIRED` (ventana cerrada con plantilla aprobada) llega con las plantillas y mientras tanto se trata como `BLOCKED(WINDOW_CLOSED)`. La decisión se guarda en el mensaje (`policy_decision`), se re-evalúa **justo antes** de enviar, y la pantalla la consulta para mostrar el tiempo restante y deshabilitar el texto libre. Instagram y Messenger traen su propia ventana por el mismo contrato.

**Plantillas (F7-10).** Sincronización con la Graph API (`message_templates`, estados `APPROVED/PENDING/REJECTED/PAUSED/DISABLED`), envío de plantilla con variables validadas por `variables_schema`, y alerta cuando una plantilla usada pasa a `PAUSED` o `REJECTED`. El propietario decide qué plantillas se piden a Meta; la aprobación es de Meta y no se controla.

**Consentimiento y baja.** WhatsApp no entrega «STOP» por sí mismo: la baja es una palabra que el cliente escribe y que el negocio tiene que respetar. Un entrante cuyo texto completo es una de las palabras de baja de la organización (`STOP`, `BAJA`, `CANCELAR`, configurables) registra una revocación (`source = KEYWORD_STOP`) y bloquea todo mensaje **proactivo** (seguimientos, cotizaciones, marketing) a ese contacto y canal; las respuestas a lo que el cliente escribe dentro de la ventana siguen permitidas (ADR-010 §2.7). El cliente recibe una confirmación fija. Esto exige `contact_consents` y su historial, que ADR-010 asignó a la Fase 4 y no existen en el código: F7-08 los crea con el comando que los escribe. La palabra de alta (`ALTA`, `START`) los restituye. Hasta entonces, el primer corte solo envía respuestas dentro de la ventana, que es lo permitido sin consentimiento previo.

### 10. Seguridad

- **Firma y verify token** por tiempo constante; rechazos uniformes y sin detalle; contadores de métricas sin payload.
- **Tamaños**: 1 MiB por webhook, ítems y textos acotados, listas acotadas.
- **Orden de verificación**: ningún dato se parsea antes de la firma; el tenant solo sale de la función de enrutado.
- **SSRF**: toda llamada saliente pasa por `core.http` (allowlist exacta, IP pública fijada, sin redirecciones). Los hosts permitidos se limitan a `graph.facebook.com` y, con medios, a los hosts de descarga que la Graph API devuelva, uno a uno. La URL de un medio nunca la elige el cliente.
- **Texto no confiable**: saneado (§5); el nombre de perfil, acotado y saneado; ningún HTML.
- **Observabilidad sin datos personales**: los registros llevan ids, tipos, estados y códigos, no teléfonos ni textos; el teléfono se muestra enmascarado en los avisos (`+51 9•• ••• 123`).
- **Auditoría**: `channel.connected`, `channel.disconnected`, `channel.credentials.rotated`, `webhook.dead_lettered`, `webhook.replayed`, con ids y sin valores.
- **Reglas de la plataforma**: la baja, la ventana y la calidad se respetan en servidor; el negocio no puede enviar a quien se dio de baja ni fuera de la ventana sin plantilla.
- **Datos del cliente**: el repositorio es público; ningún dato real de clientes, números ni nombres aparece en código, pruebas ni documentos.

### 11. Pruebas contra un servidor simulado

Un **simulador de Meta** que reside en el repositorio (en proceso para las pruebas, y como servicio ligero para el entorno local y las pruebas de punta a punta de la pantalla) cumple dos papeles:

1. **Emisor de webhooks firmados**: genera los cuerpos de Meta (mensaje de texto, estado `sent/delivered/read/failed`, tipos no soportados, eventos mal formados) y los firma con un app secret de prueba.
2. **Graph API falsa**: responde a `POST /<versión>/<phone_number_id>/messages`, a la consulta del número y, más adelante, a plantillas y medios, con respuestas correctas y con cada error de §8, además de lentitud y cortes.

Las respuestas simuladas son fixtures versionadas junto a `META_GRAPH_VERSION`: cambiar la versión obliga a revisar el contrato. **No hay red real** en tests ni en CI; la conexión con un número de verdad es la última tarea (F7-12) y la hace el propietario con sus credenciales.

### 12. Activación y salud por cuenta

`PENDING` → `CONNECTED` según §2. Después, una tarea periódica (`sync_jobs`, `HEALTHCHECK`) lee de la Graph API la calidad, el nivel de mensajería y la validez del token, y actualiza `quality_rating`, `messaging_limit_tier`, `last_healthcheck_at` y el estado: `DEGRADED` con calidad baja o límite agotado; `ERROR` con token inválido; `DISCONNECTED` cuando un administrador la desconecta (los mensajes en cola fallan con un motivo claro). La pantalla de configuración (F7-07) muestra el estado, el checklist de lo que falta y la salud, sin mostrar ningún secreto.

### 13. Lo que el propietario tiene que conseguir (sin ningún valor aquí)

El detalle, con el paso en el que se necesita cada cosa, está en `phase-7.md` («Qué necesita el propietario»). En resumen: una cuenta de Meta Business verificada; una app de Meta tipo Business con el producto WhatsApp; un WABA y un número (nuevo o migrado, D-F7-02) con su nombre visible aprobado; un usuario del sistema con un token permanente y los permisos de mensajería y administración de WhatsApp; el app secret; un verify token elegido por quien despliega; una URL pública HTTPS con certificado válido (D10-H); un método de pago en el WABA si se van a usar plantillas; y las plantillas que se quieran aprobar. **Nada de esto hace falta para desarrollar y probar.** Hace falta para F7-12.

### 14. Preguntas abiertas para el propietario

D-F7-01 a D-F7-10, con su recomendación, en `phase-7.md`.

## Alternatives considered

| Alternativa                                                            | Por qué se descarta                                                                                                                                 |
| ---------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------- |
| Un proveedor intermedio (BSP)                                          | Otro contrato, otro coste y otro punto de falla; D-CH-1 ya recomendó la Cloud API directa                                                           |
| WhatsApp Web, bibliotecas no oficiales o sesiones personales           | Incumplen los términos de WhatsApp, arriesgan el bloqueo del número y no son seguras; el propietario las excluyó                                    |
| Guardar el app secret y el verify token por cuenta en la base de datos | La firma se comprueba antes de conocer la organización; el secreto tendría que leerse sin tenant. Una app por despliegue lo resuelve con el entorno |
| Enrutar por un campo del payload o por la URL                          | La organización no puede salir de un dato que controla quien llama; sale de la función `SECURITY DEFINER` que lee la cuenta                         |
| Llamar a la API desde la petición del usuario                          | Latencia, reintentos duplicados y secretos en el proceso web; la salida es una tarea con el outbox, como ya está                                    |
| Reintentar siempre un envío que agotó el tiempo                        | Duplica mensajes al cliente (Meta no deduplica); se reconcilia con el webhook                                                                       |
| `httpx` o `requests`                                                   | Prohibidos por ADR-012 fuera de `core.http`                                                                                                         |
| Descargar la media dentro del webhook                                  | El webhook tiene que responder en menos de un segundo; la media se baja en una tarea                                                                |

## Consequences

- El primer canal real reutiliza casi todo lo construido en la Fase 6; lo nuevo es la frontera con Meta (webhook, firma, enrutado, adaptador) y unas pocas columnas y tablas.
- Hay que ampliar `messages` (`UNSUPPORTED`, `provider_timestamp`, `provider_error_code`, estado `SENDING`) y la cuenta de canal (WABA, credencial, salud), y crear `credentials`, `webhook_ingress`, `webhook_failures`, `contact_consents` y `message_attachments`: cada migración llega con la funcionalidad que la usa.
- El despachador cambia de forma (reclamar y llamar fuera de la transacción): es el cambio de más riesgo y va en su propio work item con pruebas de concurrencia.
- Hasta que existan las plantillas y el consentimiento, el producto solo responde dentro de la ventana de 24 h; los seguimientos y las cotizaciones por WhatsApp fuera de ella esperan a F7-10.
- Una app de Meta por despliegue simplifica la seguridad pero ata el producto a la cuenta del propietario; atender a terceros con apps propias es otra decisión.

## Security implications

Ver §10. Los puntos de mayor riesgo son: la ruta pública (firma y tamaño antes de cualquier trabajo), el enrutado cross-tenant (función mínima, el payload nunca decide), el token de acceso (cifrado, solo en memoria al enviar) y el texto del cliente (saneado y marcado como no confiable para la IA futura).

## Operational implications

- Variables de entorno nuevas: `META_APP_SECRETS`, `META_WEBHOOK_VERIFY_TOKEN`, `META_GRAPH_VERSION`, la allowlist con `graph.facebook.com`. Producción las exige al arrancar cuando haya una cuenta de WhatsApp; el desarrollo usa los del simulador.
- El worker y beat llevan las tareas nuevas (procesador, reintento de ingesta, salud, sincronización de plantillas, purga de 90 días) antes de recibir tráfico (aviso del outbox sobre el orden de despliegue).
- Métricas: firmas inválidas, ítems recibidos/procesados/`FAILED`/`DEAD`, lag de procesamiento, envíos por resultado y por código de error, calidad y nivel de cada número.
- Runbook de activación y de incidentes (token caducado, webhook desactivado por Meta, número bloqueado) en `docs/runbooks/` con F7-12.
