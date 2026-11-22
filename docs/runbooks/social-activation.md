# Activación de Instagram y Messenger (API oficiales de Meta) en el servidor compartido (F7b-09)

Cómo conectar al CRM de producción **la cuenta profesional de Instagram de la tienda y su página de Facebook**, con la app de Meta del propietario, sin App Review para la primera prueba: qué se configura en Meta, qué va en el servidor, qué se comprueba en la CDN y cómo se hace la prueba de seis pasos de cada canal. Las ejecuciones son F7b-10 ([#1074](https://github.com/PapiCuche/crm-gooddoggy/issues/1074), Instagram, primero por D-F7-09) y F7b-11 ([#1075](https://github.com/PapiCuche/crm-gooddoggy/issues/1075), Messenger). Este documento no activa nada.

- **Diseño:** [ADR-035](../adr/ADR-035-instagram-messenger-channels.md) (`Accepted`: Facebook Login vinculada a la página, la misma app de Meta que WhatsApp, token de página cifrado), [ADR-040](../adr/ADR-040-social-ingress-key-per-account.md) (deduplicación por cuenta), [ADR-046](../adr/ADR-046-shared-host-variant.md) (servidor compartido, CDN y regla de tasa), [ADR-029](../adr/ADR-029-whatsapp-cloud-api-channel.md) (`Proposed`: el patrón del webhook y del app secret), [ADR-022](../adr/ADR-022-secrets-at-rest.md) (token cifrado), [ADR-010](../adr/ADR-010-messaging-policy.md) (ventana y baja). Plan: [phase-7.md](../phases/phase-7.md), «Fase 7b».
- **Se apoya en** [production-shared-host.md](production-shared-host.md) (el servidor) y [whatsapp-activation.md](whatsapp-activation.md) (la parte común de la app de Meta: app secret, verify token, rotaciones). Si WhatsApp ya está activado, los pasos marcados «común» ya están hechos: solo se comprueban.
- **Repositorio público.** Solo marcadores: `<DOMINIO>`, `<SLUG>`, `<PAGE_ID>`, `<IG_ID>`, `<APP_ID>`. Ningún id real, dominio, nombre de la página, `@usuario`, token ni secreto se escribe en el repositorio, en un issue, en un PR, en el chat ni en un registro. En los issues se dice «la página de la tienda» y «la cuenta de Instagram de la tienda».
- **«Verificar en Meta»** marca un menú o un texto de Meta que no se pudo confirmar en su documentación: manda lo que se vea en pantalla. **«SIN VERIFICAR»** marca un comportamiento que la documentación oficial no confirma; la lista está al final (§11).

## Fuentes de Meta (consultadas el 2026-11-22)

| Tema                                                                                                                                                                                                                                                     | Documento oficial                                                                                                                                             |
| -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Requisitos de Instagram con Facebook Login: cuenta profesional, página vinculada, «Allow access to messages», permisos `instagram_basic`, `instagram_manage_messages`, `pages_manage_metadata`; token de página con `GET /<PAGE_ID>?fields=access_token` | [Instagram Messaging, get started](https://developers.facebook.com/documentation/business-messaging/instagram-messaging/get-started)                          |
| Campos del webhook de Instagram (`messages`, `messaging_seen`, `standby`…); «Your app must be published, regardless of app review status, to receive webhooks»; sin App Review, solo llegan eventos de personas con rol en la app                        | [Instagram Messaging, webhooks](https://developers.facebook.com/documentation/business-messaging/instagram-messaging/webhooks)                                |
| Suscribir la página vinculada (`POST /<PAGE_ID>/subscribed_apps` con `subscribed_fields` y un token de página); reintentos durante 36 h                                                                                                                  | [Instagram webhooks setup](https://developers.facebook.com/documentation/instagram-platform/webhooks/setup)                                                   |
| Caso de uso «Manage messaging & content on Instagram», «API setup with Facebook login» (una sola variante por app), «Add required messaging permissions», webhooks del objeto **Instagram**                                                              | [Instagram use case](https://developers.facebook.com/documentation/development/create-an-app/instagram-use-case)                                              |
| Comprobador de suscripciones y permisos de Instagram (solo administradores de la app)                                                                                                                                                                    | [Webhooks debugging](https://developers.facebook.com/documentation/business-messaging/instagram-messaging/webhooks/debugging)                                 |
| Requisitos de Messenger, permisos, App Review «Not required if you only send and receive messages for your own Facebook Page», acceso estándar limitado a personas con rol                                                                               | [Messenger Platform overview](https://developers.facebook.com/documentation/business-messaging/messenger-platform/overview)                                   |
| Webhook de Messenger: Callback URL y verify token, campos, firma `X-Hub-Signature-256` sobre el cuerpo escapado, `200` en 5 s, **desactivación tras una hora de fallos**                                                                                 | [Messenger webhooks](https://developers.facebook.com/documentation/business-messaging/messenger-platform/webhooks)                                            |
| Antes de la revisión, la página solo habla con cuentas con rol Administrator, Developer o Tester; pestaña **Roles**                                                                                                                                      | [Messenger quick start](https://developers.facebook.com/documentation/business-messaging/messenger-platform/getting-started/quick-start)                      |
| Caso de uso «Engage with customers on Messenger from Meta»                                                                                                                                                                                               | [Create an app (Messenger)](https://developers.facebook.com/documentation/business-messaging/messenger-platform/create-an-app)                                |
| Acceso estándar y avanzado; el avanzado exige la verificación del negocio                                                                                                                                                                                | [Access levels](https://developers.facebook.com/docs/graph-api/overview/access-levels)                                                                        |
| App predeterminada y enrutado de conversaciones; «Do not respond to standby messages»                                                                                                                                                                    | [Conversation routing](https://developers.facebook.com/documentation/business-messaging/messenger-platform/conversation-routing)                              |
| Token de usuario del sistema: sin caducidad o 60 días (Meta recomienda el de 60 días); el usuario del sistema y la app, de la misma cartera                                                                                                              | [System users: install apps and generate tokens](https://developers.facebook.com/docs/business-management-apis/system-users/install-apps-and-generate-tokens) |
| Versiones de la Graph API: `v23.0` vale hasta el **8 de octubre de 2027**; la última es `v27.0`                                                                                                                                                          | [Graph API changelog](https://developers.facebook.com/docs/graph-api/changelog)                                                                               |

## 0. Quién hace qué

| Rol             | Hace                                                                                                                                                                                                   | Necesita                                                                                                                                                            |
| --------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Propietario** | Todo lo de Meta, la CDN, el servidor (único con SSH, ADR-046 decisión 3), la pantalla de canales y el teléfono de prueba; firma cada prueba                                                            | Administrador de la cartera de negocio, de la app, de la página y de la cuenta de Instagram; Owner del CRM con segundo factor activo; acceso al servidor y a la CDN |
| **Programa**    | Prepara y comprueba lo que no necesita credenciales: el commit desplegado, sondas públicas sin secretos (§4.3), la lectura de salidas **sin ids** que el propietario pegue, y los issues F7b-10/F7b-11 | Nada de Meta ni del servidor. Nunca recibe un token, un secreto ni un id real                                                                                       |

**GO** marca un paso que el propietario tiene que autorizar de forma expresa (en el issue de la ejecución) antes de hacerlo: cambia algo que ven clientes reales, la otra aplicación del servidor o WhatsApp.

| GO   | Paso                                                                     | Por qué                                                                    |
| ---- | ------------------------------------------------------------------------ | -------------------------------------------------------------------------- |
| GO-1 | Publicar la app (modo Live) y añadirle los casos de uso (§2.2, §2.5)     | Es la misma app que WhatsApp (D-F7b-03): su modo y su revisión son comunes |
| GO-2 | `META_ENABLED=true` y las variables en el servidor, y redeploy (§4)      | Abre `/webhooks/meta/` a Internet y reinicia el CRM                        |
| GO-3 | La app del CRM como predeterminada del enrutado de conversaciones (§3.4) | Cambia quién controla los hilos de la página y de Instagram                |
| GO-4 | Conectar la cuenta en el CRM (§5)                                        | Suscribe la app a la página real                                           |
| GO-5 | Empezar la prueba en vivo de cada canal (§6)                             | Primer tráfico real                                                        |
| —    | App Review, acceso avanzado y Human Agent (§8)                           | **Fuera de este runbook**: solo después de firmar F7b-10 y F7b-11          |

## 1. Requisitos previos

**Del producto** (programa, sin credenciales):

- [ ] **R0 en vivo** ([#760](https://github.com/PapiCuche/crm-gooddoggy/issues/760)): `https://crm.<DOMINIO>/` responde con el certificado del borde público, detrás de la CDN, y el Owner entra con su segundo factor. Servidor en modo `shared` ([production-shared-host.md](production-shared-host.md)).
- [ ] El commit desplegado (`REVISION` de `/etc/crm/release.env`) contiene la serie F7b hasta F7b-06j ([#961](https://github.com/PapiCuche/crm-gooddoggy/issues/961)) y F7-11b ([#789](https://github.com/PapiCuche/crm-gooddoggy/issues/789), cuerpos de hasta 3 MiB). Se comprueba con `git merge-base --is-ancestor <commit de #961> <REVISION>`.
- [ ] Corren `backend` (la web), `worker`, `worker-media`, `ws` y `beat`. Sin `worker` no se procesa la ingesta; sin `beat`, ni la comprobación de salud («página suscrita») ni los barridos.

**De Meta** (propietario):

- [ ] **Página de Facebook** de la tienda en la cartera de negocio, con los mensajes activados.
- [ ] **Cuenta profesional de Instagram** (empresa; una de creador tiene requisitos de más, ADR-035 [S12]) **vinculada a esa página**, con «Allow access to messages» activado: en la app de Instagram, Ajustes > Mensajes y respuestas a historias > Controles de mensajes > Herramientas conectadas ([get started](https://developers.facebook.com/documentation/business-messaging/instagram-messaging/get-started)).
- [ ] **La app de Meta del despliegue** (la de WhatsApp, D-F7b-03), de la misma cartera que la página: un token de usuario del sistema exige que la app y el usuario del sistema sean de la misma cartera.
- [ ] **URL pública de la política de privacidad** de la tienda, que diga que los mensajes de Instagram y Messenger se procesan en el CRM y en Meta y cómo pedir el borrado (D-F7b-09). Meta la pide para publicar la app. Es también el aviso de privacidad del canal: sin él no hay primer tráfico real.
- [ ] **Una persona de prueba** con su propia cuenta de Facebook y su propia cuenta de Instagram, distinta de la tienda, que acepte un rol en la app (§2.4).
- **No hacen falta** para la primera prueba: la verificación del negocio, App Review, el acceso avanzado ni Human Agent (§8). Conviene **empezar ya** la verificación del negocio: la piden el acceso avanzado y la coexistencia de WhatsApp (D-COEX-01).

## 2. La app de Meta

### 2.1 Tipo y casos de uso (propietario, común con WhatsApp)

- La app es de tipo **Business** (verificar en Meta: las guías de Meta dan por hecho ese tipo para el caso de uso de Instagram). Si WhatsApp ya está activo, es la misma app: no se crea otra (D-F7b-03). Crear una segunda app es una decisión distinta (ADR-035 §2): su secreto se añadiría a `META_APP_SECRETS`.
- **Datos básicos** (App settings > Basic, verificar en Meta): nombre, correo de contacto, categoría, **URL de la política de privacidad** y de términos, icono. Sin ellos Meta no deja publicarla.

### 2.2 Añadir los casos de uso (propietario, GO-1)

En **Dashboard > Use cases > Add use case** (verificar en Meta el nombre exacto; un caso de uso añadido **no se puede quitar**):

1. **Engage with customers on Messenger from Meta** (Messenger).
2. **Manage messaging & content on Instagram** → **API setup with Facebook login** (nunca «with Instagram login»: cerrado para apps nuevas y descartado por ADR-035 §2; la app solo admite una de las dos variantes) → **Add required messaging permissions**.

### 2.3 Permisos que usa el código

Con acceso **estándar** (el que tiene toda app Business) no hay que pedir nada a Meta: se marcan en el caso de uso y se conceden al generar el token (§3.2). Ninguno más: el token del usuario del sistema da acceso a la página según lo que se le conceda (ADR-035 §8).

| Permiso                     | Qué llamada o evento del CRM lo necesita                                                                                                                                           |
| --------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `pages_show_list`           | Ver la página con el token y obtener su token de página (§3.3)                                                                                                                     |
| `pages_read_engagement`     | `GET /<PAGE_ID>?fields=id,name,instagram_business_account` al conectar y al rotar (`graph.page`)                                                                                   |
| `pages_manage_metadata`     | `POST /<PAGE_ID>/subscribed_apps` al conectar (`graph.subscribe`) y `GET /<PAGE_ID>/subscribed_apps` en la salud y al rotar (`graph.subscribed_apps`)                              |
| `pages_messaging`           | Responder por Messenger: `POST /<PAGE_ID>/messages` (`meta_page.PageAdapter`) y recibir los eventos `page`                                                                         |
| `instagram_basic`           | Leer la cuenta de Instagram vinculada (`instagram_business_account`)                                                                                                               |
| `instagram_manage_messages` | Responder por Instagram (el mismo `POST /<PAGE_ID>/messages`), recibir los eventos `instagram` y leer el nombre del cliente (`GET /<IGSID>?fields=name,username`, `graph.profile`) |
| `business_management`       | Dependencia de `pages_messaging`, `pages_show_list` e `instagram_manage_messages` según Meta; el CRM no lo usa directamente                                                        |

Todas las llamadas van a `graph.facebook.com/v23.0` (`META_GRAPH_VERSION`, fijada en el código, D-F7-08): Meta la mantiene hasta el 8 de octubre de 2027. Subirla es un cambio de código revisado, antes de esa fecha.

### 2.4 Acceso estándar: la persona de prueba (propietario)

- **Sin App Review, solo hablan con el CRM las personas con rol en la app.** Messenger: «only allow your Page to interact with Facebook accounts that have been granted the Administrator, Developer, or Tester role»; Instagram: sin revisión, los webhooks solo llegan si la persona tiene un rol en la app. Se cuenta con que un cliente real que escriba **no** llegue al CRM hasta el acceso avanzado (§8) y siga en la bandeja de Meta como hoy. La página general de Messenger dice también que App Review «Not required if you only send and receive messages for your own Facebook Page»: si clientes sin rol llegan sin revisión a la página propia es **SIN VERIFICAR 8**. Si llegan, la prueba deja de ser solo con la persona de prueba: anotarlo y decidir con el propietario antes de seguir.
- **App roles > Roles > Add people** (verificar en Meta): la cuenta de Facebook de la persona de prueba como **Tester**. La persona acepta la invitación desde su cuenta (verificar en Meta dónde: la invitación aparece en el panel de desarrolladores).
- **Instagram:** que la cuenta de Instagram de la persona de prueba cuente como «con rol» es **SIN VERIFICAR 1**. Lo más probable es que haga falta su cuenta de Instagram vinculada en el Centro de cuentas a la cuenta de Facebook con rol (verificar en Meta si la pestaña Roles ofrece además un rol «Instagram Tester»). Se comprueba en el paso 2 de la prueba (§6); si no llega nada, ver §9.
- La persona de prueba **no** es administradora de la página ni de la cuenta de Instagram de la tienda: si lo fuera, sus mensajes serían de la propia tienda.

### 2.5 Publicar la app (propietario, GO-1)

- «Your app must be published, regardless of app review status, to receive webhooks» (Instagram); para Messenger se da por hecho lo mismo. Publicar no pide App Review: pide los datos básicos de §2.1 (verificar en Meta la lista exacta del botón **Publish**).
- Es la **misma app que WhatsApp**: publicarla no cambia el número de prueba de WhatsApp ni sus destinatarios autorizados, pero queda en modo Live para todo. Por eso es GO.

## 3. Token, ids y enrutado de conversaciones

### 3.1 Usuario del sistema (propietario)

Un usuario del sistema **solo para estos canales** (p. ej., `crm-social`), aparte del de WhatsApp, con el mínimo de activos (ADR-035 §8):

1. **Business settings > Users > System users > Add** (verificar en Meta): rol **Employee**, no Admin.
2. **Assign assets:** la **app** (control total o el mínimo que Meta permita para generar tokens: verificar en Meta), la **página** de la tienda con acceso a mensajes y moderación (Meta exige las tareas `MESSAGING` y `MODERATE` en la página para el token de página y para `subscribed_apps`) y la **cuenta de Instagram** de la tienda con acceso a mensajes. Nada más: ni otras páginas, ni anuncios, ni el WABA.

### 3.2 Token del usuario del sistema (propietario)

1. **Generate token** → la app → caducidad → los siete permisos de §2.3.
2. **Caducidad (D-F7b-04, aceptada con ADR-035):** 60 días si el propietario acepta rotarlo a mano; si no, sin caducidad y rotación del runbook cada 90 días. **Hoy el CRM no avisa antes de la caducidad**: no lee `expires_at` del token (la columna existe y queda vacía). Con el de 60 días, el propietario pone un recordatorio el día 50 y rota (§7.2). Si caduca, la cuenta pasa a `ERROR` con «Falta un token válido» y los envíos fallan con `TOKEN_INVALID`; los mensajes entrantes siguen llegando (el webhook no usa el token).
3. Copiar el token **una sola vez** al gestor de contraseñas del propietario.

### 3.3 Token de página y ids (propietario, desde su ordenador)

El CRM necesita el **token de página** que se obtiene con el token del usuario del sistema: suscribir la página y responder por la Send API piden un token de página. Para que nada quede en el historial (igual que [whatsapp-activation.md](whatsapp-activation.md) §6):

```bash
read -rs SYS_TOKEN    # el token del usuario del sistema; no se muestra
auth() { printf 'Authorization: Bearer %s\n' "$SYS_TOKEN"; }
G=https://graph.facebook.com/v23.0
curl -s -G -H @<(auth) "$G/me/accounts" -d 'fields=id,name,instagram_business_account'
# Una fila por página asignada: su "id" es <PAGE_ID> y "instagram_business_account.id" es <IG_ID>.
```

Para el token de página, que **no** se imprime en la terminal sino que se copia directamente al portapapeles (macOS: `pbcopy`; Linux: `xclip -selection clipboard`):

```bash
curl -s -G -H @<(auth) "$G/<PAGE_ID>" -d 'fields=access_token' | python3 -c 'import json,sys; sys.stdout.write(json.load(sys.stdin)["access_token"])' | pbcopy
```

Se pega en el gestor de contraseñas («token de página, Instagram» y, aparte, «token de página, Messenger»: la pantalla guarda un token por cuenta y rotar uno no rota el otro; pueden ser el mismo valor). Que el token de página obtenido así no caduque mientras valga el del usuario del sistema es **SIN VERIFICAR 2**: comprobarlo en el **Access Token Debugger** de Meta (campo «Expires»), sin pegar el token en ningún otro sitio. Al terminar: `unset SYS_TOKEN`, vaciar el portapapeles y cerrar la terminal.

`<PAGE_ID>`, `<IG_ID>` y `<APP_ID>` (App settings > Basic) no son secretos, pero el repositorio es público: solo en el gestor de contraseñas y en el servidor.

### 3.4 Enrutado de conversaciones (propietario, GO-3)

- Meta reparte cada hilo entre las apps conectadas a la página; la bandeja de Meta Business Suite cuenta como otra app y **toma el control** de un hilo si alguien responde desde ella. Mientras otra app (o un agente de IA de Meta) controla el hilo, el CRM recibe los mensajes como `standby`: los guarda, pero no debe responder («Do not respond to standby messages») y la pantalla avisa «Otra app controla una conversación».
- Para la prueba: **Configuración de la página > Configuración avanzada de mensajes > Enrutado de conversaciones** (o Meta Business Suite > Configuración > Enrutado de conversaciones; verificar en Meta), la app del CRM como **app predeterminada** (D-F7b-07), y ninguna respuesta automática ni asistente de IA de Meta activos en la página ni en Instagram (verificar en Meta dónde se apagan).
- Qué ve el equipo de la tienda en la bandeja de Meta con el CRM como predeterminado y **solo acceso estándar** (se cuenta con que los clientes reales aún no lleguen al CRM, SIN VERIFICAR 8) es **SIN VERIFICAR 3**. Por eso el cambio se hace en la ventana de la prueba y, si el propietario lo prefiere, se deshace al terminar hasta el acceso avanzado.
- En Instagram el enrutado puede no ser idéntico al de Messenger (Meta solo lo documenta para Messenger): el checklist de la pantalla lo dice tras el primer mensaje: «El CRM atiende las conversaciones», «Otra app controla alguna conversación» o «Falta confirmar qué app atiende las conversaciones».

## 4. Servidor, CDN y webhook

### 4.1 Variables (propietario, GO-2)

En `/etc/crm/runtime.env` (`root`, `0600`; [infra/prod/README.md](../../infra/prod/README.md), «Secretos»), con `sudoedit /etc/crm/runtime.env`. Las tres primeras son **comunes con WhatsApp**: si ya están, no se tocan.

| Variable                    | Valor                                                                                                                                                                                          |
| --------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `META_ENABLED`              | `true`. Apagado, `/webhooks/meta/` da 404 y conectar da «no están habilitados en este despliegue»                                                                                              |
| `META_APP_SECRETS`          | El app secret (App settings > Basic > App secret > Show), copiado directamente al archivo. Común                                                                                               |
| `META_WEBHOOK_VERIFY_TOKEN` | Generado en el servidor, sin pasar por la pantalla (la orden, debajo de la tabla). Común: el mismo para WhatsApp, `page` e `instagram`                                                         |
| `META_APP_ID`               | `<APP_ID>`, solo dígitos. **Obligatorio aquí:** sin él, el eco de cada respuesta por Messenger se toma por ajeno (`FOREIGN_ECHO`) y el checklist dice «Suscripción de la página sin comprobar» |
| `META_HUMAN_AGENT_APPROVED` | `false` (o vacío). Solo `true` después de que Meta apruebe Human Agent (§8)                                                                                                                    |
| `HTTP_ALLOWED_HOSTS`        | Nada: con `META_ENABLED=true`, `graph.facebook.com` se añade solo                                                                                                                              |

El verify token, generado y escrito en el servidor sin mostrarlo:

```bash
printf 'META_WEBHOOK_VERIFY_TOKEN=%s\n' "$(openssl rand -hex 32)" | sudo tee -a /etc/crm/runtime.env >/dev/null
```

`META_GRAPH_VERSION` y `META_GRAPH_HOST` están en el código. Opcionales, con valor por omisión: los umbrales de alerta `META_ALERT_*` (F7-11a, `config/settings/base.py`). El token de página **no** va en el servidor: solo en la pantalla de canales (§5), cifrado.

### 4.2 Redeploy (propietario, GO-2)

```bash
sudo infra/prod/deploy.sh /etc/crm/release.env   # el mismo release: solo cambia el entorno
```

`deploy.sh` lee `/etc/crm/host-mode` y añade `compose.shared.yaml`; para cualquier otra orden a mano, `sudo infra/prod/compose.sh …`, nunca `docker compose` solo con `compose.yaml` ([production-shared-host.md](production-shared-host.md) §3). Si un proceso no arranca, el error nombra la variable, nunca su valor.

### 4.3 La ruta pública a través de la CDN (programa, tras GO-2)

Desde fuera del servidor, sin ningún secreto:

```bash
curl -s -o /dev/null -w '%{http_code}\n' 'https://crm.<DOMINIO>/webhooks/meta/?hub.mode=subscribe&hub.verify_token=incorrecto&hub.challenge=123'
# 403: la ruta existe y llega a Django. 404: META_ENABLED apagado. Otro código o una página HTML: la CDN o el borde.
curl -s -o /dev/null -w '%{http_code}\n' -X POST -H 'Content-Type: application/json' -d '{}' 'https://crm.<DOMINIO>/webhooks/meta/'
# 401: sin firma válida no entra nada.
```

### 4.4 La CDN y los dos proxies (propietario)

La cadena es CDN → borde público (el Caddy de la otra aplicación) → `proxy` del CRM → Django (ADR-046 §4). En `/webhooks/*`:

- **Tasa:** el `rate_limit` del `proxy` del CRM (50 peticiones cada 10 s por `{client_ip}`) y, si el propietario eligió la regla de la CDN (ADR-046 decisión 6, por /64 en IPv6), la misma cifra recomendada: 50 cada 10 s por IP. Un 429 a Meta no se pierde de inmediato (Instagram reintenta durante 36 h; Messenger reintenta unas pocas veces), pero **si la entrega falla durante una hora Meta desactiva el webhook y desuscribe la app de esa página o de esa cuenta profesional de Instagram**. Con un solo cliente de prueba no se acerca al límite (Meta agrupa hasta 1000 eventos por petición). Si alguna vez aparecen 429 a direcciones de Meta, se anota y se decide con el propietario; no se abre una excepción por rango de IP sin una lista oficial de Meta (**SIN VERIFICAR 4**).
- **Sin desafíos:** ninguna regla de la CDN (modo antibots, desafío de JavaScript o captcha, comprobación de navegador) puede aplicarse a `/webhooks/*`: Meta no los resuelve y el webhook falla. Verificar en la CDN que la regla de tasa **bloquea** (429), no desafía.
- **Sin caché** en `/webhooks/*` (el GET de verificación devuelve el `hub.challenge` de cada petición).
- **Topes de cuerpo:** el `proxy` del CRM corta en 4 MB y Django en 3 MiB; el borde público repite la tabla y guarda como mucho 1 MiB de cada cuerpo en memoria (ADR-046 §6 y §9). Un 502 en lugar de 413 indica que el borde no tiene la tabla.
- **El verify token en la query:** el GET de Meta lo trae en `hub.verify_token`. El `proxy` del CRM y el borde público escriben `?redacted` (laboratorio de ADR-046); la CDN puede guardar la URL completa en sus registros o analíticas (**SIN VERIFICAR 5**): la cuenta de la CDN se trata como sensible, y si alguien más tiene acceso, rotar el verify token tras verificar la URL (§7.3).

### 4.5 Webhooks en el panel de la app (propietario)

La misma Callback URL y el mismo verify token para los tres objetos:

1. **Messenger** (caso de uso de Messenger > Settings / Webhooks; verificar en Meta): **Callback URL** `https://crm.<DOMINIO>/webhooks/meta/` (con la barra final), **Verify token** copiado del servidor, **Verify and save**. Campos: `messages`, `message_echoes`, `message_deliveries`, `message_reads`, `messaging_handovers` y `standby`: los mismos que el CRM pide al suscribir la página (`graph.SUBSCRIBED_FIELDS`).
2. **Instagram** (caso de uso de Instagram > Webhooks > objeto **Instagram**): la misma URL y el mismo token. Campos: `messages` (entrantes y ecos), `messaging_seen` (leídos) y `standby`. Los demás (`message_reactions`, `messaging_postbacks`, `messaging_referral`) el CRM los ignora en el primer corte: no hace falta suscribirlos.
3. **Comprobar el registro de la verificación** en el servidor: `sudo infra/prod/compose.sh logs --since 10m proxy | grep -c 'hub.verify_token'` tiene que dar `0`. Si da más, el `proxy` no cumple: corregirlo y rotar el verify token (§7.3). El borde público, igual, en su propio registro (lo mira su operador, que es el propietario).

La suscripción de la **página** no se hace aquí: la hace el CRM al conectar (§5) con `POST /<PAGE_ID>/subscribed_apps`, también para Instagram (por su página vinculada). Que esos campos de página basten para Instagram es SIN VERIFICAR 7 de ADR-035: lo dice la prueba (§6, paso 2).

## 5. Conectar desde la pantalla de canales (propietario, GO-4)

1. Entrar como **Owner** (o un rol con `channels.manage`) en `https://crm.<DOMINIO>/o/<SLUG>/canales`, con un **segundo factor activo**: conectar y rotar piden MFA reciente (D-F7-11). Hay un tope de 5 intentos cada 10 minutos por persona.
2. **Instagram primero** (D-F7-09): **Conectar Instagram** →

   | Campo                                    | Valor                                                                |
   | ---------------------------------------- | -------------------------------------------------------------------- |
   | Nombre de la cuenta                      | Interno, p. ej. «Instagram tienda» (una línea, hasta 100 caracteres) |
   | ID de la página de Facebook vinculada    | `<PAGE_ID>`                                                          |
   | ID de la cuenta profesional de Instagram | `<IG_ID>`                                                            |
   | Token de acceso                          | El token de **página** (§3.3), del gestor de contraseñas             |

3. El CRM comprueba con Meta que el token gestiona la página y que su `instagram_business_account` es `<IG_ID>`, suscribe la app a la página y guarda la cuenta **Pendiente** con el token cifrado. Si Meta no lo acepta: «Meta no aceptó ese token…» y no se guarda nada (§9).
4. Checklist esperado: «Token válido», «Falta recibir el primer mensaje de Meta» y «Página suscrita a la app del CRM» (tras la primera comprobación de salud, cada `CHANNEL_HEALTH_INTERVAL_MINUTES`; «Suscripción de la página sin comprobar» si falta `META_APP_ID`).
5. **Messenger** (F7b-11): **Conectar Messenger** con el nombre, `<PAGE_ID>` y su token de página. Instagram y Messenger de la misma página conviven en la misma organización; una página no puede estar en dos organizaciones (mismo error genérico).

## 6. Prueba en vivo, seis pasos por canal (GO-5)

Una por canal, Instagram primero. La persona de prueba (§2.4) usa **su** teléfono o cuenta; nadie de la tienda responde desde la app de Instagram ni desde Meta Business Suite durante la prueba (D-F7b-07).

| #   | Paso                                                                                                                                                                  | Quién                                            | Qué se comprueba                                                                                                                                                                                                                                                                              |
| --- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | **El dispositivo autorizado envía.** Desde la cuenta de prueba, un mensaje directo a la cuenta de Instagram de la tienda (o a la página por Messenger): «hola prueba» | Persona de prueba                                | Sale de su app sin error                                                                                                                                                                                                                                                                      |
| 2   | **El CRM lo recibe.**                                                                                                                                                 | Propietario (servidor); programa (lee la salida) | La orden de registros de debajo de la tabla muestra `meta.webhook.received` y ninguno de los otros. En `/canales`, la cuenta pasa a **Conectada** con «Mensajes de Meta recibidos»                                                                                                            |
| 3   | **Aparece en el Inbox.**                                                                                                                                              | Propietario                                      | En `/o/<SLUG>/inbox`, la conversación con un contacto nuevo («Cliente de Instagram», que pasa al nombre o `@usuario` del perfil en segundos; «Cliente de Messenger» se queda así: el nombre de Messenger pide una función avanzada), sin recargar si `INBOX_WS=on`. No hay aviso de `standby` |
| 4   | **Responde un humano.** Desde el Inbox, antes de 24 h, «respuesta de prueba»                                                                                          | Propietario (o un vendedor con `messages.send`)  | La burbuja pasa por «Enviando» a **Enviado** sin error                                                                                                                                                                                                                                        |
| 5   | **El remitente lo recibe.**                                                                                                                                           | Persona de prueba                                | La respuesta llega a su Instagram o Messenger, **una sola vez**                                                                                                                                                                                                                               |
| 6   | **El CRM registra el estado.** La persona abre el chat                                                                                                                | Propietario                                      | Messenger: **Enviado → Entregado → Leído**. Instagram: **Enviado → Leído** (Instagram no manda entregas). Y el eco de la respuesta se reconcilia: ningún mensaje duplicado en el chat y ningún eco ajeno (abajo)                                                                              |

**Registros del paso 2** (el propietario pega la salida; sin ids ni textos):

```bash
sudo infra/prod/compose.sh logs --since 10m backend worker | grep -E '"meta\.webhook\.(received|invalid|rejected|account_mismatch|standby)"'
```

**Ecos y motivos ignorados** (paso 6, sin payload ni ids personales; con `crm_app`, nunca con `crm_migrator`):

```bash
sudo infra/prod/compose.sh exec backend python manage.py shell -c "from collections import Counter; from apps.conversations import ingress; print(Counter(r[7] for r in ingress.listing('IGNORED', 200)))"
```

Lo esperado tras la prueba: ningún `FOREIGN_ECHO`, `ACCOUNT_MISMATCH` ni `ECHO_MID_CONFLICT` en ese rato. Un `FOREIGN_ECHO` por cada respuesta del CRM en Messenger significa `META_APP_ID` vacío o equivocado (§4.1). En Instagram, que el eco traiga `app_id` y devuelva el `metadata` enviado son SIN VERIFICAR 11 y 12 de ADR-035: si la respuesta queda **Enviado** y el chat no se duplica, la prueba vale aunque el eco no correlacione (queda `UNKNOWN_MESSAGE` o `FOREIGN_ECHO` tras tres intentos); anotarlo en el issue.

**Comprobaciones extra, mismas condiciones:** «STOP» desde la cuenta de prueba confirma la baja por ese canal y «ALTA» la restituye (ADR-010; la confirmación sale solo dentro de las 24 h); una mención en una historia aparece como «Te mencionó en una historia», sin imagen.

La prueba la firma el propietario en su issue (F7b-10 para Instagram, F7b-11 para Messenger), con la hora y el resultado de cada paso, **sin** ids, nombres ni capturas con datos personales.

## 7. Marcha atrás y rotaciones

### 7.1 Apagar

- **Una cuenta:** en `/canales`, **Desconectar** (sin MFA): el token se invalida y la cuenta queda `DISCONNECTED`. No cambia nada en Meta: la app sigue suscrita a la página. Para cortarlo en Meta, `DELETE /<PAGE_ID>/subscribed_apps` con el token de página (desde el ordenador del propietario, como §3.3), o quitar la app en la configuración de apps conectadas de la página.
- **Todo Meta (también WhatsApp):** `META_ENABLED=false` y `sudo infra/prod/deploy.sh /etc/crm/release.env`. `/webhooks/meta/` da 404; Instagram reintenta 36 h, Messenger unas pocas veces, y tras una hora de fallos Meta desactiva el webhook y desuscribe la app de la página o de la cuenta de Instagram. Al volver a encender: **Verify and save** de nuevo (§4.5) y **volver a suscribir** la página, que «Verify and save» no hace: desconectar y conectar de nuevo cada cuenta en `/canales` (§5), o `POST /<PAGE_ID>/subscribed_apps` con los campos de §4.5 y el token de página (como §3.3). Comprobar después «Página suscrita a la app del CRM».
- **Enrutado:** devolver la app predeterminada a la que estaba antes (§3.4).

### 7.2 Rotar el token de página

Generar un token nuevo del usuario del sistema (§3.2), obtener el de página (§3.3) y en `/canales`, **Rotar token** (con MFA) en Instagram y, aparte, en Messenger. Después revocar el token anterior del usuario del sistema en Meta (verificar en Meta el menú).

### 7.3 App secret y verify token

Comunes con WhatsApp: [whatsapp-activation.md](whatsapp-activation.md) §9.4. Un app secret nuevo afecta a los tres canales a la vez; el verify token nuevo se cambia en los tres objetos del webhook (§4.5).

## 8. Después de las dos pruebas (fuera de este runbook)

Para que escriban **clientes reales** se cuenta con que haga falta el acceso avanzado (SIN VERIFICAR 8 para la página propia), con decisión propia del propietario:

1. **Verificación del negocio** (la piden el acceso avanzado y la coexistencia de WhatsApp).
2. **App Review** del acceso avanzado a los siete permisos de §2.3 y de la función **Human Agent** (D-F7b-08), con los videos que pida Meta.
3. Con Human Agent aprobado, `META_HUMAN_AGENT_APPROVED=true` y redeploy: solo entonces una persona responde entre las 24 h y los 7 días.
4. La app del CRM como predeterminada de forma permanente y el equipo atendiendo solo desde Good Doggy (D-F7b-07).

## 9. Problemas frecuentes

| Síntoma                                                                                                                                       | Causa probable                                                                                                                                                                                            | Qué hacer                                                                                                                                                                                                               |
| --------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Meta no guarda la Callback URL                                                                                                                | `META_ENABLED` apagado (404); verify token distinto (403); la CDN desafía o cachea `/webhooks/*`; certificado del borde no válido; la URL sin la barra final                                              | §4.3 desde fuera; reglas de la CDN (§4.4); copiar de nuevo el token del servidor                                                                                                                                        |
| «Meta no aceptó ese token…» al conectar                                                                                                       | Token del usuario del sistema en lugar del de página; página o cuenta de Instagram no asignadas al usuario del sistema; falta un permiso; `<IG_ID>` no es la cuenta vinculada a `<PAGE_ID>`               | §3.1–§3.3; el `GET /me/accounts` con el mismo token tiene que mostrar la página y su Instagram                                                                                                                          |
| «Meta no responde ahora»                                                                                                                      | Red, Meta o un límite de uso de Meta                                                                                                                                                                      | Reintentar en unos minutos (tope de 5 intentos cada 10 minutos)                                                                                                                                                         |
| La cuenta sigue «Falta recibir el primer mensaje de Meta»                                                                                     | App sin publicar (§2.5); la persona no tiene rol o no aceptó la invitación (§2.4, SIN VERIFICAR 1 en Instagram); falta el objeto o el campo en §4.5; la página no está suscrita; la CDN bloquea o desafía | Comprobador de webhooks de Instagram (solo administradores de la app); el botón **Test** del campo en el panel (si llega, el camino CDN → CRM funciona y el problema es de rol o de acceso); registros del paso 2       |
| `meta.webhook.standby` y aviso «Otra app controla una conversación»                                                                           | Otra app, la bandeja de Meta o un asistente de IA de Meta controla el hilo                                                                                                                                | §3.4; no responder ese hilo desde el CRM                                                                                                                                                                                |
| Muchos 401 en el registro del `proxy`                                                                                                         | `META_APP_SECRETS` no tiene el secreto vigente de **esta** app, o alguien prueba la URL                                                                                                                   | Comparar con App settings > Basic; [whatsapp-activation.md](whatsapp-activation.md) §9.4                                                                                                                                |
| 429 en la CDN o en el `proxy`                                                                                                                 | Regla de tasa (§4.4)                                                                                                                                                                                      | Anotar hora y volumen; si son de Meta, decidir con el propietario. Nunca quitar la regla sin decisión                                                                                                                   |
| 502 en vez de 413                                                                                                                             | El borde público no repite la tabla de topes (ADR-046 §6)                                                                                                                                                 | Lo corrige el operador del borde, en su repositorio                                                                                                                                                                     |
| «La página no tiene suscrita la app del CRM» (detalle `PAGE_NOT_SUBSCRIBED`: «…los mensajes nuevos no llegan. Vuelve a suscribirla en Meta.») | Meta desactivó el webhook tras una hora de fallos y desuscribió la página o la cuenta de Instagram, o alguien quitó la app de la página                                                                   | Corregir la causa y **Verify and save** (§4.5). Rotar el token no vuelve a suscribir: desconectar y conectar de nuevo (§5), o `POST /<PAGE_ID>/subscribed_apps` con los campos de §4.5 y el token de página (como §3.3) |
| «El acceso a los mensajes de Instagram está apagado»                                                                                          | «Allow access to messages» apagado en la app de Instagram                                                                                                                                                 | §1; después, rotar el token o esperar la siguiente comprobación                                                                                                                                                         |
| «Falta un token válido»                                                                                                                       | Token caducado (60 días, §3.2), revocado o sin la página                                                                                                                                                  | §7.2                                                                                                                                                                                                                    |
| «Pasaron más de 24 horas…»                                                                                                                    | Ventana cerrada y Human Agent no aprobado                                                                                                                                                                 | Que la persona de prueba escriba de nuevo                                                                                                                                                                               |
| La respuesta llega dos veces o el chat se duplica                                                                                             | No debería: Meta no deduplica y el CRM no reintenta un envío ambiguo                                                                                                                                      | Anotar la hora y abrir un issue; no reenviar a mano                                                                                                                                                                     |

Si algo no está en esta tabla: apagar (§7.1), anotar la hora y el síntoma **sin** ids ni datos personales en el issue de la ejecución, y no improvisar cambios en Meta ni en la CDN.

## 10. Lo que hace el propietario, en orden

| #   | Paso                                                                                                                    | GO   |
| --- | ----------------------------------------------------------------------------------------------------------------------- | ---- |
| 1   | Página, cuenta profesional de Instagram vinculada con «Allow access to messages», política de privacidad publicada (§1) | —    |
| 2   | Casos de uso de Messenger e Instagram (Facebook login) y permisos de mensajería (§2.2, §2.3)                            | GO-1 |
| 3   | Persona de prueba con rol Tester (§2.4)                                                                                 | —    |
| 4   | Publicar la app (§2.5)                                                                                                  | GO-1 |
| 5   | Usuario del sistema `crm-social`, token, token de página e ids (§3)                                                     | —    |
| 6   | Variables y redeploy (§4.1, §4.2)                                                                                       | GO-2 |
| 7   | Reglas de la CDN en `/webhooks/*` (§4.4) y webhooks de Messenger e Instagram en la app (§4.5)                           | —    |
| 8   | Enrutado de conversaciones (§3.4)                                                                                       | GO-3 |
| 9   | Conectar Instagram y Messenger en `/canales` (§5)                                                                       | GO-4 |
| 10  | Prueba de seis pasos de Instagram y firma en F7b-10; después, la de Messenger y firma en F7b-11 (§6)                    | GO-5 |

## 11. SIN VERIFICAR (no se usa como premisa)

1. Cómo reconoce Meta como «con rol en la app» a una **cuenta de Instagram** que escribe (su cuenta de Facebook con rol vinculada en el Centro de cuentas, o un rol de Instagram aparte). Hay informes de desarrolladores, no oficiales, de mensajes reales que no producen webhook con acceso estándar aunque el botón **Test** funcione.
2. Que el token de página obtenido con un token del usuario del sistema no caduque mientras valga este.
3. Qué ve la bandeja de Meta Business Suite con la app del CRM como predeterminada y solo acceso estándar.
4. Una lista oficial de las IP desde las que Meta entrega webhooks.
5. Si la CDN guarda la URL completa (con `hub.verify_token`) en sus registros o analíticas.
6. Los nombres exactos de los menús de Meta marcados «verificar en Meta».
7. De ADR-035 siguen abiertos el 7 (campos de la página para Instagram), el 11 y el 12 (ecos de Instagram): la prueba de §6 los responde y se anotan en F7b-10.
8. Si clientes **sin** rol en la app llegan al CRM sin App Review cuando la página es la propia («Not required if you only send and receive messages for your own Facebook Page», [Messenger Platform overview](https://developers.facebook.com/documentation/business-messaging/messenger-platform/overview)) o si hace falta el acceso avanzado (las páginas de webhooks y de inicio rápido dicen lo segundo). El runbook supone lo conservador: solo personas con rol hasta §8.
