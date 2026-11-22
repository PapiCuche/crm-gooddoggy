# ADR-039: Canal de TikTok con la Business Messaging API oficial: acceso directo con la DSPR y un proveedor de la lista de TikTok como alternativa

- **Status:** Proposed
- **Date:** 2026-11-16
- **Deciders:** propuesto por F7b-08 para la decisión del propietario. Mientras esté `Proposed` no obliga al código, no empieza ningún work item de código del canal y no se envía nada a TikTok. Las preguntas para el propietario están en §13.
- **Work items:** F7b-08 [#863](https://github.com/PapiCuche/crm-gooddoggy/issues/863) (investigación, borrador de la solicitud y este ADR). La serie de código de §12 se crea solo si el propietario acepta.
- **Related:** ADR-001, ADR-002 §3.3, ADR-003, ADR-008, ADR-010, ADR-011, ADR-013, ADR-015, ADR-022, ADR-023, ADR-029 (patrón del canal), ADR-033, ADR-034 (Accepted el 2026-11-16; IA; este ADR lo acota en §8 si se acepta), ADR-035 (Accepted el 2026-11-16; Instagram y Messenger, el patrón más parecido), ADR-040 (clave de la ingesta por cuenta), [tiktok-business-messaging.md](../research/tiktok-business-messaging.md), [tiktok-access-request-draft.md](../research/tiktok-access-request-draft.md), [phase-7.md](../phases/phase-7.md)

## Context

El propietario necesita atender en el Inbox los mensajes directos de la cuenta de TikTok de la tienda y pidió adelantar la gestión del acceso oficial (Inbox omnicanal P0, 2026-11-16). F7b-07 concluyó «esperar» porque no se sabía si TikTok aprueba un CRM propio de una sola cuenta. F7b-08 releyó las páginas oficiales completas el 2026-11-16 ([tiktok-business-messaging.md](../research/tiktok-business-messaging.md), fuentes [T…] y [P…] de su §13), y la revisión previa al merge las volvió a leer el 2026-11-22 sin cambios en las reglas oficiales. Lo que importa para decidir:

- **Hay una API oficial, en beta abierta en LATAM**: la Business Messaging API de TikTok API for Business [T3] [T13].
- **La cuenta** tiene que tener Advanced Access (vinculada a TikTok for Business o a un Business Center) o ser una Verified Business Account [T2] [T10].
- **Perú** no se nombra, pero no está excluido; las cuentas fuera de EE. UU., el EEE, Suiza y el Reino Unido («Rest of World») solo exigen que el desarrollador pase la **Data Security & Privacy Review (DSPR)** [T2]. Que Perú sea «Rest of World» es un **SUPUESTO** fuerte que TikTok tiene que confirmar.
- **El desarrollador** tiene que ser una empresa (no se admiten personas), con un correo y un sitio en un dominio propio; puede registrarse como «Direct Advertiser» y declarar la app como de **uso interno** [T19] [T20].
- **La DSPR** no tiene excepciones, suele tardar 2-4 semanas tras un inicio de hasta 10 días hábiles, es manual y pide políticas firmadas, MFA de administradores, cifrado en reposo y en tránsito, gestión de vulnerabilidades, respuesta a incidentes, aviso de privacidad y **borrado de los datos cuando se retira la autorización** [T2] [T14]. Que TikTok apruebe a un anunciante con una sola cuenta: **SIN VERIFICAR**.
- **Límites oficiales:** tras el primer mensaje del usuario, hasta 10 mensajes en 48 h; cada respuesta suya abre 48 h ilimitadas; pasadas 48 h sin respuesta, solo tres mensajes más [T18]. El cliente siempre empieza.
- **Proveedores:** de los nueve de la lista oficial de socios de mensajería de TikTok [T8] (una lista de gestión de mensajes, anuncios y medición que no nombra la Business Messaging API), solo Infobip, respond.io y Qiscus documentan mensajes directos de TikTok (Pancake: **SIN VERIFICAR**, su guía habla de medir anuncios); Infobip es el único con webhook de entrada, API de salida y DPA estándar publicados [P3] [P6] [P7].

### Lo que ya existe y este ADR reutiliza

`ChannelAdapter`, `SandboxAdapter` y `adapter_for`; `channel_accounts` y `credentials` con `core.sealed` (ADR-022); la ingesta `webhook_ingress` por funciones `SECURITY DEFINER` con su purga a 90 días; `MessagingPolicyService` con `ChannelCapabilities`; el análisis de medios con ClamAV (F7-15); la pantalla de canales con `channels.manage` y MFA reciente; el simulador de Meta como modelo. Desde F7b-04a (ADR-035, ya `Accepted`) existen en el código `contact_identities` y la función de enrutado `resolve_social_account` (migración `0040`), y la clave de la ingesta es por cuenta (ADR-040).

## Decision

### 1. Alcance y principios

1. **Solo vías oficiales:** la Business Messaging API de TikTok, directa o a través de un proveedor de la lista oficial de TikTok [T8]. Nada de scraping, sesiones personales, automatización de la app ni bibliotecas no oficiales. Si TikTok no permite algo que el canal necesita, el canal se aplaza.
2. **Primero, la vía directa** (§2). El proveedor es la alternativa (§3) y nunca coexiste con la directa sobre la misma cuenta de TikTok.
3. **Una plataforma nueva en el patrón de siempre:** `TIKTOK`, una `channel_account` por cuenta de TikTok, con su adaptador, su capacidad en la política, su enrutado por una función `SECURITY DEFINER` y su simulador. Conversaciones, mensajes, asignación, notas, auditoría y permisos (`conversations.view`, `messages.send`, `channels.manage`) **no cambian**.
4. **Primer corte: texto** entrante y saliente, y el marcador «no soportado» para el resto (lo que no se entiende se ve, no se pierde). Las imágenes van después, con ClamAV.
5. **Todo se prueba contra un simulador.** Ningún test, CI ni desarrollo llama a TikTok. Conectar la cuenta real es un work item operativo posterior a la aprobación de la DSPR.
6. **Regiones:** solo cuentas «Rest of World». La conexión de una cuenta de EE. UU., el EEE, Suiza o el Reino Unido falla con un error claro; no se pide la revisión de EE. UU.

### 2. Vía directa: la app de TikTok del despliegue

- La empresa se registra como **Direct Advertiser** y crea **una app de uso interno** con los alcances mínimos de la mensajería [T19] [T20]. El borrador de los formularios está en [tiktok-access-request-draft.md](../research/tiktok-access-request-draft.md).
- El **app id** y el **app secret** van al entorno del despliegue, como el de Meta (ADR-033 §6): `TIKTOK_ENABLED`, `TIKTOK_APP_ID`, `TIKTOK_APP_SECRETS` (lista, para rotar sin cortar) y `TIKTOK_API_BASE`. Nunca en la base ni en el frontend.
- El **token de la cuenta** (y su token de renovación, si lo hay) se guarda cifrado en `credentials` con `core.sealed`, propósito `channel.access_token`, dueño `channel_account:<id>`, `last_four` y `expires_at`. La duración y la renovación del token: **SIN VERIFICAR** (pregunta 6 a TikTok); el diseño de la renovación se cierra al leer la documentación que se abre con la aprobación.
- La autorización de la cuenta sigue el flujo OAuth de TikTok API for Business hacia `https://crm.<dominio>/…` (ruta fija del work item de conexión), con `state` firmado y de un solo uso, y solo con `channels.manage` y MFA reciente.

### 3. Alternativa: un proveedor de la lista oficial

Si el propietario elige el plan B (D-TT-01), o si la vía directa se rechaza o no hay respuesta de TikTok en 8 semanas desde el envío:

- **Proveedor recomendado: Infobip**, el único de la lista con webhook de entrada, API de salida y DPA estándar publicados [P3] [P6] [P7]. respond.io es la segunda opción (webhooks solo en su plan Advanced; DPA a pedido) [P8] [P9].
- Mismo modelo de §4-§9 con otro transporte: el adaptador llama a la API del proveedor por `core.http` (su host en la allowlist), el webhook llega a `/webhooks/infobip/` con la autenticación HMAC que Infobip permite configurar con una clave secreta en el perfil de notificación («si está disponible» [P19]; el formato exacto de la firma: **SIN VERIFICAR**), y la clave del proveedor se guarda sellada como un token de canal.
- **El proveedor es un encargado más** con acceso a todos los mensajes: requiere su DPA firmado, su región de datos declarada en el aviso de privacidad y la decisión del propietario sobre qué datos de clientes salen a un tercero.
- La plataforma sigue siendo `TIKTOK`; el proveedor es un atributo de la cuenta (`transport`), para no duplicar la política ni la identidad.

### 4. Webhook y enrutado del tenant

- **Ruta propia:** `POST /webhooks/tiktok/` (y `/webhooks/infobip/` solo en el plan B), detrás de la misma regla de Caddy para `/webhooks/*` (ADR-033 §4.3), con tope de cuerpo y el orden firma → parsear → esquema.
- **Firma antes de parsear.** El esquema de la Business Messaging API está **SIN VERIFICAR** (§6 de la investigación). El verificador se escribe **solo** después de leer la documentación oficial que se abre con la aprobación; hasta entonces el simulador usa el esquema de TikTok for Developers (`TikTok-Signature`, HMAC-SHA256 de `timestamp.cuerpo`, margen corto) [T12] marcado como provisional. `401` vacío si falla; `200` con firma válida aunque el cuerpo no sirva.
- **Ingesta:** versiones nuevas, en una migración nueva (nunca se editan las anteriores), de las funciones de la ingesta social de la `0040`. `webhook_ingress_insert_social` y `webhook_ingress_refuse_social` aceptan hoy solo `provider = 'meta'`, las plataformas `MESSENGER` e `INSTAGRAM`, los prefijos `fb:` e `ig:` y un `routing_id` numérico de Meta; la versión nueva añade el proveedor `tiktok`, la plataforma `TIKTOK`, el prefijo `tt:` y la forma del id de la cuenta de TikTok (**SIN VERIFICAR** hasta leer la documentación que se abre con la aprobación).
- **CHECK que necesitan una versión con `TIKTOK`** en la misma migración: `webhook_ingress_route_ck` (hoy `routing_id` solo para `MESSENGER` e `INSTAGRAM`), `channel_accounts_platform_ck`, `contact_identities_platform_ck` (hoy solo `MESSENGER` e `INSTAGRAM`) y, si el id de TikTok no cabe en `^[A-Za-z0-9._-]{1,100}$`, `contact_identities_external_ck`.
- **Clave de deduplicación por cuenta**, como exige [ADR-040](ADR-040-social-ingress-key-per-account.md): `tt:msg:<id de la cuenta de empresa>:<id del mensaje>`, cada id con `urllib.parse.quote(id, safe="")`. Una clave global descartaría como duplicado el mensaje que otra cuenta conectada recibe con el mismo id. El payload se vacía al cerrar el ítem, como hoy.
- **Enrutado:** `resolve_social_account` (`SECURITY DEFINER`, `0040`) existe y hoy rechaza toda plataforma que no sea `MESSENGER` o `INSTAGRAM` y todo id que no sea numérico de Meta (`^[0-9]{5,30}$`). Una versión nueva, en la migración de T2, añade `TIKTOK` con la validación del id de TikTok y sigue devolviendo, dado `('TIKTOK', id de la cuenta de empresa)`, `(organization_id, channel_account_id)` solo para cuentas vivas, sin SQL dinámico.
- **Cruce obligatorio:** la cuenta del evento tiene que ser la del destinatario del mensaje; si no cuadra, `IGNORED` con `ACCOUNT_MISMATCH`, alerta y sin escribir nada, con su prueba de «evento firmado para una cuenta de otra organización».

### 5. Mensajes entrantes

| Evento                                      | Primer corte                                                                                                                      |
| ------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------- |
| Texto                                       | `TEXT`, saneado y acotado como en ADR-029 §5                                                                                      |
| Imagen, publicación compartida, video, otro | `UNSUPPORTED` con una etiqueta fija del código; la imagen se resuelve en un work item posterior con ClamAV y el almacén (ADR-008) |
| Mensaje propio (eco), si TikTok lo envía    | Se reconcilia por id sin duplicar; si no se puede, `IGNORED` con métrica                                                          |

- **Identidad sin teléfono:** el cliente llega con un id de TikTok. Va a `contact_identities` (existe desde la `0040`, ADR-035 §5), única por (`organization_id`, `channel_account_id`, `external_id`), y crea un contacto nuevo sin teléfono con `display_name` fijo («Cliente de TikTok») hasta que haya nombre. **Nunca se fusiona** con otro contacto por nombre, usuario, teléfono ni correo. Sus CHECK de plataforma se amplían en la migración de T2 (§4).
- El id de TikTok y el nombre visible **son datos personales**: viven en el payload de la ingesta (vaciado al cerrar) y en `contact_identities`; nunca en registros, métricas, auditoría ni outbox.

### 6. Mensajes salientes

- `TikTokAdapter` implementa `ChannelAdapter` por `core.http` (allowlist con el host de TikTok API for Business, sin redirecciones, tiempos acotados), con envío con reclamo (`SENDING`, llamada fuera de la transacción, sin reintento a ciegas tras un resultado ambiguo).
- **Errores** a códigos propios, nunca el texto de TikTok: el límite de mensajes o la ventana → `WINDOW_CLOSED`; el estrangulamiento (`40100` [T6]) → `RATE_LIMITED`; el tope diario de conversaciones (`40064`, **TERCEROS** [P11]) → `RATE_LIMITED`; un token inválido → `TOKEN_INVALID` y la cuenta en `ERROR`; «Only Business account can use Message API» (**TERCEROS** [P4]) → la cuenta en `ERROR` con «la cuenta perdió Advanced Access». Los códigos exactos se confirman con la documentación de la aprobación.
- Un limitador por cuenta (Redis) muy por debajo de 10 consultas por segundo [T6].

### 7. Política (ADR-010)

| Capacidad                | `TIKTOK`                                                                                                                                                                                                                    |
| ------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `window_hours`           | 48 [T18]                                                                                                                                                                                                                    |
| Mensajes por ventana     | Tras el primer mensaje del cliente, 10 hasta su respuesta; con respuesta, sin tope dentro de las 48 h; pasadas 48 h sin respuesta, 3 más y después `WINDOW_CLOSED` [T18]. Se cuentan los salientes desde el último entrante |
| `supports_templates`     | No                                                                                                                                                                                                                          |
| Agente humano            | No (no hay equivalente conocido; **SIN VERIFICAR**)                                                                                                                                                                         |
| `max_text_length`        | 6000 (**TERCEROS** [P1])                                                                                                                                                                                                    |
| Iniciar una conversación | Nunca: no hay plantillas ni difusiones                                                                                                                                                                                      |

- La capacidad necesita un contador de salientes por conversación, que hoy `ChannelCapabilities` no tiene: es la única ampliación de la política. La regla se re-evalúa justo antes de enviar, como hoy.
- Ante la duda, lo conservador: si el simulador o la cuenta real muestran una regla más estricta (por ejemplo, la de [P1]), se aplica la más estricta.

### 8. Datos personales, borrado y la DSPR

- **Borrado al desconectar** (exigido por [T14]): desconectar la cuenta revoca el token, la marca como desconectada y programa la **purga** de sus mensajes, identidades y medios de TikTok en un plazo fijo que decide el propietario (D-TT-05; se propone 30 días, para poder revertir una desconexión por error). La purga es un job de operación con su auditoría (sin el contenido).
- **Retención:** la de los demás canales mientras la cuenta está conectada; el propietario la declara en el aviso de privacidad.
- **IA: este ADR acota ADR-034 si se acepta.** ADR-034 (Accepted) no excluye ningún canal del copiloto. Si ADR-039 se acepta, las rutas del copiloto que envían al modelo el contenido de una conversación (`conversations/<id>/copilot/suggest/` y `…/copilot/summary/`) rechazan las conversaciones de una cuenta `TIKTOK` con un código propio y sin llamar al modelo, salvo que el propietario lo permita (D-TT-03) y el proveedor de IA figure como subencargado en la DSPR y en el aviso de privacidad. Se implementa en T2 con su prueba (§12). El borrador de la DSPR declara lo mismo (subencargados y formulario de admisión).
- **Alcances:** los que TikTok pide para solicitar la mensajería («Ad Account Management», «CTX Events Management» y «Measurement» [T2]) y «TikTok Accounts» si hace falta. El CRM no los usa para anuncios, medición ni perfilado: solo llama a los endpoints de mensajería. Si TikTok permite omitir los de anuncios, se omiten (pregunta 13 del borrador).
- Los controles que pide la DSPR y sus brechas (B1-B11) están en [tiktok-access-request-draft.md §5-§6](../research/tiktok-access-request-draft.md#5-borrador-de-las-respuestas-a-la-dspr). Las brechas técnicas (cifrado del disco, `clamd` en producción, escaneo de vulnerabilidades, borrado) se cierran con sus propios work items, que no dependen de este ADR.

### 9. Simulador

`backend/tests/tiktok_sim`: webhooks firmados con el secreto de pruebas (texto, imagen, publicación compartida, tipo desconocido, duplicado, desordenado, cuenta de otra organización, firma inválida, marca de tiempo fuera de margen) y una API falsa de envío con éxito y con cada error de §6, lentitud y corte. Fixtures marcadas como provisionales hasta leer la documentación de la aprobación. Ningún id real.

### 10. Pantalla de canales

En `/o/{slug}/canales`, junto a WhatsApp: conectar (flujo OAuth), ver el estado y lo que falta (cuenta sin Advanced Access, mensajes no abiertos a todos, webhook no visto, región no admitida), reconectar y desconectar con el aviso de la purga. Con `channels.manage` y MFA reciente; el token nunca llega al navegador.

### 11. Fuera de este ADR

Imágenes y otros medios (después, con ClamAV); mensajes automáticos de TikTok (bienvenida, preguntas sugeridas); Comment-to-Message; Messaging Ads y la Messaging Events API; TikTok Shop; cuentas de EE. UU., el EEE, Suiza y el Reino Unido; varias cuentas de TikTok por organización más allá de lo que ya permite `channel_accounts`.

### 12. Plan de la serie (solo si se acepta)

| #   | Work item                                                                                                                           | Depende de                    |
| --- | ----------------------------------------------------------------------------------------------------------------------------------- | ----------------------------- |
| T0  | Aceptar ADR-039 y las decisiones D-TT                                                                                               | —                             |
| T1  | Simulador de TikTok (§9)                                                                                                            | T0                            |
| T2  | Webhook, ingesta, enrutado e identidades (§4-§5); el copiloto rechaza las conversaciones `TIKTOK` salvo D-TT-03 (§8), con su prueba | T1                            |
| T3  | Adaptador de salida y política de 48 h con contador (§6-§7)                                                                         | T2                            |
| T4  | Conexión OAuth, desconexión con purga y pantalla de canales (§2, §8, §10); cierra la serie                                          | T3                            |
| T5  | Operativo: activar con la cuenta real, configurar el webhook y probar de punta a punta                                              | T4 y la aprobación de la DSPR |

T1 a T4 pueden avanzar mientras TikTok revisa la DSPR. Con el plan B, T2-T4 apuntan a la API del proveedor y T5 depende del contrato en lugar de la DSPR.

### 13. Decisiones del propietario

| ID      | Decisión                                                                                          | Recomendación                                                                              |
| ------- | ------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------ |
| D-TT-01 | Vía: directa, directa con plan B, o solo un proveedor                                             | **Directa con plan B (Infobip)**                                                           |
| D-TT-02 | Autorizar el envío del perfil de desarrollador, de la app y del formulario de admisión de la DSPR | Sí, cuando estén cerradas las brechas «antes» del borrador (B1, B2, B11)                   |
| D-TT-03 | ¿Los mensajes de TikTok pueden ir al copiloto de IA?                                              | No en el primer corte; revisarlo cuando la DSPR esté aprobada                              |
| D-TT-04 | Aviso de privacidad y persona responsable de la privacidad                                        | Un aviso común para todos los canales, revisado por quien asesore a la tienda en lo legal  |
| D-TT-05 | Plazo de la purga tras desconectar                                                                | 30 días                                                                                    |
| D-TT-06 | Presupuesto para NIDS o HIPS y para el escaneo de vulnerabilidades                                | Lo mínimo que ofrezca el proveedor del servidor y una línea base gratuita de OWASP ZAP     |
| D-TT-07 | Orden respecto a Instagram y Messenger (ADR-035, D-F7-09)                                         | Pedir el acceso ya; T1-T4 pueden empezar al aceptarse: F7b-04 ya creó `contact_identities` |

## Alternatives considered

| Opción                                   | A favor                                                                | En contra                                                                                                                              |
| ---------------------------------------- | ---------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------- |
| **1. Directa con plan B (recomendada)**  | Sin tercero con los mensajes; sin costo por uso conocido; mismo patrón | 6-9 semanas; depende de una revisión manual de TikTok sin precedente público para una sola cuenta                                      |
| 2. Solo un proveedor (Infobip)           | Sin DSPR propia; conexión en días                                      | Costo por uso no publicado; un encargado más con todos los mensajes, en la UE por defecto; el adaptador cuesta lo mismo que el directo |
| 3. Esperar (conclusión de F7b-07)        | Ningún esfuerzo                                                        | El propietario pidió adelantarlo; no cambia nada mientras no se pregunte a TikTok                                                      |
| 4. Proveedores fuera de la lista oficial | Algunos con precios publicados                                         | Excluidos: no figuran en la lista de TikTok [T8]                                                                                       |
| 5. Vías no oficiales                     | —                                                                      | Prohibidas (§1)                                                                                                                        |

## Consequences

- Hay que cerrar las brechas de la DSPR (políticas, cifrado del disco, `clamd` en producción, escaneo, borrado), que de todos modos mejoran la producción de los demás canales.
- `MessagingPolicyService` gana un contador de salientes por conversación.
- Una API en beta puede cambiar: el adaptador tolera códigos nuevos y lleva la cuenta a «Con problemas» en lugar de fallar en silencio.
- Si TikTok rechaza la DSPR, T1-T4 se reaprovechan con el proveedor cambiando el transporte.

## Security implications

- Firma antes de parsear; tenant solo por la función de enrutado; cruce obligatorio de la cuenta; esquema estricto; texto saneado; nombre del cliente no confiable.
- El token de la cuenta, sellado (ADR-022); el app secret, solo en el entorno; nada en el navegador ni en registros.
- El id de TikTok y el nombre son datos personales (§5); la purga al desconectar es un requisito, no una opción (§8).
- **Repositorio público:** ningún id, nombre de cuenta, dominio ni dato de la tienda en código, pruebas, issues ni documentos.

## Operational implications

- Variables nuevas: `TIKTOK_ENABLED`, `TIKTOK_APP_ID`, `TIKTOK_APP_SECRETS`, `TIKTOK_API_BASE` (y las del proveedor en el plan B).
- Un runbook de activación como el de WhatsApp: requisitos de la cuenta, configuración del webhook, prueba de punta a punta y desconexión.
- Releer cada trimestre las páginas oficiales de [T2], [T14] y [T18] mientras la API esté en beta.
