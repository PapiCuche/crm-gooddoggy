# Runbook: stack local en limpio (F1-10)

**Relacionado:** ADR-002 §1.1, ADR-003 §1, ADR-012, `infra/docker/compose.yaml`, `Makefile`
**Probado:** 2026-09-30, Docker 29.5.3 (Docker Desktop, macOS arm64)

## Requisitos

- Docker con Compose v2.
- Para `make check`: uv 0.12.19, pnpm 12.8.1 (Node 24.21.0) y gitleaks 8.30.1 en el `PATH`, las mismas versiones que CI (ADR-012). Opcional: `pre-commit install`.

## 1. Configuración

```bash
cp infra/env/.env.example infra/env/.env
```

En `infra/env/.env`:

- **Contraseñas:** `POSTGRES_PASSWORD`, `CRM_MIGRATOR_PASSWORD` y `CRM_APP_PASSWORD`. Usa valores propios, nunca credenciales reales.
- **Garage:** genera `GARAGE_RPC_SECRET` (`openssl rand -hex 32`), `STORAGE_ACCESS_KEY_ID` (`GK` + `openssl rand -hex 16`) y `STORAGE_SECRET_ACCESS_KEY` (`openssl rand -hex 32`). Garage no arranca con valores de ejemplo.
- **Abre la app en `localhost`, nunca en `127.0.0.1`:** el almacén (Garage) se publica para el navegador en `127.0.0.1` (`STORAGE_PUBLIC_ENDPOINT_URL`); si la app también estuviera en `127.0.0.1`, el almacén recibiría su cookie de sesión (las cookies no distinguen puertos).
- **Puertos ocupados:** si 5432, 6379, 3900, 8080, 8025 o 1025 ya están en uso en tu máquina, define `POSTGRES_HOST_PORT`, `REDIS_HOST_PORT`, `GARAGE_HOST_PORT`, `PROXY_HOST_PORT`, `MAILPIT_UI_HOST_PORT` o `MAILPIT_SMTP_HOST_PORT`. Todos se publican solo en 127.0.0.1.

## 2. Arranque

```bash
make up        # docker compose … up -d --build --wait
```

Orden de arranque:

1. **postgres** inicializa los roles y las BD `crm` y `crm_test` (solo con el volumen vacío).
2. **migrate** aplica las migraciones como `crm_migrator`, con solo `DATABASE_MIGRATOR_URL` (`config.settings.migrate`).
3. **backend**, **ws**, **worker**, **worker-media** y **beat** arrancan como `crm_app`, con solo `DATABASE_URL` (compose no les pasa `DATABASE_MIGRATOR_URL` ni `CRM_MIGRATOR_PASSWORD`) y `ENFORCE_RUNTIME_DB_ROLE=true`. Si el rol es superusuario, tiene BYPASSRLS o es propietario, web y ws se niegan a arrancar y el worker rechaza cada conexión. Beat no abre conexiones a la BD. El rechazo de las variables del migrador al arrancar solo existe en `config.settings.production`.
4. **frontend** (Next.js standalone) y **proxy** (Caddy).

**worker-media** (F7-09a1c) atiende solo la cola `media` (`CELERY_TASK_ROUTES`): las descargas de los medios entrantes de WhatsApp, una a la vez (`--concurrency 1`), para que una descarga lenta (hasta 120 s) no ocupe al **worker** de la salida de mensajes. Usa la misma imagen y las mismas variables que **worker**, sin nada más. Sin él, los adjuntos se quedan `PENDING` hasta que arranque: `docker compose -f infra/docker/compose.yaml --env-file infra/env/.env up -d worker-media`; su registro, con `logs -f worker-media`.

Aplicación: **http://localhost:8080**, same-origin:

| Ruta | Destino |
|---|---|
| `/api/*`, `/webhooks/*` | backend |
| `/ws/*` | ws |
| `/health/*` | 404 (sondas internas, no se publican) |
| resto | frontend |

## 3. Verificación

```bash
curl -s http://localhost:8080/status | grep -o Operativo # salud del backend vista por el frontend
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8080/api/schema/   # 200: contrato OpenAPI
make check                                               # las verificaciones de CI (ver Makefile)
```

