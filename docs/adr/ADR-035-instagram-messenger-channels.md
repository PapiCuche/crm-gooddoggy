# ADR-035: Canales de Instagram (mensajes directos) y Facebook Messenger con las API oficiales de Meta

- **Status:** Proposed
- **Date:** 2026-11-13
- **Deciders:** propuesto por el programa autónomo (ADR-015 §5) para la decisión del propietario. Mientras esté `Proposed` no obliga al código y ningún work item de código de la serie F7b empieza: todos dependen de la aceptación (F7b-02, #775). Las preguntas que necesitan respuesta del propietario están en §13.
- **Work items:** F7b-01 #774 (archiva este ADR), F7b-02 #775 (aceptación y D-F7-09), F7b-03 #776 (simulador), F7b-04 #777 (webhook entrante, enrutado e identidades), F7b-05 #778 (adaptadores de salida y política), F7b-06 #779 (pantalla de canales; cierra la serie), F7b-07 #780 (investigación de TikTok, solo documentos).
- **Related:** ADR-001, ADR-002 §3.3, ADR-003, ADR-007, ADR-008, ADR-010, ADR-011, ADR-012 §6–7, ADR-022, ADR-027, ADR-029 (todo el patrón del canal), ADR-032 (Proposed; ecos y onboarding), `docs/architecture/tenancy-context.md` §5, `docs/fase-0/02-modelo-de-datos.md` §E.5 y §E.6, [phase-7.md](../phases/phase-7.md) (Fase 7b y D-F7-09)

## Context

El propietario quiere que el Inbox atienda también los **mensajes directos de Instagram** y los de **Messenger** (la página de Facebook de la tienda), después de WhatsApp y solo por las vías oficiales de Meta. El roadmap de la fase 0 los ponía en la Fase 12; [phase-7.md](../phases/phase-7.md) los adelanta como «Fase 7b» y deja el orden a la decisión D-F7-09.

Este ADR no inventa un canal nuevo: dice qué cambia respecto a WhatsApp (ADR-029) para que los dos canales entren por **el mismo patrón**: los mismos contactos, conversaciones, asignaciones, auditoría y permisos, con las particularidades de cada plataforma.

### Lo que ya existe y este ADR no cambia

| Pieza                                                                                                                                                                                                                                                                                                                                                                                          | Dónde                                        | Estado                                                                                                 |
| ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------- | ------------------------------------------------------------------------------------------------------ |
| `ChannelAdapter` (`send(claim) -> SendResult`, llamado fuera de toda transacción; `TemporaryChannelError` y `AmbiguousChannelError`), `SandboxAdapter`, `adapter_for(platform)`                                                                                                                                                                                                                | `apps.conversations.channels`                | Construido; `adapter_for` solo conoce `SANDBOX` y `WHATSAPP`                                           |
| `Claim.recipient` es el teléfono E.164 del contacto y `Claim.phone_number_id` el `external_account_id`                                                                                                                                                                                                                                                                                         | `channels.Claim`                             | Construido; hay que generalizarlo (§6)                                                                 |
| `channel_accounts`: `platform` (`SANDBOX`, `WHATSAPP`), `external_account_id` **único entre las vivas de toda la plataforma** (`channel_accounts_external_uq` sobre `platform, external_account_id`), `external_parent_id`, `credential_id`, `status` y `status_detail`, CHECK por plataforma (`channel_accounts_whatsapp_ck`, `channel_accounts_sandbox_ck`, `channel_accounts_connected_ck`) | `models.ChannelAccount`                      | Construido                                                                                             |
| `credentials`: token cifrado con `core.sealed` (propósito `channel.access_token`, dueño `channel_account:<id>`), `last_four`, `expires_at`, rotar y revocar                                                                                                                                                                                                                                    | `models.Credential`, ADR-022                 | Construido                                                                                             |
| Webhook `GET` y `POST /webhooks/meta/`: cuerpo crudo con tope (`BODY_MAX`, 1 MiB), firma `X-Hub-Signature-256` contra **cada** secreto de `META_APP_SECRETS` antes de parsear, `401` vacío si falla, `200` con firma válida aunque el cuerpo no valga, `503` solo si la base no guarda                                                                                                         | `meta_webhook.py`                            | Construido; solo acepta `object == "whatsapp_business_account"`                                        |
| Ingesta `webhook_ingress` solo por funciones `SECURITY DEFINER` (`webhook_ingress_insert`, `_claim`, `_mark`, `_list`); el payload se vacía al cerrar el ítem                                                                                                                                                                                                                                  | `ingress.py`, migraciones `0008` y `0009`    | Construido; `webhook_ingress_insert` exige un WABA numérico y `kind` en (`message`, `status`, `other`) |
| Enrutado del tenant por `resolve_channel_account(p_platform, p_phone_number_id)`, que devuelve `(organization_id, channel_account_id)` y nada más, y cruce obligatorio del WABA con `external_parent_id`                                                                                                                                                                                       | `ingress.route`, `0009`                      | Construido; la función solo responde para `WHATSAPP`                                                   |
| «Webhook visto»: el primer evento firmado lleva una cuenta `PENDING` a `CONNECTED`                                                                                                                                                                                                                                                                                                             | `ingress.seen`                               | Construido                                                                                             |
| Contacto del entrante por teléfono con `pg_advisory_xact_lock`, sin índice único; `Contact.Source.INBOUND_MESSAGE`; `display_name` obligatorio                                                                                                                                                                                                                                                 | `inbound.py`, `apps.contacts`                | Construido                                                                                             |
| Política: `ChannelCapabilities(window_hours)` por plataforma, ventana anclada en `last_inbound_at`, `WINDOW_CLOSED`, baja solo para lo proactivo                                                                                                                                                                                                                                               | `policy.py`, ADR-010                         | Construido; `INSTAGRAM` y `MESSENGER` no tienen capacidades                                            |
| `contact_identities`                                                                                                                                                                                                                                                                                                                                                                           | Diseñada en `02` §E.5                        | **No existe** en el código                                                                             |
| Cuenta de canal, conectar con token pegado y verificado, rotar con MFA reciente (`step_up`), desconectar; permiso `channels.manage`                                                                                                                                                                                                                                                            | F7-02c/d/e, `/o/{slug}/canales` (F7-07b/c/d) | Construido para WhatsApp                                                                               |
| Simulador de Meta (webhooks firmados y Graph API falsa), fixtures `v23.0`                                                                                                                                                                                                                                                                                                                      | `backend/tests/meta_sim`                     | Construido para WhatsApp                                                                               |
| `META_GRAPH_VERSION = "v23.0"`, una sola versión para todo el despliegue                                                                                                                                                                                                                                                                                                                       | `config/settings/base.py`                    | Construido                                                                                             |

### Lo que dice Meta (fuentes primarias oficiales, todas leídas el 2026-11-13)

Cada afirmación lleva su fuente ([S…], lista al final, todas leídas el **2026-11-13**). Lo que no se pudo comprobar está marcado **SIN VERIFICAR (UNVERIFIED)** y no se usa como premisa sin probarlo con una app de pruebas. Las rutas antiguas `developers.facebook.com/docs/instagram-platform/...` y `/docs/messenger-platform/...` redirigen hoy a `developers.facebook.com/documentation/...`.

#### Instagram

**Tres formas de integrarse** (tabla comparativa de [S1]):

|                         | Unified Login                                 | Instagram Login                              | Facebook Login                                        |
| ----------------------- | --------------------------------------------- | -------------------------------------------- | ----------------------------------------------------- |
| Uso que recomienda Meta | Integraciones nuevas                          | Integraciones existentes con Instagram Login | Integraciones existentes vinculadas a una página      |
| Token                   | Business Integration System User Access Token | Instagram User access token                  | Facebook User o Page access token                     |
| Host de la API          | `graph.facebook.com`                          | `graph.instagram.com`                        | `graph.facebook.com`                                  |
| Página de Facebook      | No hace falta                                 | No hace falta                                | **Obligatoria**                                       |
| Cartera de negocio      | Obligatoria                                   | No la exige el login                         | Según la configuración de Facebook Login for Business |

- Unified Login «is recommended for new integrations» y permite entrar «with either Instagram or Facebook, without a Facebook Page» [S1]. «New Instagram API features are released only for this setup» [S3].
- **Instagram Login está cerrado para apps nuevas:** «Newly created apps can't set up the Instagram API with Instagram Login.» Las existentes siguen: «There is no end date yet.» [S2]
- Unified Login usa **el app ID y el app secret de la app de Meta** («not your Instagram app ID and secret»); el token «belongs to your client's business portfolio, not to the person who logged in», su caducidad «depends on your configuration» y deja de valer si el cliente quita la app de su cartera o mueve la cuenta [S4]. En su lanzamiento **no** admite el Handover Protocol, y las suscripciones de webhooks de Instagram Login «don't carry over»; los ids de mensaje tienen otro formato [S2].
- Instagram Login tiene credenciales propias («Instagram App ID», «Instagram App Secret»); su token largo vale **60 días** y se renueva si tiene al menos 24 h y no caducó [S15].
- Con Facebook Login hace falta «A Facebook Page connected to that account»; un Page access token obtenido de un token de usuario largo «has no expiration date» [S11].
- **Cuenta profesional** (empresa o creador) [S1]. Una cuenta de creador tiene que hacer una petición a la Conversations API antes de recibir webhooks [S12].
- **«Allow access to messages»:** el titular la activa en la app de Instagram (Ajustes > Mensajes y respuestas a historias > Controles de mensajes > Herramientas conectadas) [S11]. Si está apagada, la API responde `(#200)` «The account owner has disabled access to Instagram Direct Messaging…» (código `200`, subcódigo `2534041`) [S26].

**Permisos y revisión.**

- Facebook Login: `instagram_basic`, `instagram_manage_messages`, `pages_manage_metadata`, `pages_show_list`, `business_management` [S12]; el resumen de [S1] nombra también `pages_read_engagement`. Unified Login usa la familia `instagram_*` con equivalencias (`instagram_business_manage_messages` → `instagram_manage_messages`).
- Los ámbitos `business_basic`, `business_manage_messages`… de Instagram Login se retiraron el **27 de enero de 2025** en favor de `instagram_business_*` [S15].
- **Acceso avanzado** «required if your app serves Instagram professional accounts that you don't own or manage… This access level requires App Review and Business Verification» [S1]. El acceso estándar es para apps usadas solo por personas con rol en la app [S1] [S32].
- La función **Human Agent** «requires successful completion of the App Review process» y «is only available with business verification» [S31].
- La app tiene que estar publicada (**Live**) para recibir notificaciones de los usuarios [S5].
- Entre los ajustes de la app que pide la revisión está la **URL de la política de privacidad** [S10]; la política tiene que explicar cómo pedir el borrado de los datos y la app **puede** implementar una devolución de llamada de borrado [S33].

**Webhooks.**

- `object` es `instagram`; `entry.id` es «The Instagram professional account ID of your app user»; en un entrante, `sender.id` es el IGSID del cliente y `recipient.id` la cuenta; en un eco es al revés [S7].
- Campos del mensaje: `mid`, `text`, `attachments`, `quick_reply`, `referral`, `is_deleted`, `is_echo`, `is_unsupported`, `reply_to.mid` y `reply_to.story` (respuesta a una historia). Adjuntos: `image` (incluye gif y sticker), `audio`, `video`, `file`, `share`, `ig_post`, `ig_reel`, `story_mention` y otros; también `reaction`, `postback`, `read` y `message_edit` [S7].
- Con Facebook Login los ecos vienen dentro del campo `messages` [S6] [S17].
- **Firma:** `X-Hub-Signature-256` con `sha256=`, «using the payload and your app's App Secret» [S5]. Messenger aclara que Meta firma «an escaped unicode version of the payload, with lowercase hex digits» [S17].
- **SIN VERIFICAR (UNVERIFIED) 1:** qué secreto firma los webhooks de Instagram con **Instagram Login** (el de la app de Meta o el «Instagram App Secret»). Ninguna página lo dice; con Unified Login y Facebook Login el webhook se configura en la app de Meta.
- Lotes de hasta **1000** actualizaciones, sin garantía de agrupar; reintentos «immediately, then… a few more times with decreasing frequency over the next 36 hours»; «Unacknowledged responses will be dropped after 36 hours» y hay que deduplicar [S5]. El orden de entrega no está garantizado: hay que usar el `timestamp` [S17].
- **Suscripción por cuenta:** con Facebook Login, `POST` al `/subscribed_apps` **de la página vinculada** con el Page access token; con Unified Login, `POST graph.facebook.com/<IG_ID>/subscribed_apps` con el token de sistema [S5].

**Ventana y etiquetas.**

- La ventana estándar «For most user actions, it lasts 24 hours»; tras un anuncio Click-to-Direct puede durar hasta 7 días [S1].
- La etiqueta `human_agent` permite responder «within 7 days of a user's message», solo para atención humana cuando el problema no se resolvió en la ventana estándar [S1] [S31].
- Desde el **27 de abril de 2026**, `CONFIRMED_EVENT_UPDATE`, `ACCOUNT_UPDATE` y `POST_PURCHASE_UPDATE` dan error `100` [S20]. Las One-Time Notifications no existen en Instagram [S20].
- Error fuera de ventana: código `10`, subcódigos `2534022` y `2018278` («This message is sent outside of allowed window»); `551` «This person isn't receiving messages from you right now» [S26].

**Identidad.** Un IGSID «represents that person on that app» y es «specific to the person and the Instagram account they are interacting with» [S1]: se trata como único por (app, cuenta profesional). La User Profile API devuelve `name`, `username`, `profile_pic` y otros, solo si el cliente escribió (o pulsó un icebreaker o el menú) [S9]; `profile_pic` caduca «in a few days» [S9]. Ningún campo documentado es un teléfono (que la API nunca lo dé: **SIN VERIFICAR 2**, coherente con la lista de campos).

**Tipos, tamaños y límites.**

- Texto: «UTF-8 and be a 1000 bytes or less» [S8]; otra página dice «less than 1,000 characters» [S14]. Se usa el más estricto: 1000 bytes.
- Adjuntos salientes: imagen png/jpeg hasta 8 MB; audio, video y PDF hasta 25 MB [S8].
- **Menciones en historias:** «You must not store or cache the media content on your server»; la URL deja de funcionar cuando la historia caduca o se borra [S13].
- Las URL del CDN «will not return the media when the content has been deleted or has expired» [S1]; el plazo exacto de un adjunto entrante: **SIN VERIFICAR 3**.
- Conversaciones nuevas de quien no sigue la cuenta van a «Solicitudes»; las inactivas 30 días no se devuelven en la API; responder desde una app de terceros las mueve a «General» [S8].
- **Límites de tasa** (dos páginas oficiales no coinciden): texto, enlaces, reacciones y stickers, **100** llamadas por segundo por cuenta profesional según [S1] y **300** según [S25]; audio y video, 10 por segundo; Conversations API, 2 por segundo [S1] [S25]. Se usa el más estricto.

#### Messenger

- **Requisitos:** una página de Facebook vinculada a la app, una app de Meta con el caso de uso de Messenger, un Page access token y los webhooks [S16]. La App Review «Not required if you only send and receive messages for your own Facebook Page» [S16], pero los Page tokens «only allow your Page to interact with Facebook accounts that have been granted the Administrator, Developer, or Tester role» hasta la revisión [S27]. **Las dos páginas oficiales chocan (SIN VERIFICAR 4):** el plan supone que hace falta acceso avanzado para hablar con clientes sin rol en la app, y la verificación del negocio si la usan personas sin rol [S16] [S32].
- **Permisos:** `pages_messaging`, `pages_manage_metadata`, `pages_show_list`, `pages_read_engagement`, `business_management` [S16] [S28]; `business_management` es dependencia de `pages_messaging`, `pages_show_list` e `instagram_manage_messages` [S16].
- **Tokens:** «Page access tokens are unique to each Page, admin, and app combination»; un Page token largo «do[es] not have an expiration date» y solo caduca o se invalida en ciertas condiciones [S36]. El usuario del sistema del Business Manager genera tokens que «nunca vencen» o «válidos por 60 días», y Meta recomienda los que vencen [S35]. Un token de sistema exige que la app y la página sean de la misma cartera [S28].
- **Webhooks:** `object` es `page`; `entry[].messaging[]` con `sender.id` (el PSID) y `recipient.id` (la página) [S18]. Campos útiles: `messages`, `message_echoes`, `message_deliveries`, `message_reads`, `message_reactions`, `message_edits`, `messaging_postbacks`, `messaging_handovers`, `standby` [S17]. La página se suscribe con `POST /<PAGE_ID>/subscribed_apps?subscribed_fields=…` y un Page token [S17]. Misma firma `X-Hub-Signature-256` con el app secret [S17]. Responder `200` «within 5 or less seconds» [S17] (otras páginas dicen 20 s); si la entrega falla **una hora**, Meta desactiva el webhook y desuscribe la app de la página [S17].
- **Ecos:** «This callback occurs when your page sends a message», con `is_echo` y `app_id` de la app que lo envió [S19].
- **Ventana:** 24 h para la mayoría de acciones, hasta 7 días tras un anuncio Click-to-Messenger [S21]; fuera de ella «The message will fail to send» [S29]. `HUMAN_AGENT`: «manually respond to a person's messages within a 7-day period» [S20], con App Review y verificación del negocio [S31]. Las tres etiquetas antiguas dan error `100` desde el 27-04-2026 [S20]. `messaging_type`: `RESPONSE`, `UPDATE`, `MESSAGE_TAG` [S20]. Los mensajes de utilidad (plantillas, `UTILITY`) y de marketing quedan fuera de este ADR.
- **Conversation routing:** «Meta no longer supports Handover Protocol for Messenger and all the businesses are migrated to Conversation Routing» [S22]. La app por omisión es «the primary app allowed to respond»; la bandeja de Meta Business Suite es «another application connected to your page»; si alguien responde desde la bandeja en una conversación que no controla, «the inbox takes control»; «Do not respond to standby messages» [S22] [S23].
- **Identidad:** un PSID es único «for each Facebook Page they start a conversation with» [S27]. El perfil (`first_name`, `last_name`, `profile_pic`…) exige acceso avanzado a **Business Asset User Profile Access** y solo vuelve si la persona lo hizo público; si no, un objeto vacío [S24] [S37]. Que un mismo PSID valga entre apps de la misma página: **SIN VERIFICAR 5**.
- **Envío:** `POST /<PAGE_ID>/messages` [S20]. Adjuntos: imágenes hasta 8 MB, el resto hasta 25 MB [S30]. Texto: 2000 caracteres (**SIN VERIFICAR 6**: solo se vio un fragmento de la referencia de la Send API, que no carga).
- **Límites de tasa:** «Calls within 24 hours = 200 * Number of Engaged Users»; 300 llamadas por segundo por página para texto, 10 para audio o video; Conversations API, 2 por segundo; error `613` al pasarse [S25].
- **Errores:** `551`, `613`, `190` (token), `100`; `10` con subcódigos de ventana [S26].

**Versión de la Graph API.** La última es la **v27.0**, publicada el 4 de noviembre de 2026 [S34]. El despliegue fija hoy `v23.0` para WhatsApp (D-F7-08).

**SIN VERIFICAR (UNVERIFIED), lista completa** (ninguno se usa como premisa):

1. Qué secreto firma los webhooks de Instagram Login (no se usa Instagram Login: §2).
2. Que la API de Instagram nunca entregue un teléfono.
3. El plazo de caducidad de las URL de los adjuntos entrantes de Instagram y Messenger.
4. Si basta el acceso estándar para hablar con clientes sin rol en la app desde la página propia (dos páginas oficiales chocan).
5. Si el PSID es el mismo para todas las apps de una página.
6. El tope de 2000 caracteres de Messenger.
7. La lista completa de campos a suscribir en `/subscribed_apps` de la página para Instagram con Facebook Login, y si un mensaje enviado desde la app de Instagram o desde Business Suite produce siempre un eco (lo sugiere [S12], no lo afirma).
8. Si una app nueva podrá seguir usando Instagram con Facebook Login sin fecha de fin (Meta la etiqueta «existing Page-linked integrations» [S1] y no anuncia retirada).
9. Si un token de usuario del sistema generado en el Business Manager sirve para la API de Unified Login, o solo el que entrega su flujo de alta.
10. La forma de renovar por API un token de usuario del sistema de 60 días.
11. Que el `message.metadata` enviado vuelva igual en el eco de Messenger y en el de Instagram.
12. Que el eco de Instagram traiga `app_id`.

## Decision

### 1. Alcance y principios

1. **Solo vías oficiales** (ADR-029 §1): Messenger Platform y la API de mensajería de Instagram. Nada de scraping, sesiones personales, bibliotecas no oficiales ni automatización de la app. Si Meta no permite algo que el canal necesita, el canal se aplaza.
2. **Dos plataformas nuevas en el mismo patrón:** `MESSENGER` (una página) e `INSTAGRAM` (una cuenta profesional). Cada una es una `channel_account` con su adaptador detrás de `ChannelAdapter`, su capacidad en la política, su enrutado por función `SECURITY DEFINER` y su simulador. Conversaciones, mensajes, asignación, notas, auditoría y permisos (`conversations.view`, `messages.send`, `channels.manage`) **no cambian**.
3. **Primer corte: texto** entrante y saliente, estados de lectura y entrega, errores normalizados y el marcador «no soportado» para lo demás (ADR-029 §1.5): lo que no se entiende **se ve**, no se pierde. Medios, reacciones, respuestas a comentarios, plantillas de utilidad y marketing llegan después, cada uno con su work item.
4. **Todo se prueba contra el simulador** (F7b-03). Ningún test, CI ni desarrollo llama a Meta. Conectar la página y la cuenta reales es un work item operativo posterior, con las credenciales del propietario (como F7-12b).

### 2. La app de Meta y la variante de Instagram

**Una app de Meta por despliegue** (D-F7-01): la misma app que WhatsApp, con los casos de uso de Messenger e Instagram añadidos. Así la firma de los tres objetos (`whatsapp_business_account`, `page`, `instagram`) se verifica con el mismo `META_APP_SECRETS` y la misma función `signed`, **antes** de conocer el objeto o la organización. Las consecuencias del modelo de app única de ADR-029 §4 (un secreto firma para todas las organizaciones, la rotación afecta a todas) se extienden a los dos canales; las defensas son las mismas: cruce obligatorio de ids (§4), esquema estricto, idempotencia.

Si el propietario eligiera **una app distinta** (D-F7b-03), su secreto se añade a la lista `META_APP_SECRETS` (la verificación ya prueba cada secreto con el mismo trabajo) y la nueva app pasa su propia revisión. La lista crece y un secreto filtrado de cualquiera de las dos firma para todas: se acepta solo con esa advertencia escrita.

**Variante de Instagram: Facebook Login (vinculada a la página) en el primer corte** (D-F7b-02). Motivos:

- Messenger ya exige la página y su Page token; con Facebook Login **un mismo tipo de credencial** sirve para los dos canales y se conecta como WhatsApp hoy: el Owner pega un token de usuario del sistema de su cartera, el servidor lo verifica contra la Graph API y lo guarda cifrado (F7-02c). Sin SDK de Meta en el navegador ni excepción de CSP.
- Usa `graph.facebook.com` (ya en la allowlist de `core.http`) y el app secret de la app de Meta para la firma, que se conoce y se prueba.
- Instagram Login queda descartado: está cerrado para apps nuevas [S2] y su firma es SIN VERIFICAR 1.
- **Unified Login** es lo que Meta recomienda y donde salen las funciones nuevas [S1] [S3], pero su token sale de un flujo de alta con Facebook Login for Business en el navegador (el mismo tipo de pantalla que el Embedded Signup de ADR-032 §2, todavía `Proposed`), no admite aún el Handover Protocol y su compatibilidad con un token de sistema pegado es SIN VERIFICAR 9. **Es la ruta de migración** cuando exista el alta por flujo de Meta (ADR-032) o Meta retire la variante con página (SIN VERIFICAR 8). Migrar es crear una cuenta nueva: los ids de mensaje cambian de formato [S2] y las suscripciones no se heredan.

### 3. Las cuentas de canal

| Campo                                    | `MESSENGER`                                                                                                                  | `INSTAGRAM`                                                                                                                               |
| ---------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------- |
| `external_account_id`                    | Id de la página                                                                                                              | Id de la cuenta profesional de Instagram (el `entry.id` del webhook)                                                                      |
| `external_parent_id`                     | `NULL`                                                                                                                       | Id de la página vinculada (por donde se suscribe y se envía con Facebook Login)                                                           |
| `display_identifier`                     | El nombre de la página                                                                                                       | El `@usuario` de la cuenta                                                                                                                |
| `credential_id`                          | Page access token del usuario del sistema, cifrado (ADR-022, propósito `channel.access_token`, dueño `channel_account:<id>`) | El mismo tipo de token, en **su propia** fila de `credentials` (una credencial por dueño): rotar uno no rota el otro, la pantalla lo dice |
| `status`                                 | `PENDING` → `CONNECTED` con el primer evento firmado (`ingress.seen`), como WhatsApp                                         | Igual                                                                                                                                     |
| `quality_rating`, `messaging_limit_tier` | `NULL` (no existen en estos canales)                                                                                         | `NULL`                                                                                                                                    |

- Una migración nueva (F7b-04) amplía `channel_accounts_platform_ck` y añade **un CHECK por plataforma**, como `channel_accounts_whatsapp_ck`: ids numéricos de Meta (`META_ID`); en Instagram `external_parent_id` obligatorio; en Messenger `NULL`. La inmutabilidad de `platform`, `external_account_id` y `external_parent_id` fuera de `PENDING` (F7-02b) se aplica igual.
- La unicidad global existente (`platform`, `external_account_id`) entre las vivas impide que dos organizaciones conecten la misma página o la misma cuenta; el comando de conectar convierte la violación en el mismo error genérico que un id mal formado (ADR-029 §2), sin nombrar la otra organización.
- **Conectar** (F7b-04 el comando; F7b-06 la ruta y la pantalla), con `channels.manage`, MFA reciente (D-F7-11), límite de tasa (F7-02e) y auditoría `channel.connected` con ids y sin valores. En un solo paso: (1) valida formatos; (2) con el token, `GET /<PAGE_ID>` comprueba que la página existe y que el token la gestiona, y en Instagram que el `instagram_business_account` de la página **es** el id dado; (3) suscribe la app a la página (`POST /<PAGE_ID>/subscribed_apps` con la lista mínima de campos, SIN VERIFICAR 7 para Instagram); (4) guarda la cuenta `PENDING` con la credencial cifrada. Lo que el Owner escribe es dato no confiable hasta que la Graph API lo confirma.
- **Token:** se descifra solo durante la llamada y nunca vuelve al navegador ni a una respuesta, registro, auditoría, outbox o métrica (ADR-029 §2). Si el token caduca (`expires_at`, leído al conectar cuando la API lo dé), el chequeo de salud avisa 14 y 3 días antes; **no hay renovación automática** en el primer corte (SIN VERIFICAR 10): rotar es pegar uno nuevo, como WhatsApp. Un `190` pone la cuenta en `ERROR` con `TOKEN_INVALID`.
- **Detalles de estado nuevos** (conjunto cerrado de `STATUS_DETAILS`): `MESSAGES_ACCESS_OFF` (Instagram, `200`/`2534041`: el titular apagó «Allow access to messages») y `PAGE_MESSAGING_OFF` (Messenger, la página tiene los mensajes desactivados); el texto de Meta nunca se guarda.

### 4. Webhook y enrutado del tenant

- **La misma ruta** `POST /webhooks/meta/`, el mismo tope, la misma firma sobre el cuerpo **crudo** (nunca se re-serializa: Meta firma su propio escape de Unicode [S17]) y el mismo orden: firma → parsear → esquema. `parse` despacha por `object` a un analizador por objeto (`whatsapp_business_account`, `page`, `instagram`); un `object` desconocido sigue siendo `Invalid("object")`, contado y con `200`. El `GET` de suscripción no cambia (el mismo verify token para los tres objetos).
- **Tope de cuerpo:** Meta agrupa hasta 1000 actualizaciones [S5]; el tope sigue en 1 MiB y lo que no quepa se cuenta (`413`) y Meta lo reintenta 36 h. En estos canales Meta solo reintenta 36 h [S5] (no los 7 días de WhatsApp), así que un `413` repetido pierde el evento: el tope de 3 MiB y los lotes grandes sin pérdida silenciosa de F7-11b ([#789](https://github.com/PapiCuche/crm-gooddoggy/issues/789)) aplican también a `page` e `instagram`, y F7b-04 mide el tamaño real en el simulador; nunca sin tope.
- **Un ítem por evento lógico** de `entry[].messaging[]`, con clave de deduplicación por plataforma: `fb:msg:<mid>`, `ig:msg:<mid>`, `fb:read:<page>:<psid>:<watermark>`, `fb:delivery:<page>:<psid>:<watermark>`, `ig:read:<mid>`; cada id con `urllib.parse.quote(id, safe="")`, como hoy. Ningún reintento de Meta (36 h) duplica: la ingesta deduplica por clave y el mensaje por `external_message_id` único por cuenta.
- **Ingesta:** F7b-04 crea **nuevas versiones** de `webhook_ingress_insert` y del resolutor en una migración nueva (nunca se editan `0008` ni `0009`) para que la fila lleve la plataforma y el id de enrutado sin forzarlos al hueco del WABA; mismas garantías de ADR-002 §3.3 (propietario `crm_migrator`, `search_path = pg_catalog, public, pg_temp`, argumentos tipados, `REVOKE ALL … FROM PUBLIC`, `GRANT EXECUTE … TO crm_app`, en `tests/test_pg_temp_shadowing.py`). La tabla sigue sin políticas para `crm_app` y el payload se vacía al cerrar.
- **Enrutado:** una función `SECURITY DEFINER` mínima, `resolve_social_account(p_platform text, p_external_id text) → (organization_id, channel_account_id)`, que solo responde para `MESSENGER` e `INSTAGRAM`, solo con cuentas vivas y sin SQL dinámico. La plataforma sale del `object` del cuerpo **firmado**; el `organization_id` sale de la función, **nunca** del payload (prueba T11).
- **Cruces obligatorios**, cada uno con su prueba «evento firmado para una cuenta de otra organización»:
  - Messenger: `entry.id` = `external_account_id`; en un entrante `recipient.id` = `entry.id`; en un eco `sender.id` = `entry.id`.
  - Instagram: lo mismo con el id de la cuenta profesional.
  - Si algo no cuadra, el ítem queda `IGNORED` con `ACCOUNT_MISMATCH`, alerta y sin escribir nada.
- **`standby`** (la app no controla el hilo) se registra como entrante (el cliente escribió de verdad), pero **nunca dispara una respuesta automática** [S22]; la pantalla avisa de que otra app controla la conversación. `messaging_handovers` se cuenta.
- **URL de historias fuera de la ingesta (F7b-04, con prueba):** el analizador de `instagram` **quita** `attachments[].payload.url` de un `story_mention` y `reply_to.story.url` **antes** de `ingress.store`, y deja solo el tipo y el id. No basta con vaciar el payload al cerrar: un ítem `FAILED` o `DEAD` conserva su payload hasta la purga de 90 días (`0009` no lo vacía en `DEAD`), y esa URL es media que Meta prohíbe guardar [S13]. La prueba firma un evento con las dos URL y comprueba que la fila guardada no las contiene, también tras pasar a `FAILED` y a `DEAD`.
- **Un ítem que no se entiende** (postback, referencia, reacción, edición, campo nuevo) queda `IGNORED` con métrica en el primer corte, salvo los tipos de mensaje de §5.

### 5. Mensajes entrantes

| Evento de Meta                                                                                           | Primer corte                                                                                                                                                                                                             |
| -------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `message.text`                                                                                           | `TEXT`, saneado como en ADR-029 §5 (sustitutos, controles, formato invisible, Tags) y acotado                                                                                                                            |
| `attachments` (imagen, audio, video, archivo, sticker, `share`, `ig_post`, `ig_reel`, `fallback`…)       | `UNSUPPORTED` con una etiqueta fija del código; medios, después (F7-09 los resolvió para WhatsApp con antivirus y almacén)                                                                                               |
| `reply_to.story` (Instagram)                                                                             | El texto como `TEXT` y una etiqueta fija «Respondió a tu historia»; **el analizador quita la URL de la historia antes de `ingress.store`** (§4), así que no llega ni a la ingesta                                        |
| `story_mention` (Instagram)                                                                              | `UNSUPPORTED` con «Te mencionó en una historia»; **el analizador quita la URL antes de `ingress.store`** (§4): la media nunca se guarda ni se cachea (lo prohíbe Meta [S13]) y la URL no llega a la base ni al navegador |
| `is_unsupported`, `is_deleted`                                                                           | `UNSUPPORTED` genérico; un borrado no borra lo ya registrado (se cuenta)                                                                                                                                                 |
| `is_echo` **nuestro** (§6: `app_id` de nuestra app y `metadata` con el id de nuestro mensaje)            | Reconcilia el mensaje por la correlación y guarda su `external_message_id`, sin duplicar                                                                                                                                 |
| `is_echo` de otra app o de la bandeja nativa (`app_id` distinto, o sin correlación tras la espera de §6) | `IGNORED` con métrica (`FOREIGN_ECHO`) hasta que exista el origen `BUSINESS_APP` de ADR-032 §4; D-F7b-07                                                                                                                 |
| `read`, `message_reads`, `message_deliveries`                                                            | `advance_delivery` (F7b-05): Messenger usa una marca de agua (todo lo enviado hasta ese instante en esa conversación); Instagram, el `mid`                                                                               |

**Ventana y orden.** `provider_timestamp` sale del `timestamp` del evento, acotado para no estar en el futuro, y ancla `last_inbound_at` como en WhatsApp. Los eventos pueden llegar desordenados [S17]: el chat se ordena por llegada (03 §I.7) y la ventana usa el instante del proveedor. Un evento con más de 24 h no reabre una conversación cerrada.

**Sin teléfono, sin fusión automática** (la identidad):

- F7b-04 crea **`contact_identities`** (diseño de `02` §E.5, que hoy no existe): tabla del tenant con FORCE RLS, política de tenant, FKs compuestas a `contacts` y `channel_accounts`, concesiones por columna como sus hermanas y su entrada en `TENANT_TABLES`. Columnas mínimas: `contact_id`, `channel_account_id`, `platform`, `external_id` (el PSID o el IGSID), `created_at`.
- **Único por (`organization_id`, `channel_account_id`, `external_id`)**: el PSID es por página y el IGSID por app y cuenta [S1] [S27], así que el alcance es la cuenta de canal, nunca global ni por plataforma. Al contrario que el teléfono, aquí sí hay índice único: un id con alcance de cuenta es una identidad, no un dato compartible. Dos primeros mensajes simultáneos se resuelven con `INSERT … ON CONFLICT DO NOTHING` y relectura (04 §Q, «dos mensajes simultáneos»).
- Un id nuevo crea **un contacto nuevo** sin teléfono, con `source = INBOUND_MESSAGE` y un `display_name` fijo del código («Cliente de Instagram», «Cliente de Messenger») hasta que haya nombre (D-F7b-06). El tope de contactos nuevos por minuto y cuenta (F7-04b) se aplica igual.
- **Nunca se fusiona** con un contacto por teléfono, por correo, por nombre ni por el `@usuario`: el nombre y el usuario los escribe el cliente (dato no confiable, saneado, acotado y mostrado aislado con `<bdi>`). Vincular una identidad a otro contacto es una **decisión humana**, auditada, con su propio work item después de R3 (D-F7b-05); este ADR no crea sugerencias automáticas.
- **Conversación:** `conversations` gana `contact_identity_id` (FK compuesta, previsto en `02` §E.6), obligatorio para `MESSENGER` e `INSTAGRAM` por CHECK; el adaptador lee de ahí a quién responder. La unicidad «a lo más una no cerrada por contacto y cuenta» no cambia.
- **Consentimiento y baja:** `contact_consents` ya admite el canal (`INSTAGRAM`, `MESSENGER`, ADR-010 §1); las palabras de baja de la organización (F7-08b) valen igual en estos canales.

### 6. Mensajes salientes

- `MessengerAdapter` e `InstagramAdapter` implementan `ChannelAdapter` (F7b-05) por `core.http` (allowlist con `graph.facebook.com`, IP pública fijada, sin redirecciones, tiempos acotados). `POST /<PAGE_ID>/messages` [S20] con `recipient.id` = el PSID o el IGSID, `messaging_type` y `message.text`; para Instagram con Facebook Login el endpoint exacto se confirma en F7b-05 contra [S14].
- `Claim` se generaliza sin romper WhatsApp: `recipient` pasa a ser «el destinatario en el canal» (el teléfono en WhatsApp, el id de la identidad en estos canales) y `phone_number_id` se lee como `external_account_id`, con el mismo `repr` sin datos personales.
- **Envío con reclamo** (F7-05): estado `SENDING`, llamada fuera de la transacción, sin reintento a ciegas tras un resultado ambiguo (Meta no deduplica).
- **Cómo se reconoce nuestro propio eco** (F7b-05). Tras un tiempo agotado nunca recibimos el `mid`, y en un envío normal el eco puede llegar antes de que se guarde `external_message_id`: buscar por `mid` no basta. Por eso:
  - Cada envío lleva `message.metadata` con el UUID de nuestro mensaje (y nada más: sin texto ni datos del cliente). Messenger documenta `metadata` y `app_id` en el eco [S19]; que el valor enviado vuelva **igual** en el eco, y que Instagram lo haga también, es **SIN VERIFICAR 11**.
  - Un eco es nuestro solo si `app_id` es el de nuestra app (`META_APP_ID`) **y** `metadata` es el UUID de un mensaje de **esa** cuenta y **esa** conversación en `SENDING` o enviado; entonces guarda el `mid` y avanza a `SENT` (reconcilia un ambiguo). Si el eco de Instagram no trae `app_id` (SIN VERIFICAR 12), basta la correlación de `metadata` con esas mismas comprobaciones.
  - **Espera breve:** un eco con nuestro `app_id` cuya correlación todavía no encuentra el mensaje queda `FAILED` transitorio y se reintenta con la espera de la ingesta; tras `UNKNOWN_AFTER` intentos (3, como los estados de WhatsApp, ADR-029 §8) se cierra `IGNORED` con `UNKNOWN_MESSAGE` y su payload vaciado.
  - Si las pruebas con una app real (F7b-05 contra el simulador; el runbook de activación contra Meta) muestran que la correlación no vuelve, **no hay reconciliación por eco**: un ambiguo queda `FAILED` con `PROVIDER_UNKNOWN` tras su plazo, visible, y la persona decide si reenviar (el mismo camino de ADR-029 §6).
- **Errores** a códigos propios (nunca el texto de Meta): `10`/`2534022` y `10`/`2018278` → `WINDOW_CLOSED`; `551` → `RECIPIENT_UNREACHABLE`; `190` → `TOKEN_INVALID` (cuenta en `ERROR`); `613` → `RATE_LIMITED` (pasajero); `200`/`2534041` → la cuenta pasa a `ERROR` con `MESSAGES_ACCESS_OFF`; `100` y el resto → `PROVIDER_REJECTED`; 5xx o tiempo agotado → `PROVIDER_ERROR` (ambiguo si se envió). Los números se reconfirman al implementar [S26].
- **Límite de tasa:** el limitador por cuenta (Redis) muy por debajo del tope más estricto: 100 por segundo en Instagram [S1], 300 por segundo y 200 × personas activas al día en Messenger [S25].
- **Versión de la Graph API:** la misma `META_GRAPH_VERSION` del despliegue (D-F7-08); estos canales no fijan otra. Si su contrato exige subirla, se revisan los fixtures de los tres canales en el mismo cambio.

### 7. Política (ADR-010)

| Capacidad                  | `WHATSAPP` (diseño de ADR-010 §1; el código solo tiene `window_hours`) | `MESSENGER`                                                                                                                                                        | `INSTAGRAM`                              |
| -------------------------- | ---------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ---------------------------------------- |
| `window_hours`             | 24                                                                     | 24 [S21]                                                                                                                                                           | 24 [S1]                                  |
| `supports_templates`       | Sí (F7-10)                                                             | No en este ADR (las plantillas de utilidad, después)                                                                                                               | No (no hay One-Time Notifications [S20]) |
| `supports_human_agent_tag` | No (ADR-010 §3)                                                        | **Solo si** Meta aprobó la función Human Agent: el propietario enciende `META_HUMAN_AGENT_APPROVED` (apagado por omisión) **después** de esa aprobación (D-F7b-08) | Igual                                    |
| `human_agent_window_hours` | —                                                                      | 168 [S20]                                                                                                                                                          | 168 [S1]                                 |
| `max_text_length`          | 4096                                                                   | 2000 caracteres (SIN VERIFICAR 6)                                                                                                                                  | 1000 bytes UTF-8 [S8]                    |

- `BLOCKED(WINDOW_CLOSED)` sigue lo que hace hoy `policy.py` (sin plantillas, ventana cerrada = bloqueado) y **no cambia** la regla 6 de ADR-010. La regla 6 se aplica tal cual: con la ventana cerrada, `initiated_by = USER` y la etiqueta disponible y dentro de su plazo → `HUMAN_AGENT_TAG` (`messaging_type = MESSAGE_TAG`, `tag = HUMAN_AGENT`); la **IA y las automatizaciones nunca** la usan; en otro caso `BLOCKED(WINDOW_CLOSED)` (no hay plantillas que ofrecer).
- El ancho de 7 días por anuncios (Click-to-Messenger, Click-to-Direct) **no se usa** para ampliar la ventana: el CRM no sabe con certeza qué abrió la ventana; queda en 24 h (lo conservador).
- Las etiquetas `CONFIRMED_EVENT_UPDATE`, `ACCOUNT_UPDATE` y `POST_PURCHASE_UPDATE` **no se usan** (error `100` desde el 27-04-2026 [S20]).
- La decisión se guarda en `policy_decision` y se re-evalúa justo antes de enviar, como ya hace el despachador.

### 8. Seguridad y datos personales

- Firma antes de parsear; tenant solo por la función de enrutado; cruces obligatorios (§4); esquema estricto con listas, textos y profundidad acotados; texto saneado; el nombre y el `@usuario`, no confiables.
- El PSID y el IGSID **son datos personales**: viven en el payload de la ingesta (vaciado al cerrar) y en `contact_identities`; nunca en registros, métricas, auditoría ni outbox (los registros llevan ids internos, tipos, estados y códigos).
- Las URL de media de Meta nunca se entregan al navegador; las de una mención o una respuesta a una historia se quitan en el analizador, antes de la ingesta (§4), así que no se guardan en ninguna tabla, tampoco en un ítem `FAILED` o `DEAD`.
- El token de la página da acceso a los mensajes **y** a la gestión de la página según los permisos concedidos: se piden **solo** los permisos de §13 y se documenta en el runbook que el usuario del sistema tenga asignados solo esa página y esa cuenta.
- **Repositorio público:** ningún id de página, de cuenta, de app, nombre ni dato de la tienda en código, pruebas, issues ni documentos; se dice «la página de la tienda» y «la cuenta de Instagram de la tienda».

### 9. Simulador (F7b-03)

Amplía `backend/tests/meta_sim` con los objetos `page` e `instagram`, firmados con el secreto de pruebas:

- **Webhooks:** texto; cada adjunto; respuesta y mención de historia; eco propio y eco de otra app; `read`/`message_reads`; `message_deliveries`; `standby`; `is_unsupported`; `is_deleted`; reacción; lotes con varios eventos y desordenados; un `entry.id` que no coincide con `recipient.id`; un `object` desconocido.
- **Graph API falsa:** `GET /<PAGE_ID>` (con y sin `instagram_business_account`), `POST /<PAGE_ID>/subscribed_apps`, `POST /<PAGE_ID>/messages` con éxito (`recipient_id`, `message_id`) y con cada error de §6 (`10`/`2534022`, `10`/`2018278`, `551`, `190`, `613`, `200`/`2534041`, `100`), lentitud y corte.
- Fixtures marcadas con la versión que se confirme al implementar (hoy la última es `v27.0` [S34]; el despliegue usa `v23.0`). Ningún id real.

### 10. Pantalla de canales (F7b-06)

En `/o/{slug}/canales`, junto a WhatsApp: conectar, rotar el token y desconectar Messenger e Instagram con las mismas garantías (F7-02d, F7-07b/c): `channels.manage`, MFA reciente, el token nunca en el navegador ni en respuestas, solo `last_four`, el estado y la lista de lo que falta (página sin suscribir, webhook no visto, «Allow access to messages» apagado, la app no es la predeterminada del enrutado de conversaciones). Prueba E2E en el navegador contra el stack local con el simulador.

### 11. Fuera de este ADR

- Medios entrantes y salientes de estos canales; reacciones; ediciones; respuestas privadas a comentarios de publicaciones; plantillas de utilidad y mensajes de marketing de Messenger; anuncios Click-to-Messenger o Click-to-Direct.
- Unified Login, el alta por flujo de Meta en el navegador y el modelo de proveedor de tecnología para terceros (ADR-032 y D-F7-01).
- Vincular identidades entre contactos (D-F7b-05) y la identidad por BSUID de WhatsApp (ADR-032 §12, F7-14).
- La devolución de llamada de borrado de datos de Meta (D-F7b-08).
- TikTok (F7b-07).

### 12. Plan de la serie

F7b-01 (este ADR) → F7b-02 (aceptación y D-F7-09) → F7b-03 (simulador) → F7b-04 (cuentas, webhook, enrutado, `contact_identities`; si pasa de 800 líneas se parte por canal antes de empezar) → F7b-05 (adaptadores, política y estados) → F7b-06 (pantalla; cierra R3). F7b-07 va en paralelo y no depende de nada. El detalle vive en [phase-7.md](../phases/phase-7.md), «Fase 7b».

### 13. Decisiones que necesitan al propietario

| ID       | Pregunta                                                                                                                     | Recomendación                                                                                                                                                                                    | Bloquea        |
| -------- | ---------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | -------------- |
| D-F7-09  | Orden y calendario de las demás redes frente al roadmap (Fase 12)                                                            | Instagram y Messenger juntos en R3, después de WhatsApp en vivo: comparten la página, la app, la revisión y casi todo el código. Se activa primero Instagram (el orden que pidió el propietario) | F7b-03         |
| D-F7b-02 | Variante de Instagram: Facebook Login (vinculada a la página) o Unified Login                                                | Facebook Login en el primer corte (§2); Unified Login como migración cuando exista el alta por flujo de Meta                                                                                     | F7b-04         |
| D-F7b-03 | ¿La misma app de Meta que WhatsApp o una distinta?                                                                           | La misma (D-F7-01): una firma, una revisión, un runbook                                                                                                                                          | F7b-04         |
| D-F7b-04 | Token del usuario del sistema: ¿«nunca vence» o «60 días»?                                                                   | 60 días si el propietario acepta rotarlo a mano con el aviso de la pantalla (lo que recomienda Meta [S35]); si no, «nunca vence» con rotación del runbook cada 90 días                           | F7b-04         |
| D-F7b-05 | ¿Un cliente que escribe por Instagram o Messenger y ya es contacto por teléfono?                                             | Contacto nuevo sin fusión; vincular será una acción humana auditada en un work item posterior                                                                                                    | F7b-04         |
| D-F7b-06 | ¿Nombre del cliente? (Instagram lo da tras el primer mensaje; Messenger exige la función Business Asset User Profile Access) | Instagram: leer `name` y `username` en una tarea tras crear el contacto (no confiables). Messenger: nombre fijo hasta que Meta apruebe esa función                                               | F7b-04         |
| D-F7b-07 | ¿Se responde también desde la app de Instagram o desde Meta Business Suite?                                                  | No: el equipo responde desde Good Doggy; la app del CRM es la predeterminada del enrutado de conversaciones; los ecos ajenos se cuentan y se muestran cuando exista ADR-032 §4                   | F7b-05         |
| D-F7b-08 | Pedir la función **Human Agent** (7 días, solo personas) en la revisión                                                      | Sí, junto con el acceso avanzado; hasta que se apruebe, la ventana es de 24 h                                                                                                                    | F7b-05 en vivo |
| D-F7b-09 | Política de privacidad y borrado de datos                                                                                    | Una URL pública con la política que explique el borrado; la devolución de llamada de borrado, en un work item posterior                                                                          | Activación     |

**Lo que el propietario tiene que hacer en Meta** (sin ningún valor en el repositorio; los valores solo por el gestor de secretos del despliegue):

1. Una **página de Facebook** de la tienda en su cartera de negocio, con «Mensajes» activado.
2. Una **cuenta profesional de Instagram** (empresa) **vinculada a esa página**, con «Allow access to messages» activado en Herramientas conectadas [S11].
3. La **app de Meta** del despliegue (la de WhatsApp) con los casos de uso **Messenger** e **Instagram**, en modo **Live** [S5].
4. **Verificación del negocio** y **App Review** para el **acceso avanzado** a `pages_messaging`, `pages_manage_metadata`, `pages_show_list`, `pages_read_engagement`, `business_management`, `instagram_basic` e `instagram_manage_messages`, y para la función **Human Agent** (D-F7b-08) [S1] [S16] [S31] [S32]. La misma verificación que pide la coexistencia (D-COEX-01): empezarla ya.
5. Una **URL de política de privacidad** en la app, que explique cómo pedir el borrado [S10] [S33].
6. Un **usuario del sistema** con la página y la cuenta de Instagram asignadas, y su token con esos permisos (D-F7b-04) [S35].
7. Los **webhooks** de los objetos `page` e `instagram` en la app, apuntando a `/webhooks/meta/` con el mismo verify token (D10-H para la URL pública HTTPS).
8. En la página, la app del CRM como **app predeterminada** del enrutado de conversaciones [S22] (D-F7b-07).

## Alternatives considered

| Alternativa                                                          | Por qué se descarta                                                                                                                                       |
| -------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Instagram Login                                                      | Cerrado para apps nuevas [S2]; credenciales propias y firma SIN VERIFICAR                                                                                 |
| Unified Login desde el primer corte                                  | Recomendado por Meta, pero exige el alta por flujo en el navegador (CSP, SDK, como ADR-032 §2, aún `Proposed`) y no admite Handover; queda como migración |
| Una ruta de webhook por objeto (`/webhooks/meta/page/`…)             | Duplica la firma, el tope y la ingesta; el `object` ya llega firmado y basta para despachar                                                               |
| Guardar la identidad en `contacts` (columnas `instagram_id`, `psid`) | Las identidades son por cuenta y varias por contacto; `02` §E.5 ya diseñó `contact_identities`                                                            |
| Fusionar por nombre, `@usuario` o teléfono                           | El nombre lo escribe el cliente; una fusión equivocada mezcla datos de dos personas; la decisión es humana                                                |
| Índice único global del PSID o IGSID                                 | Son ids con alcance de página o de cuenta [S1] [S27]; uno global mezclaría organizaciones                                                                 |
| Guardar la media de las menciones en historias                       | Meta lo prohíbe [S13]                                                                                                                                     |
| Usar las etiquetas de evento, cuenta o compra                        | Dan error `100` desde el 27-04-2026 [S20]                                                                                                                 |
| Herramientas o bibliotecas no oficiales                              | Prohibidas (ADR-029 §1)                                                                                                                                   |

## Consequences

- Dos canales con casi todo el código de WhatsApp: lo nuevo es un analizador por objeto, una función de enrutado, dos adaptadores, `contact_identities`, `conversations.contact_identity_id` y las capacidades.
- `channel_accounts` gana dos plataformas con sus CHECK; `STATUS_DETAILS`, dos valores; `Claim` se generaliza sin cambiar a WhatsApp.
- Habrá clientes **sin teléfono**: la ficha, la búsqueda y la lista tienen que funcionar sin él (ya lo hacen: el teléfono es opcional).
- Un mismo cliente puede aparecer como dos contactos (Instagram y WhatsApp) hasta que una persona los vincule.
- La activación real depende de trámites de Meta (verificación y revisión) con plazos que el programa no controla.

## Security implications

- Mismas defensas que WhatsApp (ADR-029 §10): firma antes de parsear, tope, esquema, idempotencia, tenant por función mínima, cruces obligatorios, token cifrado que solo se abre al enviar.
- Nuevos identificadores personales (PSID, IGSID, `@usuario`) con las mismas reglas que el teléfono: fuera de registros, métricas y auditoría.
- El modelo de app única amplía lo que firma un secreto filtrado a los mensajes de Instagram y Messenger: el cruce `entry.id` con la cuenta resuelta es obligatorio y probado por canal.
- La IA nunca usa la etiqueta de agente humano, y ningún envío sale fuera de la ventana sin ella.

## Operational implications

- Sin variables nuevas con secreto; una sin secreto: `META_HUMAN_AGENT_APPROVED` (apagada por omisión), que el propietario enciende solo después de que Meta apruebe la función Human Agent (D-F7b-08). `META_APP_ID` (sin secreto, ya previsto en ADR-032) para reconocer los ecos propios (§6). La allowlist ya tiene `graph.facebook.com`.
- El webhook recibe tres objetos: las métricas existentes se etiquetan por plataforma (firmas inválidas, ítems por estado, envíos por código).
- Meta desactiva el webhook de Messenger tras una hora de fallos [S17]: la alerta de ítems `FAILED` y de firmas inválidas (F7-11a) cubre también estos canales.
- Runbook de activación (después de F7b-06): página, cuenta, app, revisión, usuario del sistema, suscripción, enrutado de conversaciones y prueba con una cuenta de pruebas antes de la de la tienda.

## Fuentes (todas leídas el 2026-11-13)

Instagram:

- [S1] Instagram Platform overview: https://developers.facebook.com/documentation/instagram-platform/overview
- [S2] Instagram Login, migration guide to Unified Login: https://developers.facebook.com/documentation/instagram-platform/instagram-api-with-instagram-login/migration-guide
- [S3] Instagram API with Unified Login: https://developers.facebook.com/documentation/instagram-platform/instagram-api-with-unified-login
- [S4] Unified Login, authentication: https://developers.facebook.com/documentation/instagram-platform/instagram-api-with-unified-login/authentication
- [S5] Webhooks setup: https://developers.facebook.com/documentation/instagram-platform/webhooks/setup
- [S6] Webhooks fields: https://developers.facebook.com/documentation/instagram-platform/webhooks/fields
- [S7] Webhooks examples: https://developers.facebook.com/documentation/instagram-platform/webhooks/examples
- [S8] Messaging API (Instagram Login): https://developers.facebook.com/documentation/instagram-platform/instagram-api-with-instagram-login/messaging-api
- [S9] User Profile API (Instagram): https://developers.facebook.com/documentation/instagram-platform/instagram-api-with-instagram-login/messaging-api/user-profile
- [S10] App Review (Instagram): https://developers.facebook.com/documentation/instagram-platform/app-review
- [S11] Instagram Messaging, get started: https://developers.facebook.com/documentation/business-messaging/instagram-messaging/get-started
- [S12] Instagram Messaging, overview and webhooks: https://developers.facebook.com/documentation/business-messaging/instagram-messaging/overview y https://developers.facebook.com/documentation/business-messaging/instagram-messaging/webhooks
- [S13] Story mentions: https://developers.facebook.com/documentation/business-messaging/instagram-messaging/features/story-mention
- [S14] Send a message (Instagram Messaging): https://developers.facebook.com/documentation/business-messaging/instagram-messaging/features/send-message
- [S15] Business Login for Instagram (tokens y ámbitos retirados): https://developers.facebook.com/documentation/instagram-platform/instagram-api-with-instagram-login/business-login

Messenger:

- [S16] Messenger Platform overview: https://developers.facebook.com/documentation/business-messaging/messenger-platform/overview
- [S17] Webhooks: https://developers.facebook.com/documentation/business-messaging/messenger-platform/webhooks
- [S18] Webhook `messages`: https://developers.facebook.com/documentation/business-messaging/messenger-platform/webhooks/webhook-events/messages
- [S19] Webhook `message_echoes`: https://developers.facebook.com/documentation/business-messaging/messenger-platform/webhooks/webhook-events/message-echoes
- [S20] Send messages: https://developers.facebook.com/documentation/business-messaging/messenger-platform/send-messages
- [S21] Policy: https://developers.facebook.com/documentation/business-messaging/messenger-platform/policy
- [S22] Conversation routing: https://developers.facebook.com/documentation/business-messaging/messenger-platform/conversation-routing
- [S23] Webhook `standby`: https://developers.facebook.com/documentation/business-messaging/messenger-platform/webhooks/webhook-events/standby
- [S24] User Profile API (Messenger): https://developers.facebook.com/documentation/business-messaging/messenger-platform/identity/user-profile
- [S25] Rate limiting: https://developers.facebook.com/documentation/business-messaging/messenger-platform/overview/rate-limiting
- [S26] Error codes: https://developers.facebook.com/documentation/business-messaging/messenger-platform/error-codes
- [S27] Quick start: https://developers.facebook.com/documentation/business-messaging/messenger-platform/getting-started/quick-start
- [S28] Create an app (Messenger): https://developers.facebook.com/documentation/business-messaging/messenger-platform/create-an-app
- [S29] FAQ: https://developers.facebook.com/documentation/business-messaging/messenger-platform/faq
- [S30] Saving assets (attachment upload): https://developers.facebook.com/documentation/business-messaging/messenger-platform/send-messages/saving-assets

Comunes:

- [S31] Human Agent feature: https://developers.facebook.com/documentation/development/features-reference/human-agent
- [S32] Access levels: https://developers.facebook.com/docs/graph-api/overview/access-levels
- [S33] Data deletion callback: https://developers.facebook.com/documentation/development/create-an-app/app-dashboard/data-deletion-callback
- [S34] Graph API changelog: https://developers.facebook.com/docs/graph-api/changelog
- [S35] System users, install apps and generate tokens: https://developers.facebook.com/docs/business-management-apis/system-users/install-apps-and-generate-tokens
- [S36] Long-lived access tokens: https://developers.facebook.com/docs/facebook-login/guides/access-tokens/get-long-lived
- [S37] Business Asset User Profile Access: https://developers.facebook.com/docs/features-reference/business-asset-user-profile-access

No se pudo leer la referencia de la Send API de Messenger ni la página de etiquetas (`/docs/messenger-platform/send-messages/message-tags` y `…/reference/send-api` solo devuelven la navegación): por eso el tope de 2000 caracteres y la lista completa de etiquetas de Instagram quedan SIN VERIFICAR.
