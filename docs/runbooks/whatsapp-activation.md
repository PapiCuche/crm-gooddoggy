# Activación de WhatsApp (Cloud API de Meta) con un número de prueba (F7-12a)

Cómo encender el canal de WhatsApp en producción **con un solo número de prueba**, en orden y sin adivinar: qué se configura en Meta, qué va en el entorno del servidor y cómo se comprueba. La ejecución es F7-12b ([#763](https://github.com/PapiCuche/crm-gooddoggy/issues/763)); este documento no activa nada.

- **Diseño:** [ADR-029](../adr/ADR-029-whatsapp-cloud-api-channel.md) (`Proposed`), [ADR-022](../adr/ADR-022-secrets-at-rest.md) (token cifrado), [ADR-010](../adr/ADR-010-messaging-policy.md) (ventana y baja), ADR-033 (`Proposed`, [#752](https://github.com/PapiCuche/crm-gooddoggy/issues/752): servidor y almacén de secretos). Plan: [phase-7.md](../phases/phase-7.md) (F7-12 y «Qué necesita el propietario»).
- **Repositorio público.** Aquí solo hay marcadores (`<DOMINIO>`, `<WABA_ID>`, `<PHONE_NUMBER_ID>`, `<SLUG>`…). Ningún id real, dominio, número, token, PIN ni secreto se escribe en el repositorio, en un issue, en un PR, en el chat ni en un registro.
- **«Verificar en Meta»** marca cada paso de la interfaz de Meta que no se pudo confirmar en su documentación oficial: los menús de Meta cambian, y manda lo que se vea en pantalla.

## Fuentes de Meta (consultadas el 2026-11-13)

| Tema                                                                                                                  | Documento oficial                                                                                                                                        |
| --------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Endpoint del webhook: GET de verificación, firma `X-Hub-Signature-256`, reintentos, menú **WhatsApp > Configuration** | [Create a webhook endpoint](https://developers.facebook.com/documentation/business-messaging/whatsapp/webhooks/create-webhook-endpoint)                  |
| `GET`, `POST` y `DELETE /<WABA_ID>/subscribed_apps`                                                                   | [Subscribed apps API](https://developers.facebook.com/documentation/business-messaging/whatsapp/reference/whatsapp-business-account/subscribed-apps-api) |
| `POST /<PHONE_NUMBER_ID>/register` y `/deregister`, PIN de dos pasos, tope de 10 intentos en 72 h                     | [Register a business phone number](https://developers.facebook.com/documentation/business-messaging/whatsapp/business-phone-numbers/registration)        |
| Número de prueba registrado automáticamente, `GET /<WABA_ID>/phone_numbers`, PIN, nombre visible                      | [Business phone numbers](https://developers.facebook.com/documentation/business-messaging/whatsapp/business-phone-numbers/phone-numbers)                 |
| Usuario del sistema, activos y permisos del token                                                                     | [Access tokens](https://developers.facebook.com/documentation/business-messaging/whatsapp/access-tokens)                                                 |
| App, número de prueba y destinatario («From» / «To» en **API Setup**)                                                 | [Get started](https://developers.facebook.com/documentation/business-messaging/whatsapp/get-started)                                                     |
| App secret y requisitos del modo Live                                                                                 | [Basic settings](https://developers.facebook.com/documentation/development/create-an-app/app-dashboard/basic-settings)                                   |
| Ventana de atención de 24 h                                                                                           | [Send messages](https://developers.facebook.com/documentation/business-messaging/whatsapp/messages/send-messages)                                        |

Los ejemplos de Meta usan hoy la versión `v27.0` de la Graph API. El código fija `META_GRAPH_VERSION = "v23.0"` (D-F7-08); las llamadas manuales de este runbook usan la misma versión que el código. **Verificar en Meta** que `v23.0` sigue vigente y su fecha de retirada antes de activar; subirla es un cambio de código revisado, no de entorno.

## 0. Quién hace qué

| Rol                 | Hace                                                                                                    | Necesita                                                                                                    |
| ------------------- | ------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------- |
| **Propietario**     | Todo lo de Meta (pasos 1 a 3, 5 y 6), conectar la cuenta en el CRM (paso 7) y firmar la prueba (paso 8) | Acceso de administrador a la cartera de negocio de Meta y a la app; Owner del CRM con segundo factor activo |
| **Quien despliega** | El entorno del servidor y el redeploy (paso 4)                                                          | Acceso al servidor y a su almacén de secretos                                                               |

Si son la misma persona, mejor: el verify token y el app secret no tienen que viajar entre dos personas. Si son dos, se pasan por un gestor de contraseñas compartido o en persona, **nunca** por chat, correo, issue ni PR.

## 1. Requisitos previos

No se empieza sin todo esto. Cada punto se comprueba, no se supone.

**Del producto (código y despliegue):**

- [ ] **R0 en vivo** ([#760](https://github.com/PapiCuche/crm-gooddoggy/issues/760)): el CRM responde en `https://crm.<DOMINIO>/` con un certificado válido (Meta no acepta certificados autofirmados) y el Owner entra con su segundo factor.
- [ ] Fusionados en `main` y desplegados: F7-03c ([#600](https://github.com/PapiCuche/crm-gooddoggy/issues/600), purga a 90 días y vaciado del payload), F7-02e ([#592](https://github.com/PapiCuche/crm-gooddoggy/issues/592), tope de las rutas de la cuenta), F7-04b ([#607](https://github.com/PapiCuche/crm-gooddoggy/issues/607), tipos no soportados y ventana) y F7-08b ([#623](https://github.com/PapiCuche/crm-gooddoggy/issues/623), palabras de baja). Lo están a 2026-11-13; comprobarlo con `git log` en el commit desplegado.
- [ ] **El proxy de producción** ([#754](https://github.com/PapiCuche/crm-gooddoggy/issues/754), `infra/prod`) para `/webhooks/*`: limita el cuerpo (`request_body { max_size 2MB }`), limita la tasa por IP y **no registra la query** (el GET de Meta trae el verify token en `hub.verify_token`). El proxy local (`infra/docker/proxy/Caddyfile`) no hace nada de esto: no sirve para producción. Comprobarlo leyendo la configuración desplegada y un registro de acceso de prueba (paso 5).
- [ ] Corren `web`, `worker`, `worker-media`, `ws` y `beat`, con Redis (`CELERY_BROKER_URL`). Sin `worker` no se procesa la ingesta; sin `beat` no hay barridos ni comprobación de salud.
- [ ] `SECRETS_KEKS` configurada (producción no arranca sin ella; sin ella no se puede guardar el token cifrado).
- [ ] `APP_ORIGIN=https://crm.<DOMINIO>` (o el origen en `DJANGO_CSRF_TRUSTED_ORIGINS`): el WebSocket del Inbox solo acepta ese origen.

**De Meta (propietario; [checklist #781](https://github.com/PapiCuche/crm-gooddoggy/issues/781)):**

- [ ] **Cartera de negocio de Meta** (Business Manager / Business Suite) con la **verificación del negocio** iniciada o aprobada. Para el número de prueba puede no hacer falta todavía (verificar en Meta); para el número de la tienda, sí.
- [ ] **App de Meta tipo Business** con el producto (caso de uso) **WhatsApp**: «Connect with customers through WhatsApp» en el panel de apps.
- [ ] Datos básicos de la app (**App settings > Basic**: verificar en Meta el nombre exacto del menú): nombre, correo de contacto, **categoría**, **URL de la política de privacidad** y de términos. Meta exige la categoría y los términos para pasar a modo **Live**. La política debe decir que los mensajes se procesan en el CRM y en Meta (fila 11 de la tabla del propietario).
- [ ] **WABA** (cuenta de WhatsApp Business) en la cartera, creada al completar «Get started» o a mano.
- [ ] **Modo de la app:** antes del número real, pasar la app a **Live** (requisitos de arriba). Si con el número de prueba en modo desarrollo el primer evento no llega (paso 8.2), pasarla a Live antes de seguir (verificar en Meta qué eventos entrega una app en desarrollo).
- [ ] **Palabras de baja** (fila 11, D-F7-05): sin configurar valen `STOP`, `BAJA` y `CANCELAR` (baja) y `ALTA` (alta); el aviso al cliente las menciona.
- **No hacen falta para esta prueba:** el **método de pago** del WABA y las **plantillas** (filas 9 y 10 de la tabla del propietario). Sin plantillas solo se responde dentro de la ventana de 24 h; llegan con F7-10.

## 2. Número de prueba primero

- Se usa **el número de prueba que Meta crea** al completar «Get started». Según Meta, ese número «se genera y se registra automáticamente»: **no** se llama a `/register` para él (paso 6.2 solo aplica al número real).
- En **WhatsApp > API Setup** se añaden los **destinatarios de prueba** (campo «To»): los teléfonos de las personas autorizadas a escribir y recibir. Meta limita cuántos (cinco, según guías de terceros: **verificar en Meta**) y cada uno confirma con un código que recibe por WhatsApp (verificar en Meta).
- Anotar, **solo en el gestor de contraseñas o en el almacén del servidor, nunca aquí**, el **ID del número de teléfono** (`phone_number_id`) y el **ID de la cuenta de WhatsApp Business** (`WABA_ID`) que muestra API Setup. No son secretos, pero el repositorio es público.
- **El número real de la tienda solo se conecta cuando el propietario firma por escrito la prueba de este número** (criterio de F7-12b; decisión D-F7-02 y, en coexistencia, ADR-032 y F7-13k). Hasta entonces no se registra ni se conecta ningún otro número.

## 3. Usuario del sistema, token y app secret

### 3.1 Usuario del sistema y token permanente

Según [Access tokens](https://developers.facebook.com/documentation/business-messaging/whatsapp/access-tokens):

1. **Business settings > System Users > + Add.** Nombre neutro (p. ej., `crm-whatsapp`) y rol **Admin** (acceso completo a las cuentas de WhatsApp de la cartera) o **Employee** (hay que darle acceso a cada cuenta).
2. En el usuario: **Assign assets** → la app, con **Manage app**. Recargar hasta que muestre **Full control** (puede tardar unos minutos).
3. Si el rol es Employee: **Settings > Accounts > WhatsApp accounts** → el WABA → **People > + Add people** → el usuario del sistema con acceso **Full**. Meta pide hacerlo también en la «Messaging account» si existe y no depender de la herencia (verificar en Meta).
4. **Generate token** → la app → preferencia de caducidad **sin caducidad** (Meta solo dice «choose a token expiration preference»: verificar en Meta el nombre de la opción) → permisos:
   - `whatsapp_business_messaging` (enviar y recibir);
   - `whatsapp_business_management` (leer el WABA y sus números, `subscribed_apps`, `register`);
   - `business_management`: la guía oficial lo lista además de los dos anteriores. El CRM no lo usa directamente; añadirlo si Meta lo exige al generar el token.
5. **Copiar el token una sola vez** al gestor de contraseñas del propietario: Meta no lo vuelve a mostrar. El CRM lo recibe **solo** en la pantalla de canales (paso 7), donde se guarda cifrado (ADR-022) y solo se vuelve a ver su final de cuatro caracteres. El token **no** va en el entorno del servidor.

El código acepta un token de 20 a 512 caracteres `A-Z a-z 0-9 _ . -` (`TOKEN_FORMAT`, a confirmar con Meta). Meta pide tratarlo como una cadena opaca.

### 3.2 App secret

- **App settings > Basic > App secret > Show** (verificar en Meta). Se copia directamente al almacén de secretos del servidor como `META_APP_SECRETS` (paso 4). No se guarda en el CRM, en la base de datos ni en el gestor de contraseñas compartido si no hace falta.
- Meta no permite rotarlo por API; solo se restablece en Basic settings (rotación en el paso 9.3).

### 3.3 Dónde va cada secreto

| Secreto                        | Dónde vive                                                                                                                                                         | Dónde **nunca**                                                                               |
| ------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------- |
| Token del usuario del sistema  | Pegado en `/o/<SLUG>/canales` → `credentials`, cifrado (ADR-022). Copia de respaldo solo en el gestor de contraseñas del propietario                               | Entorno del servidor, repositorio, chat, issues, PRs, correo, registros, capturas de pantalla |
| App secret                     | `META_APP_SECRETS` en el almacén de secretos del servidor (ADR-033: archivo de entorno fuera del repo, `root`, modo `0600`, o el gestor de secretos del proveedor) | Ídem, y la base de datos                                                                      |
| Verify token                   | `META_WEBHOOK_VERIFY_TOKEN` en el mismo almacén **y** el campo «Verify token» de Meta (paso 5)                                                                     | Ídem; tampoco en el registro de acceso del proxy                                              |
| PIN de dos pasos (número real) | Gestor de contraseñas del propietario                                                                                                                              | Ídem; el CRM no lo necesita                                                                   |

Si un secreto aparece por error en un sitio de la columna derecha: se rota de inmediato (paso 9.3 o 9.4) y se borra el mensaje, aunque se crea que nadie lo vio.

## 4. Entorno del servidor y redeploy

### 4.1 Variables

Todas las `META_*` del código (`backend/config/settings/base.py`; comprobado con `grep -rhoE '\bMETA_[A-Z_]+' backend/config` el 2026-11-13):

| Variable                    | Dónde                              | Valor                                                                                                                                                     |
| --------------------------- | ---------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `META_ENABLED`              | Entorno                            | `true` para encender. Apagado (por omisión) no se exige nada y el webhook da 404                                                                          |
| `META_APP_SECRETS`          | Entorno (secreto)                  | El app secret vigente; durante una rotación, `<nuevo>,<anterior>`. De 1 a 3 secretos distintos, cada uno de 16 a 128 letras o dígitos, el vigente primero |
| `META_WEBHOOK_VERIFY_TOKEN` | Entorno (secreto)                  | De 16 a 128 caracteres ASCII visibles, sin espacios, distinto de cualquier app secret                                                                     |
| `META_GRAPH_VERSION`        | **Código** (`v23.0`)               | No se configura: subirla es un cambio revisado (D-F7-08)                                                                                                  |
| `META_GRAPH_HOST`           | **Código** (`graph.facebook.com`)  | Con `META_ENABLED=true` se añade solo a `HTTP_ALLOWED_HOSTS`; no hace falta ponerlo                                                                       |
| `META_MEDIA_HOSTS`          | **Código** (`lookaside.fbsbx.com`) | Solo para bajar medios; no entra en `HTTP_ALLOWED_HOSTS`                                                                                                  |

Generar el verify token **en el servidor** y escribirlo directamente en el almacén (no en la terminal compartida ni en el historial):

```bash
openssl rand -hex 32   # 64 caracteres hexadecimales: dentro del rango 16–128
```

**Topes de tasa.** `CHANNEL_CHECK_THROTTLE` (5 peticiones cada 10 minutos por persona en conectar, rotar y desconectar) y `CHANNEL_SEND_LIMIT` (20 envíos por segundo y cuenta, contados en Redis) están **fijados en el código**, no en el entorno. Con `META_ENABLED=true`, `web`, `worker`, `ws` y `beat` no arrancan si no son válidos o si falta `CELERY_BROKER_URL` (`env.check_meta`, llamado desde `production.py`). Nada que configurar; solo saber que un arranque fallido con un mensaje sobre `CHANNEL_CHECK_THROTTLE`, `CHANNEL_SEND_LIMIT` o `CELERY_BROKER_URL` viene de aquí.

**Otras del canal (opcionales, con valor por omisión seguro):** `MEDIA_SEND_UNSCANNED=false` (D-F7-04: sin antivirus no sale ningún medio; no cambiarlo sin la decisión del propietario), `CHANNEL_HEALTH_INTERVAL_MINUTES=15`, `INBOX_SENDING_DEADLINE_MINUTES=15`, `INBOX_SWEEP_AFTER_MINUTES=5`, `INBOX_GIVE_UP_AFTER_MINUTES=120`.

**Qué procesos las reciben:** `web`, `worker`, `worker-media`, `ws` y `beat`. **El job de migraciones no** (ADR-002 §1.1).

### 4.2 Arrancar con el canal encendido

1. Escribir las tres variables en el almacén de secretos del servidor.
2. Redeploy del backend con el procedimiento de producción (`deploy.sh`, [#756](https://github.com/PapiCuche/crm-gooddoggy/issues/756), cuando exista; mientras, recrear los cinco procesos con el entorno nuevo). Si un proceso no arranca, el error solo nombra la variable, nunca su valor: corregirla y repetir.
3. Comprobar que el webhook existe y rechaza lo que no es de Meta (desde cualquier máquina):

   ```bash
   curl -s -o /dev/null -w '%{http_code}\n' 'https://crm.<DOMINIO>/webhooks/meta/?hub.mode=subscribe&hub.verify_token=incorrecto&hub.challenge=123'
   # 403: la ruta existe y el token no coincide. 404: META_ENABLED sigue apagado en ese proceso.
   curl -s -o /dev/null -w '%{http_code}\n' -X POST -H 'Content-Type: application/json' -d '{}' 'https://crm.<DOMINIO>/webhooks/meta/'
   # 401: sin firma válida no entra nada.
   ```

   No probar con el verify token real en un `curl`: quedaría en el historial de la terminal.

### 4.3 Tiempo real del Inbox (`INBOX_WS=on`)

`NEXT_PUBLIC_INBOX_WS` es una variable de **build** del frontend: se incrusta al hacer `next build` (`frontend/Dockerfile`, `ARG NEXT_PUBLIC_INBOX_WS=off`; en compose, `INBOX_WS` → `NEXT_PUBLIC_INBOX_WS`). Cambiarla exige **reconstruir la imagen del frontend**, no basta con reiniciarla:

1. `INBOX_WS=on` en el entorno con el que se construye la imagen (no es un secreto).
2. Reconstruir y desplegar la imagen del frontend (en local: `docker compose -f infra/docker/compose.yaml --env-file infra/env/.env up -d --build frontend`; en producción, el build de imágenes de #756).
3. Comprobar que el servicio `ws` corre y que el proxy lleva `/ws/*` a él. Sin esto el Inbox funciona, pero hay que actualizar a mano para ver un mensaje nuevo.

## 5. Webhook en Meta

1. **App Dashboard > WhatsApp > Configuration** (con el caso de uso «Connect with customers through WhatsApp»: **Use cases > Customize > Configuration**).
2. **Callback URL:** `https://crm.<DOMINIO>/webhooks/meta/` (con la barra final).
3. **Verify token:** el mismo valor de `META_WEBHOOK_VERIFY_TOKEN`, copiado del almacén.
4. **Verify and save.** Meta envía `GET …?hub.mode=subscribe&hub.verify_token=…&hub.challenge=…`; el CRM compara el token en tiempo constante y devuelve solo el `hub.challenge` (letras y dígitos, hasta 128). Si Meta no guarda la URL, ver el paso 10.
5. En **Webhook fields**, **Subscribe** al campo **`messages`** (mensajes entrantes y estados de los enviados). Los demás campos no hacen falta para esta prueba: el CRM los guarda como `other` y no los procesa todavía.
6. **Registro del proxy:** buscar la línea de ese GET en el registro de acceso. Si la query aparece entera (con `hub.verify_token`), el proxy no cumple el requisito del paso 1: **rotar el verify token** (paso 9.4) después de corregir el proxy.

Meta también admite mTLS para los webhooks; el CRM no lo usa (fuera de alcance).

## 6. Llamadas manuales a la Graph API

El CRM **no** suscribe la app al WABA ni registra el número: al conectar (paso 7) solo hace un `GET /<WABA_ID>/phone_numbers` para comprobar el token. Estas llamadas las hace el propietario una vez, desde su propio ordenador, con el token del usuario del sistema.

Para que el token no quede en el historial ni en la lista de procesos:

```bash
read -rs META_TOKEN    # pegar el token y pulsar Intro: no se muestra
auth() { printf 'Authorization: Bearer %s\n' "$META_TOKEN"; }
G=https://graph.facebook.com/v23.0
```

(`curl -H @<(auth)` lee la cabecera de un descriptor, no de la línea de órdenes; funciona en bash y zsh.)

### 6.1 Suscribir la app al WABA (`subscribed_apps`)

```bash
curl -s -X POST -H @<(auth) "$G/<WABA_ID>/subscribed_apps"
# Esperado: {"success":true}
```

Sin cuerpo: el CRM usa la Callback URL de la app (paso 5), no `override_callback_uri`. Comprobarlo:

```bash
curl -s -G -H @<(auth) "$G/<WABA_ID>/subscribed_apps" -d 'fields=id,name'
# Esperado: data[] con la app (whatsapp_business_api_data.id y name). Vacío: la suscripción no quedó.
```

Errores: 401 token no válido; 403 el usuario del sistema no tiene la app o el WABA (paso 3.1); 404 el WABA no existe o no es de esa cartera.

### 6.2 Registrar el número (solo el número real, no el de prueba)

**No se ejecuta con el número de prueba** (Meta ya lo registró). Queda escrito para cuando el propietario firme el paso al número real (y no aplica a un número en coexistencia con la app WhatsApp Business: eso es ADR-032 y F7-13).

Antes: el número está añadido al WABA, su propiedad verificada en WhatsApp Manager y su **nombre visible aprobado** (`name_status` `APPROVED` o `AVAILABLE_WITHOUT_REVIEW`). El registro **solo se hace por API**.

```bash
read -rs META_PIN    # 6 dígitos: si el número ya tiene verificación en dos pasos, el PIN existente
curl -s -X POST -H @<(auth) -H 'Content-Type: application/json' \
  --data @<(printf '{"messaging_product":"whatsapp","pin":"%s"}' "$META_PIN") \
  "$G/<PHONE_NUMBER_ID>/register"
# Esperado: {"success":true}
```

- **Tope de Meta:** 10 registros por número en 72 h; al pasarlo, error `133016` y el número queda bloqueado 72 h. No repetir a ciegas.
- El PIN queda como PIN de verificación en dos pasos: guardarlo en el gestor de contraseñas. Si se olvidó, Meta permite fijar otro por API o desactivarlo desde WhatsApp Manager (verificar en Meta).
- Sin `data_localization_region` (almacenamiento local): no está decidido.

### 6.3 Leer el número (lo mismo que hará el CRM)

```bash
curl -s -G -H @<(auth) "$G/<WABA_ID>/phone_numbers" -d 'fields=id,display_phone_number,quality_rating,messaging_limit_tier'
# Debe aparecer un elemento con "id" igual a <PHONE_NUMBER_ID>. El CRM solo mira la primera página (100).
```

Al terminar: `unset META_TOKEN META_PIN`, y cerrar la terminal.

## 7. Conectar la cuenta en `/canales`

1. Entrar como **Owner** (o un rol con `channels.manage`) en `https://crm.<DOMINIO>/o/<SLUG>/canales`. Hace falta un **segundo factor activo**: conectar y rotar piden MFA reciente (D-F7-11; el código vale 15 minutos). Un Owner sin segundo factor lo activa primero en su perfil.
2. **Conectar WhatsApp** y rellenar:

   | Campo                                | Qué es                                                             | De dónde sale                                              |
   | ------------------------------------ | ------------------------------------------------------------------ | ---------------------------------------------------------- |
   | Nombre de la cuenta                  | Un nombre interno (hasta 100 caracteres), p. ej. «WhatsApp prueba» | Lo eliges                                                  |
   | ID de la cuenta de WhatsApp Business | `WABA_ID`, solo cifras                                             | API Setup o WhatsApp Manager                               |
   | ID del número de teléfono            | `phone_number_id`, solo cifras; **no** es el número de teléfono    | API Setup o el paso 6.3                                    |
   | Token de acceso                      | El del usuario del sistema (paso 3.1)                              | El gestor de contraseñas; se pega y no se vuelve a mostrar |

3. **Conectar** y confirmar el código de segundo factor si lo pide. El CRM hace **una** consulta a Meta (`GET /<WABA_ID>/phone_numbers`): si el token abre ese número en ese WABA, guarda la cuenta **Pendiente** con el token cifrado; si no, «Meta no aceptó ese token…» (400) sin guardar nada.
4. Resultado esperado: la cuenta aparece **Pendiente**, con el checklist «Token válido», «Falta recibir el primer mensaje de Meta» y la calidad de la primera comprobación.

Errores de la pantalla: «El canal de WhatsApp no está habilitado en este despliegue» (409: `META_ENABLED` apagado en `web`); «Meta no responde ahora» (503: red o Meta; reintentar); «Demasiados intentos» (429: 5 cada 10 minutos por persona); «No se puede registrar ese número» (400: `phone_number_id` mal formado o ya conectado en otra cuenta viva, mismo mensaje a propósito).

## 8. Prueba de humo

Con una persona que tenga uno de los **teléfonos destinatarios autorizados** del paso 2:

1. **Entrante.** Desde ese teléfono, escribir «hola prueba» al número de prueba.
2. **Webhook visto.** En `/o/<SLUG>/canales`, recargar: la cuenta pasa a **Conectada** y el checklist muestra «Mensajes de Meta recibidos». (El primer evento firmado enrutado a la cuenta quita `WEBHOOK_NOT_SEEN`; con la comprobación de salud hecha al conectar, pasa a `CONNECTED`.)
3. **Inbox.** En `/o/<SLUG>/inbox` aparece la conversación con el mensaje, sin actualizar la página si `INBOX_WS=on`.
4. **Respuesta.** Responder desde el Inbox antes de 24 h. El teléfono la recibe. En la burbuja, el estado avanza **Enviado → Entregado → Leído** (al abrir el chat en el teléfono). Si queda «No se pudo enviar», ver el motivo y el paso 10.
5. **Baja y alta.** Desde el teléfono, «STOP» (o «BAJA», «CANCELAR»: las de la organización): llega la confirmación fija y el registro `meta.webhook.consent_keyword` dice que cambió algo (la baja aún no tiene pantalla en la ficha del cliente). Después «ALTA» la restituye. Una respuesta dentro de la ventana sigue permitida tras una baja; un seguimiento no (`CONSENT_REVOKED`).
6. **Reinicio.** Reiniciar `web` y `worker` mientras el teléfono envía dos mensajes seguidos: los dos llegan una sola vez (Meta reintenta lo que no recibe con 200; la ingesta deduplica por `wa:msg:<id>`).
7. **Registros** (JSON, sin teléfonos ni textos): `meta.webhook.received` con `items` y `new`; ninguno de `meta.webhook.invalid`, `meta.webhook.rejected` ni `meta.webhook.store_failed`.
8. **Mensajes sin teléfono (BSUID).** Con F7-14 ([#749](https://github.com/PapiCuche/crm-gooddoggy/issues/749)) desplegado, un entrante sin `from` queda `IGNORED` con `NO_PHONE` (antes, `BAD_PHONE`). Contarlos, en el servidor, con el rol `crm_migrator` (`webhook_ingress` está cerrada a `crm_app`; nunca desde los procesos de la aplicación):

   ```sql
   SELECT routed_organization_id, date_trunc('day', received_at) AS dia, count(*)
   FROM webhook_ingress WHERE ignored_reason = 'NO_PHONE'
   GROUP BY 1, 2 ORDER BY 2 DESC;
   ```

   Con el número de prueba lo esperado es 0. Se puede contar también por el registro `meta.webhook.no_phone`. La tabla se purga a los 90 días.

La prueba la firma el propietario (casilla en [#763](https://github.com/PapiCuche/crm-gooddoggy/issues/763)). Sin esa firma no se pasa al número real.

## 9. Marcha atrás y rotaciones

### 9.1 Apagar el canal (rápido y reversible)

1. `META_ENABLED=false` en el almacén y redeploy de los cinco procesos.
2. Efecto: `/webhooks/meta/` da **404**; conectar y rotar dan 409; los envíos fallan con `CHANNEL_UNAVAILABLE`; la comprobación de salud no hace nada. Nada se borra.
3. Meta reintenta durante **7 días** lo que no recibe con 200 y después lo descarta, y puede desactivar un webhook que falla mucho (verificar en Meta el umbral). Para pararlo del todo: `curl -s -X DELETE -H @<(auth) "$G/<WABA_ID>/subscribed_apps"` (efecto inmediato según Meta). Volver a encender es repetir 4.2 y 6.1.

### 9.2 Desconectar la cuenta

En `/canales`, **Desconectar** (sin MFA): la cuenta queda `DISCONNECTED`, su credencial se invalida y el número se libera. Reconectar es crear una cuenta nueva (paso 7). No cambia nada en Meta: si además hay que cortar en Meta, 9.1 punto 3 y, para el token, 9.3.

### 9.3 Rotar el token del usuario del sistema

1. En Meta, generar un token nuevo para el mismo usuario del sistema (paso 3.1, punto 4).
2. En `/canales`, **Rotar token** (con MFA) y pegar el nuevo. El anterior deja de valer en el CRM.
3. En Meta, **revocar** el token anterior (Business settings > System Users > el usuario > revocar tokens: verificar en Meta el nombre exacto).
4. Comprobar: el checklist dice «Token válido» y un envío de prueba sale.

### 9.4 Rotar el app secret o el verify token

**App secret** (afecta a todas las organizaciones a la vez; avisar a quien opera el despliegue antes). Orden de ADR-029:

1. **Basic settings > App secret > Reset** en Meta. La documentación no dice que el anterior siga valiendo: suponer que deja de valer al instante.
2. `META_APP_SECRETS=<nuevo>,<anterior>` en el almacén (el nuevo primero) y redeploy. Hasta que acabe, Meta firma con el nuevo y las instancias viejas responden 401: Meta reintenta y la ingesta es idempotente, así que no se pierde nada si el redeploy termina pronto.
3. Comprobar que entran eventos (`meta.webhook.received`).
4. `META_APP_SECRETS=<nuevo>` y redeploy.

**Verify token:** generar uno nuevo (4.1), ponerlo en el almacén, redeploy, y en Meta cambiar el «Verify token» y **Verify and save** (paso 5). Los eventos no dependen de él: solo la verificación.

## 10. Problemas frecuentes

| Síntoma                                                                                   | Causa probable                                                                                                                                                                                                                           | Qué hacer                                                                                                                                                 |
| ----------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Meta no guarda la Callback URL                                                            | `META_ENABLED` apagado (404); verify token distinto (403); certificado no válido o autofirmado; la URL sin la barra final o con otro dominio; el proxy no llega a Django                                                                 | Paso 4.2 punto 3; comparar el token copiándolo de nuevo del almacén; revisar el certificado con un navegador                                              |
| 404 en `/webhooks/meta/`                                                                  | `META_ENABLED=false` en el proceso `web` (la ruta no existe a propósito)                                                                                                                                                                 | Comprobar el entorno de `web` y redeploy                                                                                                                  |
| Muchos 401 en el registro del proxy                                                       | **Firma inválida:** `META_APP_SECRETS` no tiene el app secret vigente (rotado en Meta sin redeploy, o el de otra app), o algo entre Meta y Django cambia el cuerpo                                                                       | Comparar con **Basic settings**; paso 9.4. Si nadie rotó nada y siguen, puede ser alguien probando la URL: la firma los frena; avisar si es mucho volumen |
| 413                                                                                       | Cuerpo de más de 1 MiB (tope de la vista) o de más de 2 MB (proxy)                                                                                                                                                                       | Raro con mensajes de texto; anotar y abrir un issue                                                                                                       |
| `meta.webhook.invalid` con `reason=items`                                                 | Meta envió un lote de más de 500 ítems (Meta dice que agrupa hasta 1000): se responde 200 y **no se guarda**                                                                                                                             | Con un número de prueba no debería pasar; si pasa, abrir un issue (ver «Riesgos» del PR de F7-12a)                                                        |
| La cuenta sigue «Falta recibir el primer mensaje de Meta»                                 | Falta `subscribed_apps` (6.1) o el campo `messages` (5.5); el teléfono no es destinatario autorizado; app en modo desarrollo sin acceso a eventos reales (verificar en Meta); el WABA del evento no es el de la cuenta (`WABA_MISMATCH`) | 6.1 con el GET de comprobación; revisar destinatarios en API Setup; revisar el WABA en `/canales`                                                         |
| «Meta no aceptó ese token…» al conectar                                                   | Token de otro usuario, sin el WABA asignado, sin permisos, caducado o temporal de API Setup; WABA o `phone_number_id` cambiados de sitio                                                                                                 | Paso 3.1; 6.3 con el mismo token: si ahí no aparece el número, el CRM tampoco lo verá                                                                     |
| Token inválido después de funcionar (checklist «Falta un token válido», cuenta con error) | Token revocado o caducado, o el usuario del sistema perdió el activo                                                                                                                                                                     | 9.3                                                                                                                                                       |
| «No se pudo enviar» / «Pasaron más de 24 horas desde el último mensaje del cliente»       | **Ventana de 24 h cerrada** (`WINDOW_CLOSED`): solo se responde texto libre dentro de las 24 h desde el último mensaje del cliente; fuera hacen falta plantillas (F7-10, aún no)                                                         | Pedir al cliente de prueba que escriba de nuevo; nunca forzar                                                                                             |
| «El cliente se dio de baja…» (`CONSENT_REVOKED`)                                          | El cliente escribió una palabra de baja (`STOP`, `BAJA`, `CANCELAR`): los mensajes proactivos quedan bloqueados                                                                                                                          | Respetarlo. Una baja por palabra se levanta solo si el cliente escribe la palabra de alta (`ALTA`); no hay pantalla para levantarla a mano todavía        |
| Un estado no avanza de «Enviado»                                                          | El teléfono no recibió o no abrió el mensaje; o el estado llegó antes que la confirmación del envío y se reintenta unos minutos                                                                                                          | Esperar unos minutos; si no, revisar los registros del `worker`                                                                                           |
| El Inbox no se actualiza solo                                                             | La imagen del frontend se construyó con `INBOX_WS=off`, el servicio `ws` no corre, o el origen no está en `APP_ORIGIN`/`DJANGO_CSRF_TRUSTED_ORIGINS`                                                                                     | Paso 4.3                                                                                                                                                  |
| Error `133016` al registrar                                                               | 10 registros en 72 h                                                                                                                                                                                                                     | Esperar 72 h; no repetir                                                                                                                                  |
| Meta desactivó el webhook                                                                 | Demasiados fallos o lentitud (verificar en Meta el aviso y el umbral)                                                                                                                                                                    | Corregir la causa (registros del proxy y `meta.webhook.store_failed`), volver a **Verify and save** en el paso 5 y repetir el GET de 6.1                  |

Si algo no está en esta tabla: apagar (9.1), anotar la hora y el síntoma **sin** ids ni secretos en el issue de F7-12b, y no improvisar cambios en Meta.