Dirección del cliente (F2-03D): tras un login, la fila de `platform_audit_logs` lleva la puerta de enlace de la red del stack (por defecto `172.31.250.128`), nunca `STACK_PROXY_IP`, y una cabecera `X-Forwarded-For` enviada con `curl` no la cambia.

`make check` ejecuta en local los checks de CI del backend, el frontend, `.github/scripts`, `docker compose config` y gitleaks.

- Construye las imágenes de backend y frontend, comprueba el usuario no root y lanza la misma prueba de humo que CI (`infra/docker/smoke-image.sh`: `/health/live` y `/o/ci`). El contenedor usa un puerto libre de 127.0.0.1 y se elimina siempre.
- La suite del backend corre contra el PostgreSQL 18.6 y el Garage v2.4.1 del compose, sobre la BD **`crm_test`** (los tests limpian sus tablas y nunca tocan `crm`).
- Las migraciones se aplican como `crm_migrator` y los tests corren como `crm_app`.

## 4. Operación

| Tarea | Comando |
|---|---|
| Logs | `make logs` (JSON redactado, ADR-011) |
| Parar | `make down` |
| Reinicio en limpio (borra BD, Redis y storage locales) | `docker compose -f infra/docker/compose.yaml --env-file infra/env/.env down -v`, y después `make up` |
| Solo los servicios base | `docker compose … up -d postgres redis garage mailpit` |

### Crear una organización de prueba

El stack arranca sin organizaciones. Para crear una con su Owner (F2-06):

```bash
docker compose -f infra/docker/compose.yaml --env-file infra/env/.env exec backend \
  python manage.py bootstrap_organization --slug demo --name "Organización de prueba" \
  --owner-email owner@example.com --reason "entorno local"
```

Pide la contraseña del Owner dos veces y no la muestra. Debe cumplir la política vigente (12 caracteres como mínimo, no común, no solo números). Sin terminal, añade `-T` a `exec` y pasa la contraseña por la entrada estándar con `--password-stdin`. Si el email ya es un usuario, indícalo con `--existing-owner`.

## 5. Problemas frecuentes

- **`ports are not available`:** el puerto del host está ocupado. Define el `*_HOST_PORT` correspondiente.
- **`Pool overlaps with other one on this address space`:** la subred del stack choca con otra red de Docker. Define `STACK_SUBNET`, `STACK_IP_RANGE` y `STACK_PROXY_IP` en `infra/env/.env`.
- **`network crm-blackdog_default has active endpoints`** (tras actualizar a F2-03D, o al cambiar entre ramas de antes y de después): la red del stack cambió de subred y Compose no puede recrearla mientras queden contenedores conectados. Se ha visto en `make check`, que solo levanta `postgres` y `garage`: los deja parados y falla. `make down` y después `make up`; los volúmenes se conservan. Hasta entonces, el stack que sigue en marcha no tiene base de datos.
- **`crm_test` no existe:** el volumen de PostgreSQL es anterior a F1-10. Reinicia en limpio (`down -v`).
- **Garage `Invalid RPC secret key`:** `GARAGE_RPC_SECRET` debe tener 64 caracteres hex.

## 6. Probar el envío de imágenes y PDF (F7-15b)

Cada imagen o PDF que sube una persona pasa por el antivirus (**clamd**, §7) **antes** de guardarse: solo un archivo limpio queda en cola y sale; uno infectado se rechaza y uno que no se pudo revisar no se guarda. No hay ningún interruptor que encender. El canal Sandbox no sale a ningún proveedor: un medio enviado queda `DELIVERED` al instante. Necesitas una cuenta con `channels.manage`, `contacts.view`, `conversations.view` y `messages.send` (el Owner las tiene).

1. `make up` y espera a que `clamd` esté sano (`ps clamd`, §7; la primera vez tarda ~1 min).
2. Entra en **http://localhost:8080** y abre un cliente en `/o/<slug>/clientes`: el id del contacto es el último tramo de la URL.
3. Simula que ese cliente escribe (abre la conversación y su ventana de 24 h). En la consola del navegador, en la misma pestaña:

   ```js
   const csrf = document.cookie.match(/csrftoken=([^;]+)/)[1];
   await fetch("/api/v1/o/<slug>/channels/sandbox/inbound/", {
     method: "POST",
     headers: { "Content-Type": "application/json", "X-CSRFToken": csrf },
     body: JSON.stringify({ contact_id: "<id del contacto>", external_message_id: "prueba-medios-1", text: "Hola, ¿me mandan la cotización?" }),
   }).then((r) => r.json());
   ```

