# ADR-032: Coexistencia de la app WhatsApp Business con la Cloud API (un solo número en el teléfono y en Good Doggy)

- **Status:** Proposed
- **Date:** 2026-11-13
- **Deciders:** propuesto por el programa autónomo (ADR-015 §5) para la decisión del propietario. Mientras esté `Proposed` no obliga al código y ningún work item de coexistencia empieza (todos dependen del issue de aceptación, F7-13b, #739). Las preguntas que necesitan respuesta del propietario están en §13.
- **Work items:** F7-13a #738 (archiva este ADR), F7-13b #739 (aceptación), F7-13c #740, F7-13d #741, F7-13e #742, F7-13f #743, F7-13g #744, F7-13h #745, F7-13i #746, F7-13j #747, F7-13k #748.
- **Related:** ADR-001, ADR-002 §3.3, ADR-003, ADR-008, ADR-010, ADR-011, ADR-012 §6–7, ADR-015, ADR-022, ADR-029 (todo el canal; responde a su D-F7-02 y mantiene D-F7-01), `docs/architecture/tenancy-context.md` §5, `docs/architecture/security-boundaries.md` B1, [phase-7.md](../phases/phase-7.md)

## Context

El propietario quiere **un solo número** para la tienda, usado a la vez en la **app WhatsApp Business del teléfono** y en **Good Doggy CRM**:

- En la app del teléfono se quedan los estados, las llamadas y las respuestas manuales que alguien quiera dar desde ahí.
- En Good Doggy se gestionan las conversaciones, los vendedores, la IA y las campañas.
- La conexión se hace con el **Embedded Signup** de Meta en su modo de coexistencia, después de comprobar los requisitos y permisos oficiales.
- Los mensajes y eventos compatibles se sincronizan **sin duplicados**.
- Se respetan la arquitectura, la seguridad, el aislamiento entre organizaciones y las políticas de mensajería.
- **Kommo no se integra**: ya no se usa.
- **El número de producción no se activa nunca sin la autorización expresa del propietario.**

ADR-029 (Proposed) dejó la coexistencia como opción en D-F7-02 («coexistir con la app de WhatsApp Business si Meta lo permite para ese número») y recomendó un número nuevo para el piloto. Este ADR estudia esa opción con la documentación oficial vigente y propone cómo encajarla sin cambiar el dominio.

### Lo que ya existe y este ADR no cambia

| Pieza | Dónde | Estado |
| --- | --- | --- |
| Cuenta de canal `WHATSAPP` (`external_account_id` = `phone_number_id`, `external_parent_id` = WABA, inmutables), credencial cifrada (ADR-022), conectar con token pegado, rotar y desconectar con MFA reciente | `apps.conversations.accounts`, `credentials`, F7-02a…e | Construido |
| Webhook `POST /webhooks/meta/`: firma sobre el cuerpo crudo, tope de cuerpo `BODY_MAX` = 1 MiB, esquema estricto, una fila de `webhook_ingress` por ítem con clave de deduplicación | `meta_webhook.py`, `ingress.py`, F7-03a…c | Construido |
| Enrutado del tenant por `resolve_channel_account('WHATSAPP', phone_number_id)` (`SECURITY DEFINER`) y cruce obligatorio `entry.id == external_parent_id` | `ingress.route`, F7-03c | Construido |
| Hoy, todo campo distinto de `messages` se guarda como ítem `other` sin número y queda `IGNORED` (`NO_NUMBER`) con el payload vaciado | `meta_webhook._change`, `ingress.route` | Construido: con coexistencia, los ecos y el historial **se perderían en silencio** si se activara hoy |
| Mensajes entrantes de texto, no soportados y medios; contacto por teléfono E.164 con `pg_advisory_xact_lock`, sin índice único; tope de contactos nuevos por minuto | `inbound.py`, F7-04a/b, F7-09a | Construido |
| Un mensaje entrante sin `from` (un cliente que llega solo con su BSUID) se cierra `IGNORED` con `BAD_PHONE`, el mismo código que un teléfono mal formado: sin contacto, sin mensaje, con el payload vaciado y sin registro propio | `inbound.receive`, `ingress.route` | Construido; medirlo por separado es F7-14 (#749) |
| `messages`: `direction`, `sender_type` (`CONTACT`, `USER`, `AI_AGENT`, `SYSTEM`), `external_message_id` único por (organización, cuenta), `provider_timestamp`, `delivery_status` obligatorio en lo saliente y que solo avanza; `messages_unsupported_ck` deja `UNSUPPORTED`, `AUDIO` y `VIDEO` solo para lo entrante (`INBOUND_ONLY`) | `models.Message` | Construido |
| Política (`policy.evaluate`): ventana anclada en `last_inbound_at`, contacto bloqueado, baja (`CONSENT_REVOKED` solo para lo proactivo) | F7-05a, F7-08b | Construido |
| Consentimientos con historial inmutable y palabras de baja/alta | `apps.contacts.consents`, F7-08a/b | Construido |
| Estados de entrega por webhook; un estado de un mensaje desconocido queda `IGNORED` tras tres intentos («un número que también se usa desde la app del teléfono recibe estados de mensajes que el CRM no envió», ADR-029 §8) | F7-06a | Construido |
| Simulador de Meta (webhooks firmados y Graph API falsa), versión de fixtures `v23.0` | `backend/tests/meta_sim`, F7-01 | Construido |
| CSP estricta por nonce (`frame-src 'none'`, `connect-src 'self'`) y `Cross-Origin-Opener-Policy: same-origin` en todo el frontend | `frontend/src/lib/csp.ts`, `frontend/next.config.ts` | Construido |

### Lo que dice Meta (fuentes primarias, leídas el 2026-11-13)

Cada afirmación lleva su fuente. Lo que no se pudo comprobar está marcado **SIN VERIFICAR** y no se usa como premisa sin una prueba con una app de pruebas.

**Alta (onboarding) por Embedded Signup.** [S1]

- La coexistencia se llama «onboarding de usuarios de la app WhatsApp Business». Requisitos: la app del teléfono en la versión **2.24.17 o superior**; quien integra «ya debe ser **Solution Partner o Tech Provider**»; saber usar la Cloud API; un webhook capaz de recibir los eventos; y usar Embedded Signup con registro de la sesión. [S1]
- Se activa con `featureType: "whatsapp_business_app_onboarding"` en `extras` y `sessionInfoVersion: "3"`. En la v5 viene activado por omisión (se desactiva con `features: [{name: "whatsapp_business_app_onboarding", enabled: false}]`). Al terminar llega el evento `FINISH_WHATSAPP_BUSINESS_APP_ONBOARDING` (tipo `WA_EMBEDDED_SIGNUP`) con `data.waba_id`. [S1] [S5]
- **No se registra el número** (ya está registrado en la app): se sigue el alta normal de Tech Provider saltando el paso `register`. [S1]
- Embedded Signup usa **Facebook Login for Business** y el **SDK de JavaScript** (`https://connect.facebook.net/en_US/sdk.js`), con un `config_id`; el dominio de la página debe estar en «Allowed domains» y «Valid OAuth redirect URIs» y tener **HTTPS** con certificado válido. El código intercambiable dura **30 segundos**. El ejemplo oficial filtra los mensajes con `event.origin.endsWith('facebook.com')`. [S5]
- **Las versiones v2 y v3 de Embedded Signup quedaron obsoletas el 15 de octubre de 2026**; hay que usar la v4 (la página de coexistencia ya menciona la v5). [S3] [S5]
- Tras el flujo (servidor a servidor): intercambiar el código por el **token de negocio** con `GET /oauth/access_token` (`client_id`, `client_secret`, `code`); suscribir la app al WABA con `POST /<WABA_ID>/subscribed_apps`; registrar el número (no en coexistencia); el cliente añade un método de pago en WhatsApp Manager. «Haz todas estas peticiones de servidor a servidor». [S4]
- Comprobación opcional: `GET /<PHONE_NUMBER_ID>?fields=is_on_biz_app,platform_type` → `is_on_biz_app: true` y `platform_type: "CLOUD_API"`. [S1]

**Sincronizaciones de una sola vez.** [S1]

- Contactos: `POST /<PHONE_NUMBER_ID>/smb_app_data` con `"sync_type": "smb_app_state_sync"`. Historial: el mismo endpoint con `"sync_type": "history"`.
- **Las dos deben hacerse dentro de las 24 horas siguientes al alta**; si no, el negocio tiene que desconectarse y repetir el flujo. **Cada una solo puede ejecutarse una vez.**
- Si el negocio no acepta compartir el historial, llega un webhook `history` con el error `2593109`.
- Se recomienda que el negocio tenga la app abierta durante la sincronización.

**Webhooks que añade la coexistencia.** Todos exigen `whatsapp_business_management`; `messages` exige `whatsapp_business_messaging`. Los cuerpos pueden medir **hasta 3 MB**; Meta reintenta **hasta 7 días** y puede duplicar. [S10]

| Campo | Qué trae | Forma | Fuente |
| --- | --- | --- | --- |
| `smb_message_echoes` | Mensajes que el negocio envía **desde la app o un dispositivo acompañante** después del alta, y sus ediciones y eliminaciones | `value.message_echoes[]`: `from` (el número del negocio), `to` (opcional), `to_user_id`, `id`, `timestamp`, `type` y el objeto del tipo; `value.contacts[]` con `wa_id` (opcional) y `user_id`. Tipos: `text`, `image`, `video`, `document`, `revoke`, `edit` (con `original_message_id`) | [S6] |
| `history` | Mensajes enviados y recibidos en los **180 días** anteriores al alta, sin grupos | `value.history[]`: `metadata` (`phase` 0/1/2 = día 0–1, 1–90, 90–180; `chunk_order`; `progress`, 100 = terminado) y `threads[]` (`id`, `context` con `wa_id`, `user_id`, `username`) con `messages[]` (`from`, `from_user_id`, `id`, `timestamp`, `type`, objeto del tipo, `history_context.status`: `delivered`, `error`, `pending`, `played`, `read`, `sent`). Sin `to`: si `from` es el número del negocio, lo envió el negocio. Los trozos pueden llegar desordenados (`chunk_order`). «Un solo webhook podría describir miles de mensajes». Los medios **no traen id**; llegan en webhooks aparte y **solo** los de los 14 días previos al alta | [S7] [S1] |
| `smb_app_state_sync` | Contactos de la libreta de la app: los actuales (al pedir la sincronización) y los que el negocio añade, edita o quita después | `value.state_sync[]`: `type: "contact"`, `contact` (`full_name`, `first_name`, `phone_number`, `user_id`, `username`), `action` (`add` también es «editado», o `remove`), `metadata.timestamp` | [S8] |
| `account_update` | Ciclo de vida: `PARTNER_ADDED`, `PARTNER_REMOVED` (con `disconnection_info.reason` e `initiated_by`), `ACCOUNT_OFFBOARDED`, `ACCOUNT_RECONNECTED` | `waba_info.waba_id`, `phone_number` (número visible), sin `metadata.phone_number_id` | [S9] |

**Identidad sin teléfono (BSUID).** Desde abril de 2026 los webhooks traen `user_id` (id del usuario por cartera de negocio, «BSUID»). Con los nombres de usuario de WhatsApp (despliegue gradual en 2026), `wa_id`, `from` o `to` **pueden faltar**: el teléfono solo se incluye si hubo un mensaje o una llamada en los últimos 30 días o si el usuario está en la libreta del negocio. Entonces el BSUID es el único identificador; cambia si el usuario cambia de número. [S13] [S6] [S7] [S8]

**Lo que cambia en el teléfono después del alta.** [S1]

| En la app | Después del alta |
| --- | --- |
| Chats 1:1 | Siguen; se reflejan en ambos sentidos entre la app y la Cloud API. Editar y eliminar, admitidos |
| Contactos | Se pueden sincronizar los que tienen WhatsApp |
| Grupos | No se sincronizan |
| Mensajes temporales | Se **desactivan** en todos los chats 1:1 |
| Ver una vez, ubicación en tiempo real | Se **desactivan** en los chats 1:1 |
| Listas de difusión | Las existentes quedan **de solo lectura**; no se crean nuevas |
| Llamadas de voz y video, herramientas de empresa, perfil, canales | Sin cambio en la app; **no** pasan a la Cloud API |
| Dispositivos acompañantes | Se **desvinculan** todos al hacer el alta; se pueden volver a vincular salvo WhatsApp para Windows y WearOS |

- Rendimiento fijo de **20 mensajes por segundo** para un número en coexistencia. [S1]
- Los mensajes enviados desde la app **siguen siendo gratis** y **no abren, no alargan ni afectan las ventanas** de la Cloud API ni su precio. [S1]
- Desconexión: el negocio la hace en la app (**Ajustes > Cuenta > Business Platform > Desconectar cuenta**); llega `account_update` con `PARTNER_REMOVED`. **No se puede usar la API de desregistro** en un número en coexistencia. Motivos de desconexión del sistema: el teléfono principal inactivo **unos 14 días**, un acompañante inactivo unos 30 días, cambio de número, reinstalación en otro dispositivo, paso a la app de consumo, o una sanción. [S1] [S9]

**Tech Provider.** Exige la **verificación del negocio**, una app de Meta con el caso de uso de WhatsApp, y **App Review** (ajustes de la app, dos videos: un mensaje enviado desde la app y recibido en WhatsApp, y la creación de una plantilla) para obtener **acceso avanzado** a `whatsapp_business_messaging` y `whatsapp_business_management`; sin él, las llamadas a cuentas de otra cartera dan el error `200`. Los clientes añaden una tarjeta. [S2] Con la app en modo Live, en Embedded Signup solo aparecen los permisos aprobados para acceso avanzado. [S3] Embedded Signup pide `whatsapp_business_management`, `whatsapp_business_messaging` y, para el crédito de un Solution Partner, `business_management`. [S3]

**Precios.** Desde el 1 de julio de 2025 Meta cobra por mensaje; la ventana de atención de 24 h se abre con cada mensaje o llamada del usuario; los mensajes de servicio fueron gratis desde el 1 de noviembre de 2024 y vuelven a cobrarse desde el 1 de octubre de 2026, con **1.000 mensajes de servicio entregados gratis al mes** por número; las plantillas de utilidad dentro de la ventana también dejaron de ser gratis el 1 de octubre de 2026. [S11] _Esto matiza ADR-029, que da por gratuitas las respuestas dentro de la ventana: lo revisa F7-12, no este ADR._

**Modelo de cuentas nuevo.** Meta separa «cuenta de WhatsApp» y «cuenta de mensajería»; en coexistencia **convierte el WABA existente** en una cuenta de mensajería compatible. Fase 1 (2.º semestre de 2026): sin cambios de código, `waba_id` pasa a ser el id de la cuenta de mensajería. Fase 3 (1.er semestre de 2028): las API que usan el `phone_number_id` en la ruta pasarán a usar el id de la cuenta de WhatsApp. [S12]

**SIN VERIFICAR** (no se usa como premisa):

1. Países o regiones donde la coexistencia no está disponible. La página oficial no lista ninguno; guías de terceros antiguas excluían la UE, el Reino Unido y otros, y una de 2026 dice que se levantaron. Hay que confirmarlo para **el país del número de la tienda** en el propio flujo.
2. Un tiempo mínimo de uso previo del número en la app (terceros dicen 7 días; Meta no lo dice).
3. Si un mensaje enviado **por la Cloud API** genera también un `smb_message_echoes` (la referencia solo nombra la app y los acompañantes). El diseño deduplica igual (§4).
4. Si Meta envía `statuses` de los mensajes que el negocio manda desde la app.
5. Si una app cuya **misma cartera** es dueña del WABA puede hacer el alta de coexistencia **sin** ser Tech Provider (con acceso estándar). La página de coexistencia exige Tech Provider o Solution Partner sin excepción.
6. La caducidad del token de negocio que devuelve el intercambio.
7. Los orígenes exactos de `postMessage` del flujo y si el popup necesita `window.opener` (afecta a `Cross-Origin-Opener-Policy`).
8. El cuerpo exacto de `smb_app_data` además de `sync_type` (por ejemplo, `messaging_product`) y su respuesta.
9. Si desde el CRM hay una forma de «desconectar» un número en coexistencia distinta de quitar la suscripción del WABA (`DELETE /<WABA_ID>/subscribed_apps`).
10. La fecha concreta de los nombres de usuario y cuándo dejará de llegar el teléfono en la práctica.

## Decision

### 1. Alcance y principios

1. **Solo vías oficiales**, como ADR-029 §1: Cloud API de Meta, Embedded Signup oficial y la app WhatsApp Business oficial del teléfono. Sin WhatsApp Web, sin bibliotecas no oficiales, **sin Kommo** ni otro CRM intermedio.
2. **La coexistencia es un modo de la misma cuenta de canal**, no una plataforma nueva: `platform = WHATSAPP`, el mismo adaptador, el mismo webhook, el mismo enrutado y la misma política. Columna nueva en `channel_accounts`: `connection_mode` (`CLOUD_API` por omisión | `COEXISTENCE`), fijada al conectar e inmutable como `platform`.
3. **Reparto de funciones** (lo que dice Meta, no una preferencia): llamadas, estados, grupos, listas de difusión y herramientas de empresa se quedan en el teléfono; los chats 1:1 se reflejan en los dos lados.
4. **Nada de lo que se haga en el teléfono pasa por la política del CRM**: ya se envió. El CRM lo registra, lo muestra y avisa si choca con una baja (§6).
5. **Una app de Meta por despliegue** (D-F7-01 de ADR-029) se mantiene: la app del despliegue pasa a ser Tech Provider y la tienda es la cartera que se conecta por Embedded Signup. El app secret sigue solo en el entorno.
6. **Todo se prueba contra el simulador** (ADR-029 §11). Ninguna prueba, ningún CI y ningún desarrollo llama a Meta. **El número de producción solo se conecta con la autorización escrita del propietario** (F7-13k).

### 2. Alta por Embedded Signup en modo coexistencia

**Backend (F7-13d).** Ruta nueva bajo `/api/v1/o/<slug>/channels/whatsapp/embedded-signup/` con `channels.manage`, MFA reciente (D-F7-11), límite de tasa (F7-02e) y CSRF. Recibe `code`, `waba_id`, `phone_number_id`, el tipo de evento (`FINISH_WHATSAPP_BUSINESS_APP_ONBOARDING` o `FINISH`) y las dos elecciones de sincronización (§8, §9). Todo lo del navegador es **dato no confiable**. En un solo comando:

1. Valida formatos (como `connect_whatsapp`) y que el código llegue dentro de su vida (30 s; si no, error claro y se repite el flujo).
2. Intercambia el código por el token **en el servidor**, por `core.http` (allowlist `graph.facebook.com`, sin redirecciones), con `META_APP_ID` y el primer secreto de `META_APP_SECRETS`. La URL lleva el `client_secret` en la query: **ni la URL ni la respuesta se registran nunca**; una prueba lo comprueba sobre los registros, la auditoría y el outbox.
3. Con ese token, lee el número y comprueba que pertenece al WABA (el mismo `graph.phone_number` de F7-02c) y, en coexistencia, que `is_on_biz_app` sea `true` y `platform_type` `CLOUD_API`. Si no cuadra, no guarda nada.
4. Suscribe la app al WABA (`POST /<WABA_ID>/subscribed_apps`).
5. Guarda la cuenta `PENDING` con `connection_mode`, la credencial cifrada (ADR-022, propósito `channel.access_token`) y `coexistence_onboarded_at`. **No llama a `register`** en coexistencia.
6. Encola las sincronizaciones elegidas (§8, §9) en una tarea con su propio estado en la cuenta (`contacts_sync_state`, `history_sync_state`: `NOT_REQUESTED`, `REQUESTED`, `DECLINED`, `IN_PROGRESS`, `COMPLETE`, `EXPIRED`) y plazo de 24 h desde el alta. Cada petición se hace **una sola vez** (Meta no permite más): se marca `REQUESTED` en la base **antes** de llamar, y un fallo ambiguo no se reintenta a ciegas; se muestra para que el propietario decida.
7. Audita `channel.connected` con ids y el modo, nunca con valores. La cuenta pasa a `CONNECTED` con el primer evento firmado, como hoy.

Ajustes nuevos, sin secretos: `META_APP_ID` y `META_EMBEDDED_SIGNUP_CONFIG_ID` (los entrega la API de configuración al frontend; no son `NEXT_PUBLIC_*` ni se compilan). La conexión manual con token pegado (F7-02c) sigue para `CLOUD_API`; **no** sirve para coexistencia.

**Pantalla (F7-13e).** Un botón «Conectar con la app WhatsApp Business» en la pantalla de canales (F7-07b) que:

- Carga el SDK de Meta **solo en esa ruta**, con el nonce de la CSP.
- Excepción de cabeceras **solo para esa ruta** y documentada en `security-boundaries.md` B1: `frame-src` y `connect-src` con los orígenes de Meta que el flujo necesite, y `Cross-Origin-Opener-Policy: same-origin-allow-popups` si se confirma que el popup necesita `window.opener` (SIN VERIFICAR 7). El resto del frontend no cambia.
- Acepta `postMessage` **solo** de una lista exacta de orígenes `https://` de Meta (nunca `endsWith('facebook.com')`, que acepta dominios ajenos), con el tipo `WA_EMBEDDED_SIGNUP`, y envía el código al backend en el acto (vive 30 s).
- Antes de abrir el flujo, muestra lo que cambia en el teléfono (tabla del Context), pide las dos elecciones de sincronización y, si el propietario no ha autorizado conectar un número real (F7-13k), solo permite números de prueba en entornos que no son `production`.

### 3. Webhook: los campos nuevos (F7-13f)

- **Suscripción:** la app se suscribe a `messages`, `smb_message_echoes`, `history`, `smb_app_state_sync` y `account_update`.
- **Tope de cuerpo:** `BODY_MAX` pasa de 1 MiB al máximo documentado (**3 MB**, se usa 3 MiB) y el proxy de producción a un valor algo mayor. Se mantienen la firma antes de parsear, la profundidad y los textos acotados.
- **Ítems y claves** (siempre `INSERT … ON CONFLICT DO NOTHING`):

| Campo | Ítem de `webhook_ingress` | Clave |
| --- | --- | --- |
| `smb_message_echoes` | Uno por eco (`kind = echo`) | `wa:echo:{id}` |
| `history` | **Uno por trozo** (`kind = history`), no por mensaje: un trozo puede traer miles | `wa:history:{phone_number_id}:{phase}:{chunk_order}:{sha256[:32]}` |
| `history` con `errors` (`2593109`) | Uno (`kind = history_declined`) | `wa:history:{phone_number_id}:declined` |
| `smb_app_state_sync` | Uno por contacto y acción (`kind = contact_sync`) | `wa:contact:{user_id}:{action}:{timestamp}` |
| `account_update` | Uno por evento (`kind = account`) | `wa:account_update:{sha256[:32]}` (como hoy) |

- Los límites por lista (`MAX_ROWS`, `MAX_ITEMS`) siguen para los ecos y los contactos. El trozo de historial se valida en su procesador (F7-13j), no en la petición, con su propio tope por trozo.
- **Enrutado:** ecos, historial y contactos traen `metadata.phone_number_id` y usan `resolve_channel_account` con el cruce obligatorio del WABA, igual que `messages`. Además, el procesador exige que la cuenta resuelta tenga `connection_mode = COEXISTENCE`: un eco o un historial para una cuenta `CLOUD_API` queda `IGNORED` y alerta.
- **`account_update` no trae `phone_number_id`.** Se enruta con una función `SECURITY DEFINER` nueva y mínima, `resolve_channel_accounts_by_parent('WHATSAPP', waba_id)` (ADR-002 §3.3: propietario explícito, `search_path` fijo con `pg_temp`, argumentos tipados, solo cuentas vivas, retorno `(organization_id, channel_account_id, display_identifier)` y nada más). Si el WABA aparece en más de una organización, el ítem queda `IGNORED` y alerta (no debería pasar nunca). El número visible del evento solo filtra entre las cuentas de **esa** organización.
- **Desconexión desde el teléfono:** `PARTNER_REMOVED` o `ACCOUNT_OFFBOARDED` no desconectan a ciegas (un evento forjado con el secreto robado podría apagar el canal de otra organización): la cuenta pasa a `DEGRADED` con un código nuevo (`APP_DISCONNECTED`), se encola una comprobación de salud y **solo** si la Graph API confirma que el número ya no está en coexistencia o el token ya no vale, pasa a `DISCONNECTED`. `ACCOUNT_RECONNECTED` también se confirma antes de volver a `CONNECTED`. Se avisa a quien tenga `channels.manage`.
- **Desconectar desde el CRM** en coexistencia: deja la cuenta `DISCONNECTED`, **no** llama al desregistro (Meta lo prohíbe) y quita la suscripción del WABA si Meta lo admite (SIN VERIFICAR 9). La pantalla explica que la desconexión real se hace en la app del teléfono.

### 4. Ecos: lo que el negocio envía desde la app (F7-13g y F7-13h)

**Modelo.** Sin tabla nueva. Dos cambios en `messages` (una migración, con su funcionalidad):

- `sender_type` admite `BUSINESS_APP`: «una persona del negocio desde la app del teléfono», sin usuario del CRM (`sender_user_id` nulo; las restricciones `messages_contact_ck` y `messages_sender_user_ck` lo permiten tal cual).
- Columna `origin` (`CRM` por omisión | `PHONE_APP` | `HISTORY`): de dónde entró el mensaje al CRM. `CRM` es lo escrito en el CRM o recibido en vivo por `messages`; `PHONE_APP`, un eco; `HISTORY`, la importación (§8).

**Un eco** se registra como `direction = OUTBOUND`, `sender_type = BUSINESS_APP`, `origin = PHONE_APP`, `external_message_id = id`, `provider_timestamp` = `timestamp` acotado a «ahora», y `delivery_status = SENT` (ya salió; si llegan estados, avanzan por el mismo `advance_delivery`).

**Sin duplicados:**

- La ingesta deduplica por `wa:echo:{id}`; el mensaje, por el índice único `(organization_id, channel_account, external_message_id)`. Un eco repetido o de un mensaje que ya existe (por ejemplo, si la Cloud API también emitiera ecos de sus envíos, SIN VERIFICAR 3) no escribe nada.
- **Carrera con un envío del CRM:** si la cuenta tiene mensajes `SENDING` en esa conversación y el eco trae un id desconocido, el ítem falla de forma transitoria y se reintenta con la espera de ADR-029 §4 (los primeros intentos cubren la carrera, como los estados de F7-06a); después se crea. Así un eco nunca se adelanta a guardar el id de un envío del CRM y rompe el índice único.
- El contacto se busca por el teléfono de `to` / `contacts[].wa_id` con el mismo bloqueo de `inbound.py`. **Si no hay teléfono** (usuario con nombre de usuario, solo `to_user_id`), el eco queda `IGNORED` con el motivo `NO_PHONE` y una métrica, hasta que exista la identidad por BSUID (§12). No se crea un contacto vacío.
- La conversación: la abierta de ese contacto en esa cuenta o, si no hay, una nueva con `open_conversation` (el cliente escribió desde el teléfono a alguien sin hilo en el CRM).

**Política:** un eco **no** pasa por `MessagingPolicyService` (ya se envió), **no** cambia `last_inbound_at` (la ventana sigue anclada en el cliente; Meta confirma que la app no la afecta), **no** dispara palabras de baja ni la IA, y **no** genera notificaciones de mensaje nuevo del cliente. Si el contacto está bloqueado o tiene una baja que alcanza a WhatsApp, el eco se guarda igual, se marca en el chat («enviado desde el teléfono a un cliente dado de baja») y se cuenta en una métrica y en la auditoría con ids: el CRM no puede impedir lo que se hace en el teléfono, pero lo deja a la vista.

**Atribución:** **no** se atribuye a ningún vendedor (ni al asignado): el CRM no sabe quién tenía el teléfono. El chat dice «Enviado desde la app del teléfono». La conversación pasa a `WAITING_CUSTOMER` como con una respuesta del CRM, sin cambiar la asignación (D-COEX-04).

**Tipos (F7-13g):** `text` y los tipos que el corte no entiende. `messages_unsupported_ck` se amplía para admitir en lo saliente los tipos que hoy son solo entrantes (`UNSUPPORTED`, y `AUDIO` y `VIDEO` para los ecos de F7-13h y el historial) **solo** con `origin = PHONE_APP` o `HISTORY` (un vendedor sigue sin poder enviarlos). **(F7-13h):** `image`, `video` y `document` con el flujo de descarga de medios de F7-09a (el eco trae el id del medio); `edit` cambia el cuerpo mostrado de un mensaje `PHONE_APP` y guarda el original en su historial de cambios del propio mensaje; `revoke` lo marca como eliminado (el cuerpo se sustituye por una frase fija) sin borrar la fila. Un `edit` o `revoke` de un mensaje que no es del teléfono o de otra cuenta se ignora con métrica.

### 5. Mensajes entrantes en vivo

Los entrantes de un número en coexistencia siguen llegando por `messages` y se procesan **sin cambios** (F7-04, F7-09a). Si el negocio responde desde el teléfono, el eco aparece en el mismo hilo.

### 6. Consentimiento y baja

- Lo que envía el CRM sigue igual: ventana, bloqueo y baja en el servidor (ADR-010, F7-05a, F7-08b).
- Lo que envía el teléfono **no** se bloquea (no se puede), pero se marca y se cuenta (§4). La pantalla de canales recuerda que las bajas registradas en el CRM **no** se aplican en la app del teléfono.
- Un `STOP` que el cliente escribe llega por `messages` y revoca como hoy, venga de donde venga la conversación.
- Historial: §8 y D-COEX-05.
- Contactos de la libreta: no conceden ningún consentimiento.

### 7. Contactos de la libreta del teléfono (F7-13i)

- Cada `state_sync` `add` busca el contacto por teléfono normalizado con el bloqueo de `inbound.py`. Recomendación (D-COEX-03): **solo** se enriquece o se crea el contacto si ese número tiene o tuvo una conversación en esa cuenta (en vivo o en el historial importado); los demás se ignoran con métrica. Así no entran al CRM los contactos personales que haya en el teléfono del negocio.
- El nombre de la libreta es dato no confiable (se sanea y se acota como el nombre de perfil, ADR-029 §5) y **solo** rellena el nombre si el contacto del CRM no tiene uno escrito por una persona; **nunca** sobrescribe un dato editado en el CRM ni se usa para buscar o fusionar.
- `remove` **nunca** borra un contacto del CRM: se ignora con métrica.
- Sin teléfono (solo `user_id`): `IGNORED` con `NO_PHONE` (§12).
- Origen de un contacto creado así: `IMPORT`.

### 8. Historial (F7-13j)

- **Se pide solo si el propietario lo elige en el alta** (D-COEX-02): son hasta 6 meses de conversaciones de clientes reales, se pide una sola vez y dentro de 24 h.
- Cada trozo se procesa en orden de `chunk_order` dentro de su fase; un trozo que llega antes que el anterior espera (fallo transitorio) hasta un plazo, y después se procesa igual (los mensajes llevan su `timestamp`). `progress` se guarda en la cuenta (`history_sync_state`, porcentaje) y la pantalla lo muestra.
- **Dónde va:** cada hilo importado crea una conversación propia `RESOLVED`, sin asignación, sin SLA, sin IA y sin notificaciones, con sus mensajes `origin = HISTORY` ordenados por `provider_timestamp`. La conversación en vivo del mismo contacto no se toca; el chat enlaza «historial anterior a la conexión».
- **Dirección:** si `from` es el número del negocio, `OUTBOUND` con `sender_type = BUSINESS_APP` y `delivery_status` según `history_context.status` (`error` → `FAILED` con el código propio `PROVIDER_REJECTED`; `pending` → `SENT`); si no, `INBOUND` de `CONTACT`.
- **Sin duplicados:** por el mismo índice único de `external_message_id` (un mensaje del día del alta que también llegó en vivo no se repite) y por la clave de cada trozo.
- **Nunca** cambia `last_inbound_at` de una conversación en vivo ni abre ventana (conservador: la ventana solo la abren los entrantes en vivo).
- **Sin confirmaciones ni palabras de baja automáticas** en lo importado; ver D-COEX-05.
- Contactos: se crean con origen `IMPORT` bajo el bloqueo por teléfono, con un tope propio por importación (no el tope por minuto del entrante en vivo), y se ignoran los hilos sin teléfono (§12).
- Medios del historial: sin id no se pueden bajar; se muestran con la etiqueta fija del tipo («Imagen del historial, no disponible»). Bajar los de los últimos 14 días es opcional y queda fuera del primer corte (D-COEX-10).
- `2593109` (el negocio no compartió el historial) deja `history_sync_state = DECLINED`, visible, sin error.

### 9. Rendimiento y salud

- El limitador por cuenta de F7-05c usa como techo el **rendimiento fijo de 20 mensajes por segundo** de la coexistencia, muy por debajo como hoy.
- La comprobación de salud (F7-06b) lee además `is_on_biz_app` y `platform_type` en las cuentas `COEXISTENCE` y avisa si el número salió de la coexistencia. La pantalla recuerda que el teléfono principal no debe quedar inactivo **unos 14 días** (Meta desconecta).

### 10. Aislamiento entre organizaciones

- El tenant **siempre** sale de la función de enrutado (`resolve_channel_account` o la nueva por WABA), nunca del payload; el cruce `entry.id == external_parent_id` es obligatorio para todos los campos nuevos (prueba por campo: «evento firmado para un número de otra organización»).
- `user_id`, `to_user_id`, `from_user_id`, `phone_number`, `full_name` y los textos son datos personales: se guardan solo en el payload de la ingesta (que se vacía al procesarse, ADR-029 §3) y en las tablas del tenant; nunca en registros, métricas ni auditoría.
- Un eco, un trozo o un contacto solo escriben en la cuenta resuelta; un `edit` o `revoke` solo toca mensajes de esa cuenta.

### 11. Simulador (F7-13c)

El simulador construye y firma los cuatro campos nuevos (con los casos sin teléfono, desordenados, repetidos y el `2593109`) y la Graph API falsa responde a `oauth/access_token`, `subscribed_apps`, `smb_app_data`, `?fields=is_on_biz_app,platform_type` y sus errores. Las fixtures se marcan con la versión que se verifique al implementar (hoy `v23.0`; el ejemplo de la página de implementación usa la `v27.0` [S5]): subir la versión es una revisión del contrato (D-F7-08).

### 12. Fuera de este ADR

- **Identidad por BSUID** (clientes sin teléfono en los webhooks): afecta también al entrante de F7-04 y merece su propio ADR o enmienda, después de medir cuántos mensajes llegan sin teléfono (F7-14, #749). Hasta entonces, coexistencia ignora con métrica lo que llega sin teléfono.
- **Campañas** por WhatsApp: siguen dependiendo de las plantillas (F7-10) y del consentimiento; la coexistencia no las cambia.
- **Llamadas y estados**: se quedan en el teléfono (no hay API en coexistencia).
- **Grupos**: no se sincronizan.
- **Kommo**: no se integra.

### 13. Decisiones que necesitan al propietario

| ID | Pregunta | Recomendación | Bloquea |
| --- | --- | --- | --- |
| D-COEX-01 | ¿La app del despliegue se convierte en **Tech Provider** (verificación del negocio, App Review con dos videos, acceso avanzado a `whatsapp_business_management` y `whatsapp_business_messaging`) o se usa un Solution Partner? | Tech Provider con la app propia: mantiene D-CH-1 (sin intermediario) y D-F7-01. Empezar ya la verificación del negocio; mientras, todo se construye contra el simulador | F7-13k (y probar con una app real) |
| D-COEX-02 | ¿Importar el historial de 6 meses? Se pide una sola vez y dentro de 24 h del alta | Sí, solo si el aviso de privacidad lo cubre; si no, no pedirlo (no se puede pedir después) | F7-13j, F7-13k |
| D-COEX-03 | ¿Qué contactos de la libreta del teléfono entran al CRM? | Solo los que tienen o tuvieron conversación en la cuenta | F7-13i |
| D-COEX-04 | ¿Cómo cuenta una respuesta desde el teléfono? | Sin vendedor atribuido; la conversación pasa a `WAITING_CUSTOMER`; no cambia la asignación | F7-13g |
| D-COEX-05 | ¿Qué hacer con un `STOP` o `ALTA` del historial? | Aplicar la última palabra de cada contacto en orden cronológico, sin enviar confirmación, con la evidencia del mensaje importado, salvo que el CRM ya tenga un cambio de consentimiento posterior | F7-13j |
| D-COEX-06 | ¿Quién responde dónde? Dos personas pueden contestar a la vez (CRM y teléfono) | Los vendedores en Good Doggy; el teléfono para llamadas, estados y excepciones; el chat muestra los ecos en tiempo real | F7-13k (operación) |
| D-COEX-07 | El número: primero uno de prueba en coexistencia (otro teléfono con la app Business) y después el de la tienda | Sí; **el número de la tienda solo con autorización escrita del propietario** y en la ventana que él elija | F7-13k |
| D-COEX-08 | Aceptar lo que se pierde en el teléfono: mensajes temporales, ver una vez, ubicación en tiempo real, listas de difusión nuevas, acompañantes Windows y WearOS | Aceptar, informando al equipo antes del alta | F7-13k |
| D-COEX-09 | Precios: lo enviado desde el teléfono es gratis; lo del CRM sigue el precio por mensaje vigente (1.000 de servicio gratis al mes por número) | Revisar el coste con el volumen real antes de activar | F7-13k |
| D-COEX-10 | ¿Bajar los medios del historial de los últimos 14 días? | No en el primer corte | F7-13j |
| — | ADR-029 sigue `Proposed` | Aceptar ADR-029 (o decidir sobre él) a la vez o antes que este | F7-13b |

## Alternatives considered

| Alternativa | Por qué se descarta |
| --- | --- |
| Un número nuevo solo para la Cloud API (la recomendación de ADR-029 D-F7-02) | No cumple el requisito: la tienda quiere un solo número en el teléfono y en el CRM. Sigue siendo el plan B si Meta no admite la coexistencia para el número |
| Migrar el número a la Cloud API y dejar la app del teléfono | Se pierden la app, las llamadas y los estados del número, que el propietario quiere conservar |
| Un Solution Partner (BSP) que haga el alta | Otro contrato, otro coste y otro punto de falla; contradice D-CH-1. Queda como salida si Meta no aprueba la app como Tech Provider (D-COEX-01) |
| Kommo u otro CRM intermedio | Ya no se usa; añadiría otro procesador de datos de clientes |
| WhatsApp Web, bibliotecas no oficiales, sesiones del teléfono | Prohibidas (ADR-029 §1) |
| Guardar los ecos y el historial en tablas propias, fuera de `messages` | Duplicaría el chat, la búsqueda y la deduplicación; con `origin` y `sender_type` basta |
| Atribuir los ecos al vendedor asignado | Sería falso: el CRM no sabe quién tenía el teléfono |
| Meter el historial en la conversación en vivo | Desordenaría el chat (se ordena por llegada) y podría reabrir ventanas o colas |
| Aceptar `postMessage` de cualquier dominio que termine en `facebook.com` (el ejemplo oficial) | Lo cumple un dominio ajeno (`evilfacebook.com`); se usa una lista exacta |
| Relajar la CSP y el COOP en todo el frontend | Solo la ruta del alta los necesita |

## Consequences

- El mismo número sirve para el teléfono y para Good Doggy; el CRM ve lo que se responde desde el teléfono y, si se elige, los 6 meses anteriores.
- `channel_accounts` gana `connection_mode`, `coexistence_onboarded_at` y el estado de las dos sincronizaciones; `messages`, `origin` y el valor `BUSINESS_APP`; `STATUS_DETAILS`, `APP_DISCONNECTED`. Cada migración llega con su funcionalidad.
- El webhook acepta cuerpos de hasta 3 MiB y una función `SECURITY DEFINER` nueva para enrutar por WABA.
- La app de Meta tiene que pasar a Tech Provider: trámite de Meta, con plazos que no controla el programa.
- Lo que se haga en el teléfono queda fuera de la política del CRM: se ve y se cuenta, no se impide.
- Si el propietario no pide el historial en las primeras 24 h, ya no se puede pedir sin desconectar y repetir el alta.

## Security implications

- **Intercambio del código:** solo en el servidor, con el app secret del entorno; la URL con `client_secret` y la respuesta con el token nunca se registran; el token se cifra al momento (ADR-022) y no vuelve al navegador.
- **Datos del navegador** (`waba_id`, `phone_number_id`, evento): no confiables; se verifican contra la Graph API con el token antes de guardar nada.
- **Cabeceras:** la excepción de CSP y COOP se limita a la ruta del alta y se documenta en B1; `postMessage` con lista exacta de orígenes.
- **Webhook:** firma antes de parsear, tope de 3 MiB, enrutado solo por funciones mínimas, cruce del WABA obligatorio, `connection_mode` comprobado; `PARTNER_REMOVED` y `ACCOUNT_RECONNECTED` se confirman con la Graph API antes de cambiar el estado.
- **Datos personales:** el historial y la libreta son datos de clientes y de terceros: se importan solo si el propietario lo elige, el payload se vacía al procesar y nada personal va a registros o métricas.
- **Repositorio público:** ningún número, id de cuenta, nombre ni dato de la tienda en código, pruebas, issues ni documentos; se dice «el número de la tienda».

## Operational implications

- Variables nuevas sin secreto: `META_APP_ID`, `META_EMBEDDED_SIGNUP_CONFIG_ID`. El proxy acepta cuerpos de más de 3 MiB en `/webhooks/meta/`.
- La app de Meta: Tech Provider, Facebook Login for Business con su configuración de Embedded Signup v4 o posterior, dominio con HTTPS en «Allowed domains», y suscripción a los cinco campos del webhook.
- Runbook de alta y de incidencias de coexistencia (F7-13k): checklist del teléfono (versión 2.24.17+, app abierta durante la sincronización, acompañantes), las 24 h de la sincronización, qué hacer si llega `PARTNER_REMOVED`, y la **firma del propietario** antes de conectar el número de la tienda.
- Métricas: ecos por resultado, ecos a clientes dados de baja, trozos de historial y progreso, contactos sincronizados e ignorados, eventos sin teléfono, desconexiones desde el teléfono.

## Fuentes (leídas el 2026-11-13)

- [S1] Onboarding WhatsApp Business app users (coexistence): https://developers.facebook.com/documentation/business-messaging/whatsapp/embedded-signup/onboarding-business-app-users
- [S2] Become a Tech Provider: https://developers.facebook.com/documentation/business-messaging/whatsapp/solution-providers/get-started-for-tech-providers
- [S3] Embedded Signup overview: https://developers.facebook.com/documentation/business-messaging/whatsapp/embedded-signup/overview/
- [S4] Onboarding customers as a Tech Provider: https://developers.facebook.com/documentation/business-messaging/whatsapp/embedded-signup/onboarding-customers-as-a-tech-provider
- [S5] Embedded Signup implementation: https://developers.facebook.com/documentation/business-messaging/whatsapp/embedded-signup/implementation
- [S6] Webhook `smb_message_echoes`: https://developers.facebook.com/documentation/business-messaging/whatsapp/webhooks/reference/smb_message_echoes
- [S7] Webhook `history`: https://developers.facebook.com/documentation/business-messaging/whatsapp/webhooks/reference/history
- [S8] Webhook `smb_app_state_sync`: https://developers.facebook.com/documentation/business-messaging/whatsapp/webhooks/reference/smb_app_state_sync
- [S9] Webhook `account_update`: https://developers.facebook.com/documentation/business-messaging/whatsapp/webhooks/reference/account_update
- [S10] Webhooks overview: https://developers.facebook.com/documentation/business-messaging/whatsapp/webhooks/overview
- [S11] Pricing: https://developers.facebook.com/documentation/business-messaging/whatsapp/pricing
- [S12] Account model evolution, onboarding: https://developers.facebook.com/documentation/business-messaging/whatsapp/account-model-evolution/onboarding/
- [S13] Business-scoped user IDs: https://developers.facebook.com/documentation/business-messaging/whatsapp/business-scoped-user-ids

No se encontró en business.whatsapp.com ni en faq.whatsapp.com una página oficial sobre la coexistencia; la ruta de desconexión en la app sale de [S1].
