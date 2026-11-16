# Primer arranque de producción

Deja el CRM en marcha por primera vez en un servidor nuevo: secretos generados en el servidor, roles de la base, backups, el primer despliegue, la organización con su Owner, el segundo factor del Owner y la prueba de humo. Implementa [ADR-033](../adr/ADR-033-production-hosting.md) §2, §6 y §10 (F1-17, #758).

Los valores que aparecen entre `<…>` son marcadores: `<DOMINIO>` es el dominio sin el prefijo `crm.`, `<SHA>` el commit de `main` que se despliega y `<OWNER>/<REPO>` este repositorio. Ningún valor real (dominio, IP, correo, nombre de la tienda, claves) va en este runbook, en el repositorio, en un issue, en un PR ni en el chat.

**Reglas de los secretos, en todo el runbook:**

- Se generan **en el servidor**, con `openssl rand`, y van directos a su archivo de `/etc/crm` (root, `0600`). Nadie los escribe a mano ni los copia a otra parte que no sea la copia cifrada del paso 2.4.
- Nunca en el chat, en un issue, en un PR, en un log, en una captura ni en los argumentos de una orden (se verían en `ps` y en el historial de la shell). Las órdenes de este runbook los pasan por tuberías, archivos o la entrada estándar.
- Nadie entra en el grupo `docker`: quien puede usar Docker lee todos los secretos (ADR-033 §6.2). Siempre `sudo docker`.

## 1. El servidor

Lo provisiona el propietario (F1-18). Antes de empezar tiene que cumplir ADR-033 §2:

| Requisito         | Mínimo                                                 | Recomendado |
| ----------------- | ------------------------------------------------------ | ----------- |
| CPU               | 2 vCPU                                                 | 4 vCPU      |
| Memoria           | 4 GB                                                   | 8 GB        |
| Disco             | 60 GB SSD                                              | 100 GB SSD  |
| Sistema operativo | Ubuntu Server LTS con soporte (24.04) o Debian estable | Igual       |
| Arquitectura      | `amd64` (CI solo publica imágenes `linux/amd64`)       | Igual       |
| Red               | IP pública fija IPv4                                   | Igual       |

Y además:

- Un registro **A** `crm.<DOMINIO>` hacia la IP del servidor, con TTL corto (p. ej., 300 s), y **sin AAAA** (ADR-033 §4.4). Caddy pide el certificado en el primer despliegue: el registro tiene que resolver ya y los puertos 80 y 443 tienen que estar abiertos.
- El cortafuegos del proveedor abre solo `80/tcp`, `443/tcp` y el SSH, este último restringido a las direcciones de quien administra cuando sea posible.
- Un usuario nominal con `sudo` por persona, entrando con clave SSH.

**Endurecimiento** (ADR-033 §2), como ese usuario:

```bash
# SSH solo con clave y sin root. Antes de cerrar esta sesión, comprueba en otra terminal que entras.
printf 'PasswordAuthentication no\nKbdInteractiveAuthentication no\nPermitRootLogin no\n' |
  sudo install -m 0644 /dev/stdin /etc/ssh/sshd_config.d/10-crm.conf
sudo sshd -t && sudo systemctl reload ssh

# Cortafuegos del sistema (segunda capa). Docker se salta ufw con los puertos publicados:
# por eso en infra/prod solo el proxy publica puertos (infra/prod/README.md, «Red y cortafuegos»).
sudo apt-get update && sudo apt-get install -y ufw unattended-upgrades
sudo ufw allow OpenSSH && sudo ufw allow 80/tcp && sudo ufw allow 443/tcp && sudo ufw enable

# Actualizaciones de seguridad automáticas.
sudo dpkg-reconfigure -plow unattended-upgrades

# Algo de swap, para que un worker desbocado no haga que el kernel mate a PostgreSQL.
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

**Software:**

- Docker Engine y el plugin Compose desde el repositorio oficial de Docker ([Ubuntu](https://docs.docker.com/engine/install/ubuntu/), [Debian](https://docs.docker.com/engine/install/debian/)). Hacen falta Docker Engine 25 o superior y Compose 2.24 o superior: `sudo docker version` y `sudo docker compose version`. Docker se actualiza después en una ventana de mantenimiento, porque reiniciar el demonio reinicia los contenedores.
- El resto, de los paquetes del sistema:

  ```bash
  sudo apt-get install -y git curl openssl age rclone tmux
  ```

**El repositorio** en `/opt/crm`, la ruta que esperan las unidades de backup:

```bash
sudo git clone https://github.com/<OWNER>/<REPO>.git /opt/crm
```

## 2. Los secretos

### 2.1 El dominio

`proxy.env` lleva solo `CRM_DOMAIN` (no es secreto, pero no va al repositorio). Lo usan Caddy, `deploy.sh` y el paso 2.2:

```bash
sudo install -d -m 0700 /etc/crm
echo 'CRM_DOMAIN=<DOMINIO>' | sudo install -m 0600 /dev/stdin /etc/crm/proxy.env
```

### 2.2 Generarlos

Esta orden genera en el servidor la `DJANGO_SECRET_KEY` (96 caracteres hexadecimales), la primera KEK (`SECRETS_KEKS`, versión `1`, 32 bytes en base64 para URL, ADR-022), y las contraseñas del superusuario de PostgreSQL, de `crm_migrator`, de `crm_app` y de Redis (`openssl rand -hex 32` cada una). Las escribe en los archivos que espera `infra/prod/compose.yaml`, cada una solo donde hace falta (ADR-033 §6.1), y compone con el dominio las URL y los orígenes de `runtime.env`. Ningún valor pasa por la pantalla ni por los argumentos de un proceso: `printf` es una orden interna de la shell. Se niega a sobrescribir un archivo que ya exista.

```bash
sudo sh -eu <<'EOF'
umask 077
cd /etc/crm
domain=$(sed -n 's/^CRM_DOMAIN=//p' proxy.env)
[ -n "$domain" ] || { echo "falta CRM_DOMAIN en proxy.env" >&2; exit 1; }
for f in runtime.env migrate.env postgres.env redis.env; do
  [ ! -e "$f" ] || { echo "$f ya existe: no lo sobrescribo" >&2; exit 1; }
done
pg=$(openssl rand -hex 32) migrator=$(openssl rand -hex 32) app=$(openssl rand -hex 32) redis=$(openssl rand -hex 32)
printf 'POSTGRES_PASSWORD=%s\nCRM_MIGRATOR_PASSWORD=%s\nCRM_APP_PASSWORD=%s\n' "$pg" "$migrator" "$app" > postgres.env
printf 'DATABASE_MIGRATOR_URL=postgres://crm_migrator:%s@postgres:5432/crm\n' "$migrator" > migrate.env
printf 'REDIS_PASSWORD=%s\n' "$redis" > redis.env
{
  printf 'DJANGO_SECRET_KEY=%s\n' "$(openssl rand -hex 48)"
  printf 'SECRETS_KEKS=1:%s\n' "$(openssl rand -base64 32 | tr '+/' '-_')"
  printf 'DATABASE_URL=postgres://crm_app:%s@postgres:5432/crm\n' "$app"
  printf 'CELERY_BROKER_URL=redis://:%s@redis:6379/0\n' "$redis"
  printf 'CHANNEL_LAYER_REDIS_URL=redis://:%s@redis:6379/1\n' "$redis"
  printf 'DJANGO_ALLOWED_HOSTS=crm.%s,backend\n' "$domain"
  printf 'DJANGO_CSRF_TRUSTED_ORIGINS=https://crm.%s\n' "$domain"
  printf 'APP_ORIGIN=https://crm.%s\n' "$domain"
} > runtime.env
EOF
sudo ls -l /etc/crm   # todos -rw------- root root
```

### 2.3 Lo que da el propietario

Abre `runtime.env` con `sudoedit` (edita una copia temporal y la devuelve con el mismo dueño y modo) y añade, con los nombres de la sección `runtime.env` de [`.env.production.example`](../../infra/prod/.env.production.example). El editor no debe dejar copias del archivo con los secretos: con vim, `-n` (sin archivo de intercambio), `-i NONE` (sin viminfo, que guardaría en tu `$HOME` lo copiado) y `-u NONE` (sin un vimrc que active `backup`); nano no deja copias por omisión (`SUDO_EDITOR=nano`, sin `-B`).

```bash
export SUDO_EDITOR='vim -u NONE -n -i NONE'
sudoedit /etc/crm/runtime.env
```

- **Almacenamiento** (ADR-033 §5): `STORAGE_ENDPOINT_URL`, `STORAGE_PUBLIC_ENDPOINT_URL` si es otro, `STORAGE_REGION`, `STORAGE_BUCKET`, `STORAGE_ACCESS_KEY_ID`, `STORAGE_SECRET_ACCESS_KEY` y, si el proveedor lo pide, `STORAGE_ADDRESSING_STYLE`. Bucket privado y credencial limitada a él, con `s3:ListBucket`: `/health/ready` comprueba el bucket con un `HEAD`.
- **Correo** (ADR-019): `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_USE_TLS=true` (587) o `EMAIL_USE_SSL=true` (465), `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD` y `MAIL_FROM`. Sin correo no hay recuperación de contraseña ni altas de miembros ([add-member.md](add-member.md)).
- **Opcional:** `SENTRY_DSN` y `SENTRY_ENVIRONMENT` (ADR-033 decisión 7).
- **No van en el primer arranque:** `META_ENABLED`, `META_APP_SECRETS`, `META_WEBHOOK_VERIFY_TOKEN` y `HTTP_ALLOWED_HOSTS` ([whatsapp-activation.md](whatsapp-activation.md)), `CLAMD_ADDRESS` (no hay servicio `clamd` en `infra/prod`) y `MFA_ENFORCEMENT` (paso 7.3). Una variable que no se usa se **borra** del archivo: vacía no es lo mismo que ausente.

`backup.env` lo crea el paso 4.

### 2.4 Copia fuera del servidor

Si el servidor se pierde sin copia de las KEKs, los secretos cifrados de la base (los segundos factores, los tokens de los canales) son irrecuperables (ADR-033 §6.2, ADR-022). La copia va cifrada con [age](https://age-encryption.org) para las claves públicas de backup del propietario y de su suplente, las mismas de los backups. Cada uno crea la suya **en su propio equipo**, como dice [backup-restore.md](backup-restore.md) §2, y lleva al servidor solo la línea pública `age1…`:

```bash
sudo install -m 0644 /dev/stdin /etc/crm/backup-recipients.txt   # pega las líneas age1… y pulsa Ctrl-D
```

Después, en el servidor, un archivo cifrado con todos los archivos de entorno salvo `postgres.env`, que se borra en el paso 5.2:

```bash
sudo sh -c 'cd /etc/crm && tar -cf - --exclude=postgres.env -- *.env | age -R backup-recipients.txt' \
  > "crm-secretos-$(date -u +%Y%m%dT%H%MZ).tar.age"
```

Desde el equipo del propietario, tráelo, comprueba que se abre con su clave privada y bórralo del servidor:

```bash
scp '<usuario>@<servidor>:crm-secretos-*.tar.age' .
age -d -i backup-key.txt crm-secretos-<FECHA>.tar.age | tar -tf -   # lista runtime.env, migrate.env…
ssh <usuario>@<servidor> 'rm crm-secretos-*.tar.age'
```

- Va a un gestor de contraseñas o a una copia cifrada sin conexión, nunca al repositorio ni a un chat.
- **Se repite** tras crear `backup.env` (paso 4) y tras cada cambio en `/etc/crm`: una rotación, una credencial nueva del proveedor.
- **Las KEKs se rotan** como dice ADR-022 §5 (resumido en ADR-033 §6.3): la nueva versión entra con todos los procesos de runtime parados, el re-cifrado termina y solo entonces se quita la anterior. Una KEK no se quita al volver atrás un despliegue.
- **Retención:** cada versión de KEK se guarda **6 meses** después de quitarla de `SECRETS_KEKS`, mientras quede un backup cifrado con ella (la retención mensual de [backup-restore.md](backup-restore.md) §1). Por eso no se borra una copia antigua mientras sea la única que tiene una KEK de ese periodo.

## 3. Los roles de la base

El volumen de PostgreSQL se inicializa al primer arranque con `postgres.env`: la imagen ejecuta [`bootstrap-roles.sh`](../../infra/prod/postgres/bootstrap-roles.sh), que crea `crm_migrator` (propietario del esquema, `BYPASSRLS`, sin `CREATEDB`) y `crm_app` (el runtime: ni propietario, ni superusuario, ni `BYPASSRLS`), como pide ADR-002 §1.

**La versión.** Cada push a `main` publica las imágenes por digest (`.github/workflows/images.yml`). En GitHub, en la ejecución de `images` del commit `<SHA>`, el resumen del job trae su `release.env`. Pon el repositorio en ese commit y guarda el bloque en el servidor (no es secreto):

```bash
sudo git -C /opt/crm checkout --detach <SHA>
sudo install -m 0600 /dev/stdin /root/release-<SHA>.env   # pega el release.env del resumen y pulsa Ctrl-D
```

Si los paquetes de GHCR siguen privados, antes `sudo docker login ghcr.io` con un token de solo lectura de paquetes ([infra/prod/README.md](../../infra/prod/README.md), «Imágenes»).

**Arrancar solo PostgreSQL y comprobar los roles.** Todavía no existe `/etc/crm/release.env`: lo escribe `deploy.sh` cuando el despliegue pasa. Hasta entonces se usa el archivo de `/root`:

```bash
cd /opt/crm
sudo docker compose -f infra/prod/compose.yaml --env-file /root/release-<SHA>.env up -d --wait postgres
sudo docker compose -f infra/prod/compose.yaml --env-file /root/release-<SHA>.env exec -T postgres \
  psql -U postgres -d crm -Atc "SELECT rolname, rolsuper, rolbypassrls, rolcreatedb FROM pg_roles WHERE rolname IN ('crm_app', 'crm_migrator') ORDER BY 1"
```

Tiene que responder exactamente:

```text
crm_app|f|f|f
crm_migrator|f|t|f
```

Si el volumen ya existía (un intento anterior), la imagen no vuelve a ejecutar el script. Es idempotente: con `postgres.env` aún en su sitio, `sudo docker compose -f infra/prod/compose.yaml --env-file /root/release-<SHA>.env exec -T postgres /docker-entrypoint-initdb.d/10-roles.sh` crea lo que falte y fija atributos y contraseñas.

## 4. Lo que necesitan los backups

Se prepara antes del primer despliegue y se activa en el paso 5.3. Todo está en [backup-restore.md](backup-restore.md); aquí solo el orden:

1. Las claves de backup del propietario y del suplente (§2): ya están en `backup-recipients.txt` desde el paso 2.4. La clave de despliegue, si el propietario decide que exista, en `/etc/crm/backup-deploy-recipients.txt`.
2. El destino (§3): bucket privado con versionado, las reglas de `lifecycle.json`, una credencial del servidor que solo escribe y la opción de protección que elija el propietario.
3. `backup.env` (§4, las dos primeras órdenes): `sudo install -m 0600 /dev/null /etc/crm/backup.env` y `sudoedit /etc/crm/backup.env` (con el mismo `SUDO_EDITOR` del paso 2.3) con la sección `backup.env` de la plantilla. Opcional, `BACKUP_HEARTBEAT_URL` (§5).
4. Repetir la copia del paso 2.4, que ahora incluye `backup.env`.

Desde el segundo despliegue, `deploy.sh` no arranca sin esto: su volcado previo es `backup.sh predeploy`.

## 5. El primer despliegue

### 5.1 `deploy.sh`

Dentro de `tmux`, para que una sesión SSH perdida no corte el despliegue. La primera orden comprueba que `crm.<DOMINIO>` ya resuelve a la IP de este servidor: si no, Caddy no obtiene el certificado:

```bash
tmux new -s deploy
getent ahostsv4 crm.<DOMINIO>
cd /opt/crm
sudo infra/prod/deploy.sh /root/release-<SHA>.env
```

`deploy.sh` ([infra/prod/README.md](../../infra/prod/README.md), «Desplegar») valida el release, descarga los cinco digests, se salta el volcado previo (en el primer despliegue no hay nada que volcar), arranca PostgreSQL y Redis, ejecuta el job `migrate` como `crm_migrator`, levanta el stack y pasa la puerta de salud: `/health/ready` 200 desde dentro del backend, [`smoke.sh`](../../infra/prod/smoke.sh) y `https://crm.<DOMINIO>/login` 200. En el primer arranque Caddy pide el certificado en segundo plano, después de que `up --wait` termine: `smoke.sh` lo espera hasta 120 s de reloj (`CRM_SMOKE_TLS_TIMEOUT`; un intento cada 5 s, y cada uno corta a los 15 s) y, si tarda, dice cuánto (`smoke: certificado válido tras 15s`). Termina con `DESPLIEGUE OK: /etc/crm/release.env`.

Si la puerta falla en el primer despliegue no hay versión a la que volver: sale con `ROLLBACK IMPOSIBLE` y el stack queda como está. Mira `sudo docker compose -f infra/prod/compose.yaml --env-file /root/release-<SHA>.env ps` y `… logs backend proxy`, corrige y repite la misma orden. Lo habitual:

- **El certificado:**
  - `smoke FAIL: el certificado de … no es válido tras 125s (curl sale con 35 …)` (o 7, o 28; los segundos son los reales, algo más de 120): Caddy no lo obtuvo en ese tiempo. Los logs de `proxy` dicen qué respondió la autoridad. Si el registro A no resuelve aquí (`getent`) o el 80 o el 443 están cerrados, corrígelo. Si la autoridad solo va lenta, repite con más margen: `sudo CRM_SMOKE_TLS_TIMEOUT=300 infra/prod/deploy.sh /root/release-<SHA>.env`.
  - `… (curl sale con 60 …)`: hay certificado, pero no vale para `crm.<DOMINIO>` (otro nombre, caducado o de una autoridad de pruebas).
- **El backend no arranca:** `production` se niega a arrancar si falta o no vale una variable, y el error del log nombra la variable, nunca su valor.
- **`/health/ready` no da 200:** su respuesta dice qué falla (`database`, `redis` o `storage`). `storage`, casi siempre, es la credencial sin `s3:ListBucket`.

Un despliegue cortado (Ctrl-C, un reinicio, un error inesperado) no vuelve atrás: deja `/var/lib/crm-deploy/lock` y `/etc/crm/release.env.next`, y ningún despliegue arranca mientras estén. Qué hacer: [infra/prod/README.md](../../infra/prod/README.md), «Volver atrás», «Un despliegue cortado».

### 5.2 Retirar `postgres.env`

Con `migrate` ya hecho, la contraseña de `crm_migrator` no puede quedar en el entorno del contenedor de PostgreSQL (ADR-002 §1.1, ADR-033 §6.1). Se borra el archivo y se recrea el contenedor; los datos siguen en el volumen:

```bash
sudo shred -u /etc/crm/postgres.env
sudo docker compose -f infra/prod/compose.yaml --env-file /etc/crm/release.env up -d --wait --force-recreate postgres
sudo docker compose -f infra/prod/compose.yaml --env-file /etc/crm/release.env exec -T postgres \
  sh -c 'test -z "$POSTGRES_PASSWORD$CRM_MIGRATOR_PASSWORD$CRM_APP_PASSWORD" && echo limpio'
```

La última orden tiene que responder `limpio`. Las contraseñas de los roles siguen en `migrate.env` y `runtime.env`.

- **En un servidor nuevo** no hacen falta: `restore.sh` crea los roles con contraseñas nuevas, que se ponen también en `migrate.env` y `runtime.env` ([backup-restore.md](backup-restore.md) §6).
- **En este mismo servidor** (restaurar el volcado previo de un despliegue con `--replace-existing-data`), `--roles-env` tiene que llevar las contraseñas **actuales**: unas nuevas dejarían de coincidir con `migrate.env` y `runtime.env`. Se sacan de las URL de esos archivos, sin mostrarlas, a un archivo en memoria. La orden supone las contraseñas hexadecimales del paso 2.2: sin `@` ni codificación con `%`.

  ```bash
  sudo sh -eu <<'EOF'
  umask 077
  m=$(sed -n 's|^DATABASE_MIGRATOR_URL=postgres://crm_migrator:\([^@]*\)@.*|\1|p' /etc/crm/migrate.env)
  a=$(sed -n 's|^DATABASE_URL=postgres://crm_app:\([^@]*\)@.*|\1|p' /etc/crm/runtime.env)
  [ -n "$m" ] && [ -n "$a" ] || { echo "no encuentro las contraseñas actuales" >&2; exit 1; }
  printf 'CRM_MIGRATOR_PASSWORD=%s\nCRM_APP_PASSWORD=%s\n' "$m" "$a" > /dev/shm/roles.env
  EOF
  ```

  Después, `restore.sh … --roles-env /dev/shm/roles.env`, en el orden de [infra/prod/README.md](../../infra/prod/README.md), «Volver atrás», y al terminar `sudo shred -u /dev/shm/roles.env`.

### 5.3 Activar los backups

[backup-restore.md](backup-restore.md) §4, desde la copia de las unidades de systemd hasta la primera copia (`systemctl start crm-backup.service`). **Sáltate sus dos órdenes `install … /dev/null`**: vaciarían `backup.env` (paso 4) y `backup-recipients.txt` (paso 2.4), que ya están rellenos. El journal tiene que terminar en `"event":"backup.ok"`.

## 6. La organización y su Owner

`bootstrap_organization` crea la organización, sus roles de plantilla y el Owner inicial. Corre en el contenedor `backend` como `crm_app`, con motivo obligatorio, y queda auditado. Sin `-T`, para que haya terminal y pida la contraseña:

```bash
cd /opt/crm
sudo docker compose -f infra/prod/compose.yaml --env-file /etc/crm/release.env exec backend \
  python manage.py bootstrap_organization --slug <slug> --name "<Nombre de la organización>" \
  --owner-email <correo del Owner> --reason "primer arranque de producción"
```

- `--slug`: minúsculas, dígitos y guiones, sin `--`. Es la ruta del CRM: `https://crm.<DOMINIO>/o/<slug>`.
- El comando pide dos veces la contraseña del Owner, sin mostrarla, y la valida con la política de contraseñas. Si quien ejecuta no es el Owner, el Owner la escribe en ese momento; o, con el correo ya configurado, la cambia después con «¿Olvidaste tu contraseña?».
- Si la cuenta ya existe, `--existing-owner` en lugar de la contraseña: conserva la suya.
- Sin terminal (un script) existe `--password-stdin` con `exec -T`, leyendo la contraseña de un archivo `0600` que se borra después. Nunca como argumento.

Responde `Organización <slug> (<id>) creada. Owner: usuario nuevo <id>.` Con un slug repetido, `Ya existe una organización con ese slug`, y no escribe nada.

## 7. El Owner entra y activa su segundo factor

Antes de cargar ningún dato (ADR-023, ADR-003):

1. **Entrar:** en `https://crm.<DOMINIO>/login`, con el correo y la contraseña del paso 6. Con una sola organización, entra directamente en ella.
2. **Activar la verificación en dos pasos:** desde el menú, «Seguridad de la cuenta» (`/cuenta/seguridad`) → «Activar la verificación en dos pasos»:
   1. la contraseña actual;
   2. escanear el código QR con una aplicación de autenticación del teléfono (o escribir la clave) y escribir el código de 6 dígitos que muestra → «Activar»;
   3. guardar los **diez códigos de respaldo** («Copiar los códigos» o «Descargar») en el gestor de contraseñas, marcar «He guardado estos códigos en un lugar seguro» → «Terminar».
3. **Comprobarlo:** cerrar la sesión y volver a entrar. Tras la contraseña, `/login` pide el código. En «Seguridad de la cuenta» se lee «Activada desde el …» y «Te quedan 10 códigos de respaldo sin usar».

**`MFA_ENFORCEMENT` se queda sin poner** (`warn`). ADR-038 §8 prohíbe `enforce` en un entorno con cuentas reales mientras no exista el comando de operador que quita el segundo factor de otra persona (ADR-023 §11, todavía _Proposed_). Con `warn`, el segundo factor de quien lo tiene se pide en cada acceso igual (ADR-023 §5). Lo que no hace es impedir que entre un Administrador que aún no lo activó: cada persona que se dé de alta lo activa igual que el Owner.

Hoy no hay forma de recuperar una cuenta que pierde a la vez el teléfono y los códigos de respaldo (ADR-023 §11). Por eso los códigos se guardan fuera del teléfono.

## 8. La prueba de humo

**En el navegador**, como Owner:

1. Entrar (paso 7, con el código).
2. «Clientes» → «Crear cliente», con nombre `Prueba de humo` → aparece en la lista. Después, «Borrar» en su fila y confirmar: no quedan datos de prueba en producción.
3. «Inbox»: abre sin errores. Sin canales conectados, vacío.

**En el servidor:**

```bash
cd /opt/crm
# /health/ready desde dentro del backend: «200» y el estado de database, redis y storage.
sudo docker compose -f infra/prod/compose.yaml --env-file /etc/crm/release.env exec -T backend python -c 'import urllib.error as e, urllib.request as u
try: r = u.urlopen("http://127.0.0.1:8000/health/ready", timeout=5)
except e.HTTPError as x: r = x
print(r.status, r.read().decode())'

# Las comprobaciones públicas sin credenciales: certificado, PostgreSQL, topes de cuerpo,
# /health/* y /demo con 404, HTTP → HTTPS y la query de /webhooks fuera de los logs.
sudo CRM_RELEASE_ENV=/etc/crm/release.env sh infra/prod/smoke.sh https://crm.<DOMINIO>
```

`smoke.sh` termina en `smoke OK: https://crm.<DOMINIO>`. Si falla, dice cuál: `smoke FAIL: …`.

**Desde fuera** (el equipo de quien administra, no el servidor):

```bash
curl -s -o /dev/null -w '%{http_code}\n' https://crm.<DOMINIO>/health/ready   # 404
curl -s -o /dev/null -w '%{http_code}\n' https://crm.<DOMINIO>/health/live    # 404
curl -s -o /dev/null -w '%{http_code}\n' https://crm.<DOMINIO>/demo           # 404
curl -sS -o /dev/null -w '%{ssl_verify_result}\n' https://crm.<DOMINIO>/      # 0, sin error de certificado
```

Y el navegador no avisa de nada al abrir `https://crm.<DOMINIO>/`: el certificado es para `crm.<DOMINIO>` y está vigente.

## 9. Siguientes pasos

- **El equipo del Owner:** [add-member.md](add-member.md), una ejecución por persona.
- **El simulacro de restauración** en un servidor nuevo, obligatorio antes de salir a producción (F1-18): [backup-restore.md](backup-restore.md) §7.
- **Una comprobación externa** de `https://crm.<DOMINIO>/` con un servicio de monitorización de disponibilidad, más las alertas de disco y de backup (ADR-033 §9).
- **Los siguientes despliegues y la vuelta atrás:** [infra/prod/README.md](../../infra/prod/README.md), «Desplegar» y «Volver atrás». Desde el segundo despliegue, `deploy.sh` hace antes de migrar el volcado previo cifrado (`backup.sh predeploy`, a `db/predeploy/`, 7 días). Restaurarlo es la única vuelta atrás de una versión con migraciones que no siguen expandir y contraer, y **el orden depende de cómo terminó `deploy.sh`**: tras `DESPLIEGUE OK`, `restore.sh` con `CRM_RELEASE_ENV=/etc/crm/release.env.previous` y después `deploy.sh --rollback`; tras `ROLLBACK OK` o `ROLLBACK FALLIDO`, `restore.sh` con `CRM_RELEASE_ENV=/etc/crm/release.env` y después `deploy.sh /etc/crm/release.env`, nunca `--rollback`. `--roles-env`, con las contraseñas actuales (paso 5.2). Los pasos completos están en ese README.