4. Abre `/o/<slug>/inbox`, la conversación del cliente, y pulsa el clip («Adjuntar imagen o PDF»). Elige un JPEG o PNG: se ve su nombre y tamaño, y el cuadro pasa a decir «Pie de foto (opcional)». Escribe un pie y pulsa «Enviar». Repite con un PDF, sin pie.
   - **Esperado:** dos burbujas a la derecha, «Imagen» con el pie y «Documento», que pasan de «En cola» a «Entregado» (pulsa «Actualizar mensajes» si no cambia sola).
5. **Un archivo infectado.** Crea un PDF de prueba con el archivo EICAR dentro (no es un virus real; todos los antivirus lo reconocen):

   ```bash
   printf '%%PDF-1.4\n1 0 obj\n<< /Length 68 >>\nstream\nX5O!P%%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*\nendstream\nendobj\n%%%%EOF\n' > eicar-prueba.pdf
   ```

   Adjúntalo como en el paso 4 y pulsa «Enviar». Si tu antivirus del equipo lo borra al crearlo, créalo en una carpeta que no vigile.
   - **Esperado:** no aparece ningún mensaje nuevo; bajo el cuadro, en rojo, «El antivirus encontró un virus en este archivo, así que no se envió. Elige otro archivo.», el adjunto se quita y no hay «Reintentar». Queda en la auditoría (`message.media_infected`, con la firma y sin el contenido) y en el registro del backend la alerta `media.alert.infected`. No se guarda ningún archivo.
   - Un archivo que pasa los topes del antivirus (por ejemplo, un PDF con demasiados niveles anidados) da en cambio «Este archivo es demasiado complejo para que el antivirus lo revise, así que no se envió. Elige otro archivo.» (`422 MEDIA_TOO_COMPLEX`, F7-15c): no es un virus, pero tampoco sale. Queda en la auditoría como `message.media_too_complex` (con la firma `Heuristics.Limits.Exceeded…`) y en el registro del backend como `media.alert.too_complex`.
6. **Sin antivirus.** Para clamd (`docker compose -f infra/docker/compose.yaml --env-file infra/env/.env stop clamd`), adjunta una imagen y pulsa «Enviar».
   - **Esperado:** «No se pudo revisar el archivo con el antivirus, así que no se envió. Vuelve a intentarlo en unos minutos.»; el adjunto se conserva y el botón dice «Reintentar». No se guarda nada.
   - Arranca clamd otra vez (`… start clamd`), espera a que esté sano y pulsa «Reintentar»: ahora sale.
7. Un GIF o una imagen de más de 5 MB (un PDF de más de 10 MB) no se adjunta y se explica; no se sube nada.

## 7. Abrir los medios entrantes: el antivirus (F7-15)

Un medio que llega por WhatsApp (imagen, audio, video o sticker) se descarga y queda en espera hasta que el antivirus **ClamAV** lo revisa. Solo un archivo limpio se puede descargar desde el chat; uno infectado o que no se pudo revisar nunca se sirve.

**Qué arranca.** `make up` levanta también el servicio **clamd** (imagen `clamav/clamav:1.5.4-debian`). No publica puertos al host y vive en su propia red `av`, interna (sin salida), donde solo está además **clamd-relay** (`alpine/socat`, F7-15c). **worker-media** (lo recibido) y **backend** (lo que se envía, §6) llegan a clamd solo por ese relay, en `clamd-relay:3310`, desde otra red interna, `av-client`. Así, un clamd comprometido por un archivo no alcanza ningún listener HTTP, tampoco `backend:8000`. Su configuración es `infra/docker/clamav/clamd.conf`. Trae firmas en la imagen; el servicio **freshclam** (otro contenedor, el único con salida) las actualiza en el volumen `clamavdata` y clamd las recarga solo. Cargarlas tarda alrededor de un minuto y ocupa ~1 GiB de memoria; `make up` espera a que esté sano.

