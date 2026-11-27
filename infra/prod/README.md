# infra/prod

Producción en un servidor Linux con Docker Compose, según [ADR-033](../../docs/adr/ADR-033-production-hosting.md). El primer arranque, el despliegue y los backups tienen sus runbooks: [production-first-boot.md](../../docs/runbooks/production-first-boot.md) (F1-17), F1-15 (#756) y F1-16 (#757). Aquí solo está lo que esos runbooks usan.

| Archivo | Qué es |
| --- | --- |
| `compose.yaml` | Los servicios de ADR-033 §3, sin Garage, Mailpit ni `build:`. Usa imágenes por digest y límites de memoria, y rota los logs |
| `compose.shared.yaml` | Variante de servidor compartido (ADR-046), encima de `compose.yaml`: el `proxy` no publica puertos, monta `Caddyfile.shared` y entra en la red externa `ingress` |
| `Caddyfile.shared` | El proxy interno del modo compartido: HTTP en `:80`, sin ACME, IP del cliente solo del borde público por su IP fija; mismas rutas y topes que `Caddyfile` (`validate.sh`) |
| `host-mode.sh` | Qué archivos de Compose usa este servidor, según `/etc/crm/host-mode` (`dedicated` o `shared`). Lo leen `deploy.sh`, `smoke.sh` y `backup/`. Prueba: `test-host-mode.sh` |
| `Caddyfile` | El borde: HTTPS automático para `crm.<DOMINIO>`, las rutas del stack local, los topes de cuerpo, la tasa por IP en `/webhooks/*` y los registros sin query. `/health/*` responde 404 |
| `caddy/Dockerfile` | Caddy con `caddy-ratelimit` (decisión 10), con sus dos bases fijadas por digest. CI la publica (`images.yml`) |
| `.env.production.example` | Los nombres de las variables, sin valores, en una sección por archivo del servidor |
| `postgres/bootstrap-roles.sh` | Crea `crm_migrator` y `crm_app` (ADR-002 §1), sin `CREATEDB` ni BD de tests. Se puede repetir |
| `validate.sh` | `docker compose config` con valores ficticios. Comprueba que solo el proxy publica puertos y que cada `FROM` y cada `# syntax=` de todos los Dockerfile van por digest. Con `--caddy`, también `caddy validate` (CI: `infra-prod.yml`) |
| `build-images.sh` | Construye y prueba las tres imágenes. Lo usan `images-pr.yml` (en cada PR) e `images.yml` (antes de publicar) |
| `smoke.sh` | La prueba de humo tras `up --wait`, que la llama `deploy.sh` |
| `deploy.sh` | Despliega un `release.env` por digest: pull, volcado previo cifrado, migrate, up, puerta de salud y vuelta atrás automática (abajo) |
| `test-deploy.sh` | Prueba `deploy.sh` con un `docker` falso, sin servidor: orden de los pasos, vuelta atrás, negativas y el lock compartido con el backup nocturno (CI: `infra-prod.yml`) |
| `cd-preflight.sh` | Las condiciones locales que bloquean un despliegue (ADR-048 §6, §7 y §9), de solo lectura (abajo, «Condiciones que bloquean»). Prueba con un servidor simulado: `test-cd-preflight.sh` (CI: `infra-prod.yml`) |
| `crm-migrate.service`, `crm-migrate.timer` | `deploy.sh --migrate` el día 15 de cada mes (abajo, «Migrate mensual») |
| `backup/` | Backups (F1-16): `backup.sh` (volcado cifrado con age y copia de los objetos a un destino que el servidor no puede leer ni borrar), `restore.sh`, `check.sh` (aviso a las 26 h), las unidades de systemd, `lifecycle.json` (retención 7/4/6 en el destino) y `drill.sh` (el simulacro de CI). Runbook: [backup-restore.md](../../docs/runbooks/backup-restore.md) |

## Secretos

Cada sección de la plantilla es un archivo en `CRM_ENV_DIR` (`/etc/crm` por omisión), propiedad de `root` y con modo `0600`. Cada proceso recibe solo el suyo: el runtime, `runtime.env`; `migrate`, `migrate.env`; Redis, `redis.env`; el proxy, `proxy.env`; el backup, `backup.env` (los temporizadores de root). Los digests van en `release.env`, que se pasa con `--env-file` y no lleva secretos.

`postgres.env` solo sirve para el primer arranque del volumen. Cuando `migrate` termina bien, se **borra** el archivo y se recrea `postgres` (`up -d --force-recreate postgres`). Así la contraseña de `crm_migrator` no queda en el entorno del contenedor (ADR-002 §1.1). Para restaurar en un servidor nuevo, `bootstrap-roles.sh` vuelve a crear los roles con contraseñas nuevas (ADR-033 §8.1).

## Red y cortafuegos

- Solo `proxy` tiene `ports:`, y publica 80/tcp y 443/tcp en IPv4 (ADR-033 §4.4). HTTP/3 está apagado. PostgreSQL y Redis están en la red `data` (`internal: true`), y ni el proxy ni el frontend entran en ella.
- Docker escribe sus propias reglas de iptables para los puertos publicados y se salta `ufw`. Por eso la regla es «nada publica salvo el proxy», no «`ufw` lo cierra». Si hace falta filtrar el tráfico que llega a los contenedores, va en la cadena `DOCKER-USER`, no en `ufw`. El cortafuegos del proveedor es la primera capa.
- `postgres/pg_hba.conf` rechaza al superusuario por red: solo entra por el socket local. `smoke.sh` lo comprueba.
- El backend confía en `X-Forwarded-For` solo si viene de la IP fija del proxy (`STACK_PROXY_IP`, la misma expresión en `FORWARDED_ALLOW_IPS`). Caddy no confía en el que manda el cliente.

## Webhooks y registros

- `/webhooks/*` corta los cuerpos de más de 4 MB, y `/api/*` los de más de 11 MB. Con ASGI, Django lee el cuerpo entero antes de la vista, también si llega en `chunked`. `test_stack_proxy.py` compara los topes con `BODY_MAX` y con la subida mayor, y `smoke.sh` los comprueba en el servidor.
- El resto de rutas (`/ws/*` y las del frontend) corta en 1 MB: ni Next ni el upgrade del WebSocket necesitan más.
- Hay un límite de 50 peticiones cada 10 s por IP del cliente en `/webhooks/*`. Al superarlo, Caddy responde 429 y Meta reintenta.
- Los registros de Caddy, los de errores incluidos, sustituyen la query de la URI por `?redacted` y no guardan `Referer`. La query lleva el verify token de Meta y las búsquedas de clientes. El backend arranca sin log de acceso.
- PostgreSQL arranca con `log_min_error_statement=panic`, `log_statement=none`, `log_parameter_max_length_on_error=0`, `log_error_verbosity=terse` y `log_min_messages=log`: ni la sentencia, ni sus parámetros, ni el `DETAIL` ni el texto de un error llegan a su log; el cliente sigue recibiendo el error. `smoke.sh` los lee con `SHOW` como `crm_app`. Su log va a la salida estándar, con la rotación de Docker (5 × 10 MB por contenedor). No está en el volumen de datos, así que no entra en `pg_dump`.
- **Binding en el servidor** (`OPTIONS = {"server_side_binding": True}`): no se activa aquí. Es un cambio del backend que exige la suite completa, y con `log_min_error_statement=panic` el log ya no recibe las sentencias. Queda como mejora opcional.

## Tiempo real del Inbox

La imagen del frontend se construye con `NEXT_PUBLIC_INBOX_WS=on` (F6-09): el Inbox abre `wss://crm.<DOMINIO>/ws/o/<slug>/inbox/` y los mensajes nuevos llegan sin recargar. Lo que lo sostiene:

- **Ruta.** Caddy lleva `/ws/*` a `ws` (uvicorn, `--ws-max-size 16384`), con el upgrade de HTTP/1.1 y tope de 1 MB al cuerpo. `ws` va en las redes `edge` y `data` y entra en la puerta de salud.
- **Origen y sesión.** `ws` solo acepta el `Origin` de `APP_ORIGIN` y `DJANGO_CSRF_TRUSTED_ORIGINS` (`runtime.env`); sin ellos rechaza todo. La sesión sale de la cookie; el permiso y el alcance de `conversations.view` se vuelven a mirar cada minuto y en cada evento. Los eventos solo llevan ids.
- **Límites.** 8 conexiones por persona y proceso, 30 tramas por minuto, 1 KB por trama, cierre a los 90 s sin latido (el cliente manda uno cada 25 s). La capa de canales usa Redis: `CHANNEL_LAYER_REDIS_URL` o, si falta, la URL del broker (`CELERY_BROKER_URL`).
- **Reconexión.** Un despliegue o una recarga de Caddy cortan las conexiones. El cliente vuelve con espera de 1 a 30 s y relee el Inbox una vez. Mientras tanto, el Inbox se sigue leyendo al enfocar la pestaña.
- **Pruebas.** `test_inbox_realtime.py` (servidor), `inbox-socket` y `use-inbox-realtime` (cliente) y el E2E del stack local de F6-07e y F6-09. Ninguna prueba automática cruza el Caddy de producción: tras desplegar, en el Inbox, la petición `/ws/…` debe responder 101.
- **Apagarlo.** Construir la imagen con `--build-arg NEXT_PUBLIC_INBOX_WS=off` y desplegarla. El Inbox funciona igual, pero sin tiempo real.

## Imágenes

`.github/workflows/images.yml` corre en cada push a `main` (ADR-033 §7). Construye el backend, el frontend y el proxy para `linux/amd64`, los arranca, revisa con gitleaks su configuración y su historial de capas, y los publica como `ghcr.io/<owner>/<repo>/{backend,frontend,proxy}:<SHA>` con procedencia y SBOM. El resumen del job trae el `release.env` de ese SHA: las tres imágenes y las de PostgreSQL y Redis, todas por digest. Es el único workflow con `packages: write`, y no tiene más secreto que `GITHUB_TOKEN`.

- **En cada PR** que toca una imagen, `images-pr.yml` hace la misma construcción y las mismas pruebas (`build-images.sh`) sin credenciales ni publicar. No es un check requerido.
- **Lo publicado es lo probado.** `build-images.sh` anota el digest de la configuración de cada imagen probada (sus capas, `ENV`, etiquetas e historial). Tras subirla, `images.yml` lee la configuración del registro y, si no es la misma, falla sin dar `release.env`.
- **Bases por digest.** Las de los tres Dockerfile y su frontend (`# syntax=`) van fijadas por `@sha256:`. `validate.sh` rechaza cualquier `FROM` sin digest. Actualizar una base es cambiar la etiqueta y el digest a la vez (`docker buildx imagetools inspect <imagen>:<etiqueta>`).
- **`REVISION`.** El `release.env` lleva `REVISION=<SHA>`, el commit de las imágenes.

Los paquetes son **públicos** (ADR-033, decisión 8). GitHub crea cada paquete privado la primera vez. El propietario lo cambia a público una sola vez en la configuración del paquete. Mientras no lo haga, el job avisa. Con paquetes privados, se entra antes de desplegar con un token de solo lectura de paquetes: `sudo docker login ghcr.io`.

### Atestaciones de procedencia (F1-20b, ADR-048 §7)

Tras publicar, el job `attest` de `images.yml` firma una atestación de procedencia (SLSA v1, Sigstore) por imagen propia, solo en los push a `main`. Es el único job con `id-token: write` y `attestations: write`, y no hace checkout ni toca, sube o reetiqueta las imágenes. Si falla, las imágenes quedan publicadas **sin** atestación y el verificador bloquea ese release: se corrige con un commit nuevo.

`python3 infra/prod/verify_attestations.py --sha <SHA> release.env` lo comprueba sin credenciales y solo lee. Sale con 0 solo si las tres imágenes tienen una atestación válida de `images.yml` en `refs/heads/main` para ese SHA y ese digest, y su etiqueta `org.opencontainers.image.revision` es el SHA. Cualquier otra cosa sale con 1: una atestación ausente o distinta, un error de red o un 403/429 de la cuota anónima de la API (60 por hora por IP, compartida con la otra aplicación; son de 3 a 9 por ejecución, hasta 3 páginas por imagen, así que el agente lo ejecuta una vez por release). Necesita `gh` **2.103.3** exacto, sin sesión, instalado desde el tarball oficial `gh_2.103.3_linux_amd64.tar.gz` (sha256 `4c806978ffce1cec70853302a56d70ac6da4f19bae7adca4e1a8ab00633dd91d`, con `sha256sum --check`; `bin/gh` en `/usr/local/bin`). No va en ninguna imagen.

**Todavía no se usa en producción.** Este cambio no lo instala ni lo ejecuta en el servidor. Eso llega con el agente (F1-20e1) y solo cuando el propietario habilite la fase 1 (ADR-048, enmienda E1). Hasta entonces rige el GO exacto por SHA.

## Desplegar

En el servidor, con el checkout de `infra/prod` en el mismo commit que las imágenes y sin cambios locales. Siempre dentro de `tmux` (o `screen`), para que una sesión SSH perdida no corte el despliegue:

```bash
tmux new -s deploy
sudo install -m 0600 /dev/stdin /root/release-<SHA>.env   # pega el release.env del resumen del job
sudo infra/prod/deploy.sh /root/release-<SHA>.env
```

`deploy.sh` necesita, en `CRM_ENV_DIR` (`/etc/crm`), los archivos de entorno y `CRM_DOMAIN` en `proxy.env`. Desde el segundo despliegue necesita también el backup de F1-16 configurado, porque el volcado previo lo hace `backup.sh predeploy` (paso 2): `backup.env`, `backup-recipients.txt`, la opcional `backup-deploy-recipients.txt` y `age` y `rclone` instalados ([backup-restore.md](../../docs/runbooks/backup-restore.md)). Se niega a correr, antes de tocar nada:

- sin `root`;
- con cambios sin commit en `infra/prod`;
- con un `REVISION` que no es el commit del checkout de `infra/prod`. `compose.yaml`, `smoke.sh` y `backup/` tienen que ser los de las imágenes: `git checkout <REVISION>`. `--allow-revision-mismatch`, solo como primer argumento, lo permite y lo anota en `deploy.log` como `AVISO`; cualquier otro `--allow…`, en cualquier sitio, se rechaza. Sin checkout de git, solo con ese flag;
- con otro despliegue a medias o el backup nocturno en curso (`/var/lib/crm-deploy/lock`);
- con un `CRM_DOMAIN` que no sea un nombre de dominio;
- con un release (el nuevo o el que está en marcha) que tenga otra variable, una imagen sin `@sha256:<64 hex>`, o un backend, frontend o proxy que no salga de `ghcr.io/papicuche/crm-gooddoggy/`;
- con un cambio de versión mayor de PostgreSQL: eso exige `pg_upgrade` en una ventana.

| Paso | Si falla |
| --- | --- |
| 1. Valida el release y hace `pull` de los cinco digests | Sale. No se ha tocado nada |
| 2. Arranca `postgres` y `redis` con los digests **en marcha**, también si estaban parados, sin recrearlos (`--no-recreate`: un cambio de `compose.yaml` no reinicia la base antes del volcado). Después, el volcado previo de la versión en marcha con `backup/backup.sh predeploy`: `pg_dump` como superusuario por el socket, cifrado al vuelo con `age` para las claves de backup y de despliegue, y subido a `db/predeploy/` del destino de los backups, donde caduca a los 7 días. No queda en el servidor. En el primer despliegue no hay nada que volcar. Nunca se migra sin volcado | Sale. El runtime no se ha tocado |
| 3. Job `migrate` como `crm_migrator`, contra `postgres` y `redis` con los digests **en marcha** (solo el primer despliegue los arranca con los nuevos) | Sale. El runtime anterior sigue en marcha y `release.env` no cambia |
| 4. `up -d --wait` del stack: aquí cambian todos los digests, también los de `postgres` y `redis`. `up` vuelve a lanzar `migrate`, porque el runtime depende de él en `compose.yaml` (así un `up` a mano nunca arranca sin migrar). Ya no queda nada que aplicar: termina en segundos | Vuelta atrás automática |
| 5. Puerta de salud: `/health/ready` 200 desde dentro del backend (hasta `CRM_HEALTH_TIMEOUT`, 180 s), `smoke.sh https://crm.<DOMINIO>` y `https://crm.<DOMINIO>/login` 200 a través de Caddy | Vuelta atrás automática |

Si todo pasa, `release.env` pasa a `release.env.previous` y el nuevo queda como `release.env`. Repetir el mismo release es seguro: no pierde la versión anterior. El registro está en `/var/lib/crm-deploy/deploy.log`: fechas, pasos, digests y el commit de `infra/prod`, nunca valores de los archivos de entorno.

## Volver atrás

- **Automática.** Si fallan los pasos 4 o 5, `deploy.sh` vuelve a levantar los digests de `release.env`, es decir, la versión que estaba en marcha, y pasa la puerta otra vez. Entonces imprime `DESPLIEGUE FALLIDO. ROLLBACK OK` y sale con 1. Si la vuelta atrás tampoco pasa la puerta, imprime `ROLLBACK FALLIDO` y sale con 2: mira `docker compose ps` y los logs, y sigue con la manual.
- **Manual** (una versión desplegada da problemas después): `sudo infra/prod/deploy.sh --rollback` despliega `release.env.previous` con los mismos pasos e intercambia los dos archivos. Antes, `git checkout` del `REVISION` de `release.env.previous`, o `--allow-revision-mismatch --rollback`. Para ir más atrás, despliega el `release.env` de ese SHA (el resumen de su job de `images.yml`).
- **Nunca se deshacen migraciones.** Volver atrás es seguro solo si las migraciones siguen la regla de expandir y después contraer (ADR-033 §7). Una versión que no la cumple se despliega en una ventana. Su vuelta atrás es restaurar el volcado previo de `db/predeploy/`, con la clave de despliegue o la de backup, y se pierde lo escrito desde entonces ([backup-restore.md](../../docs/runbooks/backup-restore.md), «Volver atrás tras un despliegue con migraciones»).

  El orden importa, porque la imagen nueva volvería a migrar la base restaurada. Y depende de cómo terminó `deploy.sh`, porque `release.env` solo rota tras `DESPLIEGUE OK`:
  - **Tras `DESPLIEGUE OK`** (la versión nueva quedó en `release.env` y da problemas después):
    1. `restore.sh --object db/predeploy/crm-….dump.age --replace-existing-data --roles-env …` con `CRM_RELEASE_ENV=/etc/crm/release.env.previous`. Así levanta el backend y `migrate` de la versión anterior.
    2. Después, `sudo infra/prod/deploy.sh --rollback`, que lleva todo el stack a esos digests, pasa la puerta e intercambia los dos archivos. Si ese `--rollback` fallara su puerta, su vuelta atrás automática levanta la versión nueva, cuyo `migrate` vuelve a aplicar sus migraciones sobre la base restaurada: repite el paso 1.
  - **Tras `ROLLBACK OK` o `ROLLBACK FALLIDO`** (`release.env` sigue siendo la versión que estaba en marcha, y `release.env.previous` es la anterior a esa):
    1. `restore.sh --object db/predeploy/crm-….dump.age --replace-existing-data --roles-env …` con `CRM_RELEASE_ENV=/etc/crm/release.env`.
    2. Después, `sudo infra/prod/deploy.sh /etc/crm/release.env`, que vuelve a desplegar esa misma versión, sin intercambiar archivos. No uses `--rollback` aquí: llevaría producción a una versión aún más vieja.
- **Un despliegue cortado** (Ctrl-C, `kill`, un reinicio o un error inesperado) no vuelve atrás. Deja `/var/lib/crm-deploy/lock` y `release.env.next`, y lo anota como `INTERRUMPIDO` en `deploy.log`. Mientras estén, ningún despliegue arranca. Mira `deploy.log` y `docker compose ps`, borra los dos y repite el despliegue o haz `--rollback`: los dos son idempotentes. `deploy.sh` ignora `SIGHUP`, pero una terminal cerrada puede cortar igualmente la salida de `docker compose`: por eso, `tmux`.

## Migrate mensual

Las particiones de auditoría se crean al migrar, para el mes en curso y los 12 siguientes (ADR-033 §7). Sin despliegues en un año, se acabarían. `crm-migrate.timer` lanza `deploy.sh --migrate` el día 15 de cada mes a las 12:00 UTC: solo el job `migrate` de la versión en marcha (`release.env`). Toma antes que nada el lock de los despliegues: si hay un despliegue o el backup nocturno en curso, falla y se repite el mes siguiente. Un fallo avisa: `ExecStopPost` llama a `backup/alert.sh migrate.failed`, que lo deja en el journal y, con `SENTRY_DSN` en `backup.env`, en Sentry. Instalación, con el repositorio en `/opt/crm`:

```bash
sudo install -m 0644 infra/prod/crm-migrate.service infra/prod/crm-migrate.timer /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now crm-migrate.timer
systemctl list-timers crm-migrate.timer   # la próxima ejecución; el resultado, en journalctl -u crm-migrate
```

El mismo `migrate` quita el detalle de la IA de más de 12 meses completos (F8-08k, D-F8-05) cuando `AI_RECORDS_RETENTION` está encendido en `config/settings/base.py` (apagado por omisión). Como guarda el mes UTC entero y el temporizador corre el día 15, en la práctica el detalle dura hasta unos 13,5 meses, y unos 14,5 si se salta una ejecución. Si una tabla está ocupada más de 5 s, avisa en el log (`ai.records_retention_lock_busy`) y lo deja para el siguiente `migrate`.

## Conexiones a PostgreSQL

`backend` y `ws` (uvicorn, `config/asgi.py`) cierran la conexión de cada petición al terminarla (F1-DB-01, #1134): Django atiende cada petición ASGI en un hilo propio y, con conexiones persistentes, la del hilo terminado se quedaba abierta e inactiva hasta agotar `max_connections` (en local, 96 inactivas y el login en 500). Ahora `backend` tiene tantas conexiones como peticiones en curso, y `ws`, una solo mientras consulta la base, no una por socket. Los workers de Celery conservan la suya 60 s: `worker` tiene 2 y `worker-media`, 1. `beat` no se conecta. Con todo en reposo, `crm_app` tiene unas 3 conexiones.

Comprobación de solo lectura, como superusuario y sin cambiar nada:

```bash
sudo infra/prod/compose.sh exec -T postgres psql -U postgres -d crm -Atc \
  "SELECT usename, coalesce(state, '-'), count(*) FROM pg_stat_activity WHERE usename IS NOT NULL GROUP BY 1, 2 ORDER BY 3 DESC"
```

- **Normal:** `crm_app` con menos de 10 conexiones (`idle` incluidas), y en reposo unas 3.
- **Aviso:** más de 20 conexiones `crm_app` `idle` durante 5 minutos. No se explica por el tráfico: busca qué contenedor las tiene (`client_addr` frente a `docker network inspect`) antes de reiniciar nada.
- **Crítico:** 80 conexiones o más en total frente a `max_connections` (100, de las que 3 quedan para el superusuario). PostgreSQL empieza a rechazar conexiones y el login responde 500.

No hay tope duro: con unas 90 peticiones a la vez en `backend` se llegaría al límite. Reiniciar servicios o cambiar `max_connections` en producción solo se hace con el GO del propietario.

## Backup nocturno y despliegues

El backup nocturno (`backup.sh nightly`) toma el mismo lock que `deploy.sh`, `/var/lib/crm-deploy/lock`. Mientras dura, ningún despliegue arranca. Si un despliegue lo tiene, el backup espera hasta una hora (`BACKUP_LOCK_WAIT`). Si sigue puesto, porque un despliegue se cortó y lo dejó, el backup falla con `backup.failed` (`stage=deploy_lock`) y avisa. `backup.sh predeploy` no lo toma: lo lanza `deploy.sh`, que ya lo tiene.

## Condiciones que bloquean (`cd-preflight.sh`)

`sudo infra/prod/cd-preflight.sh` comprueba, sin cambiar nada, las condiciones locales de ADR-048 §7: backup de menos de 26 h, disco (`/` y el directorio de Docker), memoria y swap, carga, producción sana (contenedores `healthy` y `/health/ready` desde el backend, con `docker` a secas: nunca `docker compose`, que cargaría los archivos de entorno), el lock de `/var/lib/crm-deploy`, la marca de parada `cd-halted`, un issue abierto con la etiqueta `incident` o `stop-the-line` (API pública de GitHub, sin credenciales), la ventana de la fase 1 (§6), los feriados nacionales del Perú y el calendario de congelación. Escribe una línea por condición y sale con 0 si todas pasan, 1 si alguna bloquea y 2 si recibe argumentos. Lo que no puede comprobar (un archivo que falta o no se puede leer, GitHub sin respuesta o con el límite de peticiones agotado) **bloquea**. Nunca borra el lock ni la marca de parada, y no escribe en el registro ni en ningún otro sitio.

- **Cuándo se usa:** el operador lo ejecuta a mano, y más adelante el agente del servidor (F1-20e1), **solo cuando el propietario habilite la fase 1** (ADR-048, enmienda E1). Hasta entonces rige el GO exacto por SHA y ningún procedimiento lo pide. Este PR no lo ejecuta en producción.
- **Hora de Lima:** UTC−5 fija, calculada sin la base de zonas horarias (si falta, `TZ=America/Lima` cae en UTC sin avisar). Hoy el Perú no cambia de hora en verano. Si algún día lo hace, hay que actualizar el script.
- **Umbrales y feriados:** en un solo bloque al principio del script, tal como los propone ADR-048 (decisión 6). Se revisan tras los 10 primeros despliegues. La lista de feriados se revisa cada año, porque el Congreso añade feriados por ley. Jueves y Viernes Santo salen del cálculo de la Pascua.
- **`/etc/crm/deploy-freeze`** (lo mantiene el propietario, de `root`, sin escritura de grupo ni otros; no va al repositorio): una fecha `AAAA-MM-DD` o un rango `AAAA-MM-DD..AAAA-MM-DD` por línea, con `#` para los comentarios. Ahí van los días no laborables que declare el Gobierno y las campañas y operaciones de la otra aplicación del servidor (decisión 5). Si no hay fechas, el archivo existe y está vacío: si falta, bloquea.
- **Lo que no comprueba:** CI, procedencia, huella, `main`, clase y ritmo (lado de GitHub, F1-20c y F1-20e1); el vigilante de la otra aplicación y el lock común del servidor (F1-20f).

## Probar en local

```bash
sh infra/prod/validate.sh --caddy
bash infra/prod/test-smoke.sh   # smoke.sh con docker y curl falsos: certificado, /demo y /health/*
bash infra/prod/test-deploy.sh  # deploy.sh con docker falso; también el lock con backup.sh nightly
bash infra/prod/test-cd-preflight.sh  # cd-preflight.sh con un servidor simulado, una prueba por condición
```

Para levantarlo entero en local, usa archivos de entorno desechables en un directorio temporal y `CRM_DOMAIN=localhost`. Caddy emite entonces `crm.localhost` con su CA interna. Añade un override que publique el proxy en `127.0.0.1` con otros puertos y un `-p` propio. Al terminar, `down -v`.