```bash
docker compose -f infra/docker/compose.yaml --env-file infra/env/.env ps clamd clamd-relay   # clamd «healthy»
# F7-15c: desde clamd, el backend no responde (ni por nombre)
docker compose -f infra/docker/compose.yaml --env-file infra/env/.env exec clamd \
  bash -c 'timeout 3 bash -c "</dev/tcp/backend/8000" 2>/dev/null && echo ALCANZABLE || echo "sin respuesta"'
# sin respuesta
docker compose -f infra/docker/compose.yaml --env-file infra/env/.env logs -f worker-media | grep media.scan
```

**Probarlo con un medio real.** Necesitas una cuenta de WhatsApp conectada (runbook [Activación de WhatsApp](whatsapp-activation.md)) y `conversations.view` y `contacts.view` (el Owner las tiene).

1. Desde un teléfono, envía al número del negocio una **imagen**, un **audio** (nota de voz) y un **sticker**.
2. Abre `/o/<slug>/inbox` y la conversación de ese teléfono. Cada burbuja muestra primero «Recibiendo el archivo del canal…» y luego «Archivo recibido. Todavía no se puede abrir…» mientras el antivirus lo revisa.
3. **Esperado:** en unos segundos (pulsa «Actualizar mensajes» si no cambia sola) cada burbuja muestra **«Descargar»** con el tipo y el tamaño. Al pulsarlo, el archivo se descarga en otra pestaña.
4. En el registro de **worker-media** aparece una línea `media.scan` por archivo, con `outcome: CLEAN`.

**Los medios que llegaron antes de F7-15.** Siguen en espera hasta que el barredor (cada minuto) los encuentra: cualquier archivo sin revisar desde hace más de 15 minutos se vuelve a mandar al antivirus. Tras reconstruir el stack, en unos 15 minutos los medios anteriores se pueden abrir sin hacer nada.

**Qué ves si algo va mal.**

| Burbuja | Qué significa | Qué hacer |
|---|---|---|
| «El antivirus encontró un virus en este archivo…» | `INFECTED`: queda en la auditoría (`file.scan_infected`, con la firma y sin el contenido) y en el registro como alerta `media.alert.infected` | Nada que abrir. No pidas reenviarlo |
| «No se pudo revisar este archivo con el antivirus…» | `ERROR`: clamd no respondió en 4 intentos (30 s, 2 min y 10 min de espera) | Comprueba `ps clamd`. El barredor lo vuelve a escanear solo cada 2 h mientras el archivo tenga menos de 6 h (auditoría `file.scan_retried`): si clamd volvió, se abre sin hacer nada. Si sigue así pasadas 6 h, pide al cliente que lo reenvíe |
| Se queda en «Todavía no se puede abrir…» | El antivirus aún no lo revisó (clamd arrancando o la cola `media` ocupada) | Espera; si pasa de 15 min, mira `logs worker-media` |

**Comprobar que detecta un virus.** El archivo de prueba EICAR no es una imagen ni un audio, así que WhatsApp no lo entrega como medio. Para ver que clamd lo detecta (no guarda nada):

```bash
printf 'X5O!P%%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*' \
  | docker compose -f infra/docker/compose.yaml --env-file infra/env/.env exec -T clamd clamdscan --no-summary -
# stream: Eicar-Test-Signature FOUND (y sale con código 1)
```

El camino completo (EICAR queda `INFECTED`, se audita y nunca se descarga; clamd caído reintenta y acaba en `ERROR`) lo cubren `backend/tests/test_media_scan.py` y `backend/tests/test_antivirus.py`, este último también contra un clamd real en CI.

## 8. Instagram, Messenger y WhatsApp contra el simulador de Meta (F7b-06c)

Para probar conectar, recibir y responder sin cuentas de Meta ([ADR-041](../adr/ADR-041-local-meta-graph-simulator.md)). El servicio `meta-sim` sirve la Graph API falsa de `backend/tests/meta_sim` (fuera de toda imagen) y el backend le manda las llamadas a Graph **solo** con los settings `local`. Todos los valores son ficticios: **nunca pegues un token real** en un stack con el simulador encendido, y **no lo enciendas contra una base que ya tenga cuentas reales** de una página o de WhatsApp: sus tokens irían por http al simulador, que guarda en memoria las cabeceras de WhatsApp. No pongas `META_GRAPH_SIMULATOR_URL` sin el perfil `meta-sim`: el nombre `meta-sim` lo resolvería el resolvedor de tu red (con sus dominios de búsqueda).

**1. Configuración.** En `infra/env/.env`, los valores de ejemplo del bloque «Simulador local de Meta» de `.env.example`:

```bash
META_ENABLED=true
META_APP_SECRETS=metasimdevappsecretnotreal0000
META_WEBHOOK_VERIFY_TOKEN=meta-sim-dev-verify-token-not-real
META_APP_ID=100000000000301
META_GRAPH_SIMULATOR_URL=http://meta-sim:8090
```

**2. Arranque.** Con el perfil `meta-sim` (sin él, el servicio no arranca y Graph sale a Meta como siempre):

```bash
COMPOSE_PROFILES=meta-sim make up
curl -s http://127.0.0.1:8090/_sim/state   # {"subscribed": {}, "page_sent": [], "whatsapp_sent": []}
```

Si el puerto 8090 está ocupado, define `META_SIM_HOST_PORT`. Para apagarlo, quita `META_GRAPH_SIMULATOR_URL` del `.env` y vuelve a `make up`.

**3. Conectar una cuenta.** Los datos que acepta el simulador:

| Campo | Valor |
|---|---|
| Página (Messenger, y la de Instagram) | `100000000000101` |
| Cuenta de Instagram | `100000000000102` |
| Token | `meta-sim-test-access-token-not-real` |
| WhatsApp: WABA y `phone_number_id` | `100000000000001` y `100000000000002` |

Las tres cuentas se conectan desde **Canales** («Conectar WhatsApp», «Conectar Messenger», «Conectar Instagram», con el segundo factor del Owner). También desde el shell del backend, con un Owner de la organización (p. ej. la de pruebas de §4):

```bash
docker compose -f infra/docker/compose.yaml --env-file infra/env/.env exec -T backend python manage.py shell -c "
from apps.accounts.models import User
from apps.conversations import accounts
from apps.organizations.models import Organization
from core.tenancy.context import ActorType, TenantContext
from core.tenancy.scope import tenant_scope
org, owner = Organization.objects.get(slug='pruebas'), User.objects.get(email='owner@pruebas.gooddoggy.test')
with tenant_scope(TenantContext(org.pk, 'meta-sim', owner.pk, ActorType.USER, owner.pk)) as ctx:
    print(accounts.connect_social(ctx, platform='INSTAGRAM', name='Instagram simulado', page_id='100000000000101',
          instagram_account_id='100000000000102', access_token='meta-sim-test-access-token-not-real')[0].status)
"
```

Esperado: `PENDING`, y `/_sim/state` muestra la página suscrita.

**4. Recibir un mensaje.** Un webhook firmado con el secreto de ejemplo, por el proxy:

```bash
cd backend && uv run python -m tests.meta_sim.send_webhook instagram "Hola" --url http://localhost:8080/webhooks/meta/
# instagram: HTTP 200      (también: page, whatsapp; --customer <id> cambia el cliente)
```

Cada envío lleva un `mid` nuevo y la hora actual (abre la ventana de 24 h). En unos segundos el mensaje está en el Inbox, la cuenta pasa a **Conectada** y el contacto de Instagram se llama «Cliente Simulado» (el perfil del simulador). Sin `uv` en el equipo, el mismo comando desde el contenedor: `… exec meta-sim python -m tests.meta_sim.send_webhook instagram "Hola" --url http://backend:8000/webhooks/meta/`.

**5. Responder.** Desde el chat del Inbox. El mensaje queda **Enviado** y `/_sim/state` lo muestra en `page_sent` (`whatsapp_sent` para WhatsApp).

**6. Errores de Meta.** El resultado de las próximas llamadas, uno por llamada (los nombres de `ERRORS` en `page_graph.py` y `graph.py`):

```bash
curl -s -X POST http://127.0.0.1:8090/_sim/then -d '{"page": ["window_closed"]}'
```

**Límites.** Los medios entrantes no se descargan: la descarga sigue con la política normal, y el simulador no conoce ningún medio (la consulta falla). Si un día devolviera una URL, el backend intentaría bajarla del `lookaside.fbsbx.com` **real** con el token ficticio, y Meta lo rechazaría; ninguna otra llamada sale a Meta. El simulador guarda todo en memoria: reiniciarlo lo vacía, y las cuentas ya conectadas siguen funcionando porque siempre acepta los mismos ids y el mismo token.
