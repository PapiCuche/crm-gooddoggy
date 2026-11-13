# ADR-033: Producción en un servidor Linux con Docker Compose: topología, TLS, secretos, despliegue por digest y backups (RPO/RTO)

- **Status:** Proposed
- **Date:** 2026-11-13
- **Deciders:** propuesto por Claude Code (Opus 5.5) en el programa autónomo (ADR-015 §1) para la decisión del propietario en F1-12 (#753). Elegir el proveedor, pagar los servicios y custodiar los secretos es del propietario: este ADR no elige proveedor y es neutral respecto de él. Mientras esté `Proposed` no obliga al código: #754 (F1-13) y lo que depende de él esperan a la aceptación. Las decisiones pendientes están en «Decisiones requeridas del propietario».
- **Related:** [ADR-002](ADR-002-postgresql-rls.md) §1 y §1.1, [ADR-003](ADR-003-auth-session.md), [ADR-008](ADR-008-object-storage.md), [ADR-011](ADR-011-observability-and-logs.md), [ADR-012](ADR-012-engineering-runtime-baseline.md), [ADR-015](ADR-015-autonomous-delivery-program.md), [ADR-019](ADR-019-outbound-email.md) §6, [ADR-021](ADR-021-password-recovery.md) §8, [ADR-022](ADR-022-secrets-at-rest.md) §5 y §6, [ADR-023](ADR-023-mfa-totp.md), [ADR-026](ADR-026-test-accounts.md) §1, [ADR-029](ADR-029-whatsapp-cloud-api-channel.md) §3 y §13, [security-boundaries.md](../architecture/security-boundaries.md) §5, [phase-1.md](../phases/phase-1.md) (OBS-F1-01-2, OBS-F1-02-2, OBS-F1-06-2, OBS-F1-07-1), [phase-7.md](../phases/phase-7.md) (F7-03a, F7-12, D-F7-03), D10-H en [06-riesgos-y-decisiones.md](../fase-0/06-riesgos-y-decisiones.md), serie R0: #753 a #760, checklist del propietario #781

## Context

### Lo que ya existe y este ADR no cambia

El código ya decide buena parte de cómo arranca en producción. `config.settings.production` **no arranca** si:

- recibe `DATABASE_MIGRATOR_URL` o `CRM_MIGRATOR_PASSWORD` (ADR-002 §1.1); además, el rol conectado no puede ser superusuario, tener `BYPASSRLS` ni ser propietario (`ENFORCE_RUNTIME_DB_ROLE`);
- `DJANGO_DEBUG` está activo, `DJANGO_TEST_ACCOUNTS_ORGANIZATION` tiene valor (ADR-026 §1) o falta `DJANGO_ALLOWED_HOSTS`;
- `DJANGO_SECRET_KEY` es la de desarrollo o tiene menos de 50 caracteres;
- `STORAGE_ENDPOINT_URL` no es `https` o lleva credenciales en la URL (vacío significa AWS S3);
- hay `EMAIL_HOST` sin exactamente uno de `EMAIL_USE_TLS` o `EMAIL_USE_SSL`, sin `MAIL_FROM` o sin credenciales; o hay correo y `APP_ORIGIN` no es `https` (ADR-021 §8); o `MAIL_BACKEND` no es `smtp`;
- falta `SECRETS_KEKS` (ADR-022 §6);
- `META_ENABLED` está encendido y sus ajustes no valen (`META_APP_SECRETS`, de uno a tres; `META_WEBHOOK_VERIFY_TOKEN`; el broker);
- `env.check_floor_throttle` rechaza el tope del oráculo del costo (`QUOTE_FLOOR_THROTTLE`, F5-09e2) si no es válido;
- falta `FORWARDED_ALLOW_IPS`, o no son IP o redes CIDR de como mucho `/8` (IPv4) o `/16` (IPv6), o está `*`, o aparece `UVICORN_FORWARDED_ALLOW_IPS`.

Detrás del proxy, `production` confía en `X-Forwarded-Proto`, redirige a HTTPS (salvo `/health/`), emite HSTS de un año, usa la cookie `__Host-crm_session` con `Secure` y toma `DJANGO_CSRF_TRUSTED_ORIGINS` del entorno. El frontend también emite HSTS (`frontend/next.config.ts`).

El job de migraciones usa `config.settings.migrate`: conecta como `crm_migrator` con `DATABASE_MIGRATOR_URL`, se niega a arrancar si ve `DATABASE_URL` y descarta `SECRETS_KEKS` aunque se las den.

Las imágenes (`backend/Dockerfile`, `frontend/Dockerfile`) corren sin root (uid 10001 y `node`). El backend sirve ASGI con uvicorn (`--proxy-headers`, sin log de acceso) y su `HEALTHCHECK` consulta `/health/live`; el del frontend consulta `/`. `/health/ready` comprueba hoy **solo** la base de datos (`core/health.py`); Redis y el almacenamiento llegan con F1-14 (#755). El `Caddyfile` local publica `/api/*` y `/webhooks/*` hacia `backend`, `/ws/*` hacia `ws`, responde 404 a `/health/*` y envía el resto a `frontend`.

El stack local (`infra/docker/compose.yaml`) tiene `postgres` (18.6), `redis` (8.8.3, AOF, sin expulsión), `garage`, `mailpit`, el job `migrate` y los procesos `backend`, `ws`, `worker`, `worker-media` (cola `media`), `beat`, `frontend` y `proxy` (Caddy en una IP fija de una subred propia, que es la de `FORWARDED_ALLOW_IPS`).

La primera organización se crea con `manage.py bootstrap_organization` (F2-06), un comando de plataforma que pide `--reason`, corre en un contenedor de runtime y recibe la contraseña del Owner sin eco o por `--password-stdin`.

### Lo que falta

No hay un entorno de producción ni una decisión de hosting (D10-H, D-F7-03). El propietario tiene un servidor o un proveedor que aún no ha nombrado y no tiene dominio. La serie R0 (#753 a #760) necesita una decisión que fije la topología, los requisitos del servidor, dónde viven los secretos, cómo se despliega, qué se respalda y en cuánto tiempo se recupera.

### Consumo medido del stack local

`docker stats` del stack local en reposo, con datos sintéticos (2026-11-13):

| Proceso                                                       | Memoria  |
| ------------------------------------------------------------- | -------- |
| `worker` (Celery, concurrencia 2)                             | 231 MiB  |
| `postgres`                                                    | 141 MiB  |
| `beat`                                                        | 105 MiB  |
| `backend`, `ws`, `frontend` (cada uno)                        | ~92 MiB  |
| `proxy`, `redis` (juntos)                                     | ~32 MiB  |
| **Total**, sin `worker-media` (otro proceso Celery, ~230 MiB) | ~786 MiB |

Las imágenes ocupan unos 1,8 GB por versión (backend 456 MB, frontend 410 MB, PostgreSQL 672 MB, Redis 155 MB, Caddy 85 MB). La base local pesa 19 MB: no dice nada del volumen real, que crecerá con los mensajes y la auditoría.

## Decision

### 1. Un servidor Linux con Docker Compose

Producción corre en **un solo servidor Linux** con Docker Engine y el plugin Compose, con la misma forma que el stack local: los mismos procesos, las mismas imágenes y la misma separación de credenciales. No hay orquestador ni réplicas. La redundancia se cubre con backups fuera del servidor y un tiempo de recuperación acordado (§8), no con alta disponibilidad.

### 2. Requisitos del servidor

| Requisito         | Mínimo                                                                              | Recomendado                                     |
| ----------------- | ----------------------------------------------------------------------------------- | ----------------------------------------------- |
| CPU               | 2 vCPU                                                                              | 4 vCPU                                          |
| Memoria           | 4 GB                                                                                | 8 GB                                            |
| Disco             | 60 GB SSD                                                                           | 100 GB SSD                                      |
| Sistema operativo | Ubuntu Server LTS con soporte vigente (p. ej., 24.04) o Debian estable (p. ej., 13) | Igual                                           |
| Arquitectura      | `x86_64`/`amd64`; `arm64` solo si CI publica las imágenes para ella (decisión 1)    | `amd64`                                         |
| Red               | IP pública fija (IPv4)                                                              | Igual; IPv6 solo cuando se haya verificado §4.4 |

**Por qué ese tamaño.** En reposo el stack ocupa ~1 GiB (medido arriba, más `worker-media`). Con 4 GB quedan el sistema, la caché de PostgreSQL, un `pg_dump` en marcha y la descarga de una imagen nueva durante un despliegue; con 2 GB no caben con margen. Con 8 GB, PostgreSQL puede tener una caché útil y se puede subir la concurrencia de los workers. El disco cubre el sistema (~10 GB), dos o tres versiones de las imágenes para volver atrás (~5 GB), los datos y el WAL de PostgreSQL, la cola de Redis, los logs de Docker con rotación (§9) y el volcado previo a un despliegue. Los medios **no** ocupan el disco: van al almacenamiento externo (§5).

**Endurecimiento** (lo aplica el runbook de F1-17, #758):

- **Puertos públicos:** solo `80/tcp` y `443/tcp` (y `443/udp` si se deja HTTP/3). SSH en su puerto, restringido por el cortafuegos del proveedor a las direcciones de quien administra cuando sea posible.
- **SSH solo con clave:** sin contraseña (`PasswordAuthentication no`) ni acceso de `root` (`PermitRootLogin no`); un usuario nominal por persona con `sudo`.
- **Cortafuegos en dos capas:** el del proveedor y el del sistema. Docker escribe sus propias reglas para los puertos publicados y se salta las de `ufw`, así que **solo el proxy publica puertos**; PostgreSQL, Redis y los demás procesos no tienen `ports:` en producción (en local se publican en `127.0.0.1`; en producción, nada).
- **Actualizaciones de seguridad automáticas** del sistema (`unattended-upgrades` o equivalente). Docker Engine se actualiza en una ventana de mantenimiento, porque reiniciar el demonio reinicia los contenedores.
- **Límites de memoria** por contenedor y algo de swap, para que un worker desbocado no haga que el kernel mate a PostgreSQL.

### 3. Topología

```text
Internet ──80/443──▶ proxy (Caddy, HTTPS automático)
                       ├─ /api/*, /webhooks/* ──▶ backend (uvicorn ASGI)
                       ├─ /ws/*               ──▶ ws (uvicorn ASGI)
                       ├─ /health/*           ──▶ 404
                       └─ resto               ──▶ frontend (Next.js standalone)
backend, ws, worker, worker-media, beat ──▶ postgres (crm_app) y redis (red de datos interna)
migrate (job por despliegue)            ──▶ postgres (crm_migrator)
backend, ws, worker, worker-media       ──▶ S3 externo (https), SMTP externo, Meta, Sentry (opcional)
```

| Servicio       | Imagen                        | Credencial de BD                       | Publica puertos | Notas                                                                   |
| -------------- | ----------------------------- | -------------------------------------- | --------------- | ----------------------------------------------------------------------- |
| `proxy`        | Caddy (con el módulo de §4.3) | —                                      | 80, 443         | Única entrada desde Internet; IP fija en la subred del stack            |
| `backend`      | backend del repo              | `DATABASE_URL` → crm_app               | No              | HTTP de la API y de los webhooks                                        |
| `ws`           | backend del repo              | `DATABASE_URL` → crm_app               | No              | WebSocket del Inbox                                                     |
| `worker`       | backend del repo              | `DATABASE_URL` → crm_app               | No              | Celery, colas generales                                                 |
| `worker-media` | backend del repo              | `DATABASE_URL` → crm_app               | No              | Celery, cola `media`, un proceso                                        |
| `beat`         | backend del repo              | `DATABASE_URL` → crm_app               | No              | Una sola instancia                                                      |
| `frontend`     | frontend del repo             | —                                      | No              | Sin secretos; `BACKEND_ORIGIN` y `NEXT_PUBLIC_*` se fijan al construir  |
| `postgres`     | `postgres:18.x`               | superusuario, por el socket local      | No              | Volumen persistente; roles según ADR-002 §1; el backup vuelca aquí (§8) |
| `redis`        | `redis:8.x`                   | —                                      | No              | Broker y capa de canales; AOF                                           |
| `migrate`      | backend del repo              | `DATABASE_MIGRATOR_URL` → crm_migrator | No              | Job efímero por despliegue (§7); nunca recibe secretos de runtime       |

**Fuera de producción:** Garage (AGPL-3.0, OBS-F1-01-2, un solo nodo sin redundancia) y Mailpit. En producción el almacenamiento es un servicio S3 compatible externo por `https` (ADR-008) y el correo, un proveedor SMTP (ADR-019).

**Redes.** Dos redes de Docker: la del borde (`proxy`, `frontend`, `backend`, `ws`) y una **red de datos** interna (`internal: true`) con `postgres` y `redis`, a la que se unen solo los procesos que los usan (`backend`, `ws`, `worker`, `worker-media`, `beat` y `migrate`). `proxy` y `frontend` no entran en ella. Los procesos que necesitan salir a Internet (S3, SMTP, Meta, Sentry) conservan además una red con salida. Redis exige autenticación (`requirepass` o un usuario de ACL), y su contraseña va en la URL de `CELERY_BROKER_URL` y `CHANNEL_LAYER_REDIS_URL` dentro de `runtime.env`.

**Base de datos en producción.** Los roles siguen ADR-002 §1, con una diferencia que ADR-002 ya prevé: `crm_migrator` **no** tiene `CREATEDB` en producción, y no hay base `crm_test`. El script de roles de producción (F1-13) es una variante del local sin esas dos cosas.

### 4. Red, dominio y TLS

#### 4.1 Dominio y certificado

- Un registro **A** `crm.<dominio>` hacia la IP del servidor, con un TTL corto (p. ej., 300 s) para poder mover el servicio a otro servidor dentro del RTO (§8).
- Caddy obtiene y renueva el certificado solo, por ACME (HTTP-01 en el 80 o TLS-ALPN-01 en el 443). El nombre llega por entorno (`crm.{$CRM_DOMAIN}` en el `Caddyfile`), así que ningún dominio real entra en el repositorio. El volumen de datos de Caddy persiste para no pedir un certificado en cada reinicio y no chocar con los límites de la autoridad.
- La API de administración de Caddy sigue apagada (`admin off`), como en local.
- **HSTS:** ya lo emiten Django y Next; Caddy no añade otro. La redirección de HTTP a HTTPS la hace Caddy, y Django la repite.

#### 4.2 Rutas

Igual que el `Caddyfile` local: `/api/*` y `/webhooks/*` a `backend`, `/ws/*` a `ws`, `/health/*` responde 404 desde Internet, y el resto a `frontend`. Un solo origen para el navegador (ADR-003).

#### 4.3 `/webhooks/*` es pública y acotada

Meta tiene que poder llegar a `/webhooks/meta/` (ADR-029 §3), así que la ruta es pública. Antes de encender `META_ENABLED` (requisito de F7-12, phase-7.md):

- **Tamaño:** `request_body { max_size … }` en `/webhooks/*`, algo por encima del tope del backend (`BODY_MAX`). Con ASGI el cuerpo entero llega a Django antes de la vista; el tope real de memoria lo pone el proxy (F7-03a). El valor no se fija aquí: F7-12 pedía 2 MB con el `BODY_MAX` actual de 1 MiB, pero Meta documenta cuerpos de hasta 3 MB y ADR-032 §3 sube `BODY_MAX` a 3 MiB, con el proxy algo por encima. F1-13 toma el valor que esté vigente al escribir el `Caddyfile`.
- **Tasa por IP:** Caddy estándar no limita la tasa. Se propone una imagen de Caddy compilada en CI con `xcaddy` y el módulo `caddy-ratelimit`, publicada y fijada por digest como las demás (§7). Alternativa si se rechaza un módulo de terceros: limitar las conexiones nuevas por IP en la cadena `DOCKER-USER` (iptables o nftables), no en `ufw`, que Docker se salta (§2). Es más tosco y no distingue rutas.
- **Sin la query en los registros:** el verify token de Meta viaja en la query del GET. Si se activa el log de acceso de Caddy, se filtra la query de la URI. El backend ya arranca sin log de acceso de uvicorn.
- **Lista de IP de Meta** como segundo candado: opcional, si se mantiene al día (FIX LATER (4) de F7-03a).

#### 4.4 Dirección del cliente

Caddy es el borde: el backend acepta `X-Forwarded-For` solo de la IP fija de Caddy en la subred del stack (`FORWARDED_ALLOW_IPS`), como en local. Caddy no debe confiar en un `X-Forwarded-For` entrante mientras no haya otro proxy delante; si algún día lo hay (una CDN, un balanceador), se declara en Caddy y se revisa este punto.

Con IPv6, Docker puede entregar la conexión a través de su proxy de espacio de usuario y el contenedor ve la dirección de la pasarela en lugar de la del cliente: todos los clientes por IPv6 compartirían una dirección para la auditoría y los límites de intentos. Por eso no se publica un registro AAAA hasta comprobar en el servidor que la dirección del cliente llega intacta.

### 5. Servicios externos

- **Almacenamiento (ADR-008):** un bucket **privado** en un servicio S3 compatible por `https`, con cifrado en reposo del proveedor y una credencial limitada a ese bucket. Reglas de ciclo de vida de ADR-008 (subidas sin finalizar a 24 h). `STORAGE_ENDPOINT_URL` sin credenciales en la URL, o vacío para AWS.
- **Correo (ADR-019):** un proveedor SMTP con STARTTLS (587) o TLS implícito (465), uno de los dos; `MAIL_FROM` en el dominio, verificado con SPF, DKIM y DMARC. Sin `EMAIL_HOST` la aplicación arranca, pero la recuperación de contraseña y las invitaciones fallan al enviar (ADR-019 §6).
- **Salidas HTTP:** solo los hosts de `HTTP_ALLOWED_HOSTS` (`core.http`), por ejemplo `graph.facebook.com` cuando se active el canal.

### 6. Secretos

#### 6.1 Dónde viven

Los secretos de producción viven **solo** en el servidor, en archivos de entorno fuera del repositorio, propiedad de `root` y con modo `0600` (o en el gestor de secretos del proveedor, si ofrece uno que Compose pueda leer). **Nunca** en el repositorio, en issues, en PRs, en el chat, en logs, en las imágenes ni en `NEXT_PUBLIC_*`. Un archivo por destinatario, para que ninguno reciba lo que no necesita:

| Archivo (ejemplo) | Lo recibe                                         | Contiene                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| ----------------- | ------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `runtime.env`     | `backend`, `ws`, `worker`, `worker-media`, `beat` | `DJANGO_SECRET_KEY`, `DATABASE_URL` (crm_app), `SECRETS_KEKS`, `DJANGO_ALLOWED_HOSTS`, `DJANGO_CSRF_TRUSTED_ORIGINS`, `APP_ORIGIN` (`https://crm.<dominio>`), `FORWARDED_ALLOW_IPS`, `CELERY_BROKER_URL`, `CHANNEL_LAYER_REDIS_URL`, `STORAGE_*`, `EMAIL_*` y `MAIL_FROM`, `HTTP_ALLOWED_HOSTS`, `META_ENABLED`, `META_APP_SECRETS`, `META_WEBHOOK_VERIFY_TOKEN`, `SENTRY_DSN` y `SENTRY_ENVIRONMENT` (opcionales). **Nunca** `DATABASE_MIGRATOR_URL` ni `CRM_MIGRATOR_PASSWORD`                                                 |
| `migrate.env`     | `migrate`                                         | Solo `DATABASE_MIGRATOR_URL` (crm_migrator). **Nunca** `DATABASE_URL` ni secretos de runtime (ADR-002 §1.1)                                                                                                                                                                                                                                                                                                                                                                                                                      |
| `postgres.env`    | `postgres`                                        | Solo para el primer arranque del volumen: la contraseña del superusuario y las iniciales de `crm_migrator` y `crm_app`. La imagen oficial solo las usa al inicializar un volumen vacío. **Después se quitan del archivo y se recrea el contenedor**, para que la credencial de `crm_migrator` no quede en el entorno de `postgres` (ADR-002 §1.1). Alternativa: un script de una sola vez de F1-13 crea los roles y las contraseñas nunca entran en este archivo. El superusuario se usa después solo por el socket local (§8.1) |
| `proxy.env`       | `proxy`                                           | `CRM_DOMAIN` (no es secreto, pero no va al repositorio)                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |

Las credenciales de IA y los tokens de los canales **no** van al entorno: se pegan en las pantallas del CRM y se guardan cifrados en la base con `core.sealed` (ADR-022, security-boundaries.md §5). El app secret de Meta sí va al entorno (`META_APP_SECRETS`).

#### 6.2 Quién accede

- `root` y las personas que el propietario designe, con su usuario nominal y `sudo`. **Quien puede usar Docker lee todos los secretos** (`docker inspect` muestra el entorno de cada contenedor): pertenecer al grupo `docker` equivale a ser `root`. Por eso **nadie entra en el grupo `docker`**: se usa `sudo docker`, que queda en el registro de `sudo`.
- CI no tiene secretos de producción: construye y publica imágenes, nada más. El programa autónomo no tiene acceso al servidor.
- El propietario guarda **fuera del servidor** (un gestor de contraseñas o una copia cifrada sin conexión) una copia de los archivos de entorno, de todas las versiones de `SECRETS_KEKS` que sigan siendo necesarias (§8) y la clave privada de descifrado de los backups. Si el servidor se pierde con sus secretos y no hay copia, lo cifrado con las KEKs (los secretos TOTP, los tokens de canales) es irrecuperable (ADR-022, _Consequences_).

#### 6.3 Rotación

| Secreto                                   | Cómo se rota                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| ----------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `SECRETS_KEKS`                            | ADR-022 §5: se añade una versión mayor con **todos** los procesos de runtime parados y se arrancan ya con ella; el trabajo de re-cifrado vuelve a cifrar lo pendiente; solo entonces se quita la anterior del entorno. Una KEK **no se quita al volver atrás un despliegue**, y su copia fuera del servidor se conserva mientras exista un backup cifrado con ella (6 meses con la retención de §8). Una KEK comprometida se rota igual, y los secretos que protegía se dan por expuestos (ADR-022 §5): el módulo dueño de cada uno los renueva (re-enrolar el TOTP, rotar el token del proveedor) |
| `DJANGO_SECRET_KEY`                       | Cambiarla cierra todas las sesiones y anula los enlaces firmados en curso: en una ventana y ante sospecha                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| Contraseñas de `crm_app` y `crm_migrator` | `ALTER ROLE … PASSWORD` como superusuario, se actualiza el archivo de entorno y se reinicia lo que la usa. La de `crm_migrator`, periódicamente y ante cualquier sospecha (ADR-002 §1.1)                                                                                                                                                                                                                                                                                                                                                                                                           |
| `META_APP_SECRETS`                        | Admite hasta tres valores: se añade el nuevo, se cambia en Meta y se quita el viejo                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| S3, SMTP, Sentry, token de GHCR           | Se emite una credencial nueva en el proveedor, se cambia el archivo de entorno, se reinicia y se revoca la anterior                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |

### 7. Imágenes y despliegue

**Imágenes.** CI construye las imágenes del backend, del frontend y de Caddy (§4.3) en cada merge a `main` y las publica en el registro de contenedores de GitHub (GHCR) del repositorio, etiquetadas con el SHA del commit. El servidor las despliega **por digest** (`@sha256:…`), nunca por etiqueta; las de terceros (PostgreSQL, Redis) también se fijan por digest. Las imágenes no contienen secretos: `production` los pide todos al arrancar. La firma o la procedencia de las imágenes (atestaciones de build de GitHub o cosign), verificada en el servidor antes de desplegar, queda como mejora posterior con su propio work item. Su visibilidad es una decisión del propietario: **públicas** (recomendado: el repositorio ya es público y el servidor no necesita credencial para descargarlas) o **privadas**, con un token de solo lectura de paquetes en el servidor (`root`, `0600`).

**Despliegue** (lo implementa `deploy.sh` en F1-15, #756):

1. Un archivo de la versión (en el servidor) lista los digests. Se conserva el de la versión anterior.
2. Si la versión trae migraciones, `pg_dump` cifrado antes de tocar nada (§8), para la clave de backup y para la clave de despliegue (§8.2).
3. `docker compose pull` de los digests.
4. **Migraciones:** el job `migrate` corre una vez como `crm_migrator` con `migrate.env` y nada más. Si falla, el despliegue se detiene y el runtime anterior sigue en marcha. Cada migración corre en su transacción (salvo las marcadas como no atómicas): PostgreSQL revierte la que falla, pero las anteriores del mismo despliegue quedan aplicadas.
5. Se recrean los procesos de runtime con las imágenes nuevas (`up -d --wait`). Con un solo servidor hay un corte de segundos: no se promete despliegue sin corte.
6. **Puerta de salud:** los `HEALTHCHECK` de `backend`, `ws` y `frontend` en `healthy`; `/health/ready` respondido desde dentro de la red del stack (desde Internet es 404); y la página de entrada por `https://crm.<dominio>`. Hoy `ready` mira solo la base de datos y `worker`, `worker-media` y `beat` no tienen `HEALTHCHECK`: F1-14 (#755) añade Redis y el almacenamiento, y la puerta de producción: ni la entrada Sandbox ni `/demo` se sirven en producción.
7. **Vuelta atrás:** si la puerta falla, se vuelve a los digests de la versión anterior. Eso solo es seguro si el esquema nuevo sigue sirviendo a la imagen anterior, así que las migraciones siguen la regla de **expandir y después contraer**: una versión añade (tablas, columnas con valor por omisión o nulas) y otra posterior quita lo que ya nadie usa. Si una versión no puede cumplirla, se despliega en una ventana de mantenimiento y su vuelta atrás es restaurar el volcado del paso 2, con la pérdida de lo escrito desde entonces. Descifrar ese volcado exige una clave privada que no está en el servidor (§8.2): durante la ventana tiene que estar disponible quien custodia la clave de despliegue (quien despliega) o la de backup (el propietario o su suplente).

   **Quién impone la regla.** Hoy ninguna herramienta: la comprueba la revisión de cada PR que trae migraciones, y el PR declara si su versión la cumple o necesita ventana. Una comprobación automática en CI (señalar `RemoveField`, `DeleteModel`, `RenameField` o `AlterField` sin valor por omisión en las migraciones nuevas, o pasar la prueba de humo de la imagen anterior contra el esquema nuevo) queda como mejora de F1-15 o posterior.

**El job de migraciones también mantiene particiones:** las de auditoría se crean al migrar para el mes en curso y los 12 siguientes, sin partición por omisión (OBS-F1-06-2). Si no se despliega en un año, las escrituras de auditoría fallan. Se propone ejecutar el job `migrate` al menos una vez al mes aunque no haya versión nueva (un temporizador del sistema, F1-15).

### 8. Backups, RPO y RTO

#### 8.1 Qué se respalda

| Qué                        | Cómo                                                                                                                                                                  | Frecuencia                                            |
| -------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------- |
| PostgreSQL (base `crm`)    | `pg_dump` en formato personalizado, cifrado al vuelo con una clave **pública** (`age` o equivalente) y subido al destino; el volcado no queda en claro en el disco    | Cada noche y antes de cada despliegue con migraciones |
| Objetos del almacenamiento | Copia del bucket de producción al destino (`rclone` o la replicación del proveedor), con versionado en el destino para que un borrado en producción no borre la copia | Cada noche                                            |
| Secretos                   | Copia del propietario fuera del servidor (§6.2), aparte de los backups de datos                                                                                       | Cada vez que cambian                                  |

No se respaldan: Redis (la cola de tareas; lo que estuviera en cola al perder el servidor puede perderse), los certificados de Caddy (se vuelven a pedir), las imágenes (están en GHCR) ni el frontend (sin estado).

**Quién vuelca.** `pg_dump` desactiva `row_security` y falla si una política filtraría filas, así que `crm_app` (sin `BYPASSRLS`) no puede volcar las tablas con RLS. Se vuelca como el superusuario por el socket local del contenedor de PostgreSQL, lanzado por un temporizador de `root` en el servidor. No se usa la credencial de `crm_migrator`, que solo existe para el job de migraciones. Un rol propio de backup (`pg_read_all_data` y `BYPASSRLS`) sería más estrecho, pero añade un rol a ADR-002 §1: queda como mejora con su propio ADR.

**Los roles no van en el volcado:** al restaurar, se crean antes con el script de roles de producción (F1-13) y contraseñas nuevas, y después se restaura el volcado, que conserva propietarios y permisos.

#### 8.2 Destino y retención

- **Fuera del servidor:** otro proveedor u otra cuenta, en un bucket privado. La credencial del servidor **puede escribir y no puede borrar** (política del bucket), y el destino tiene **obligatoriamente versionado o bloqueo de objetos** (Object Lock): sin ellos, escribir encima de un objeto con el mismo nombre equivale a borrarlo. Así un servidor comprometido no destruye sus backups.
- **Retención:** 7 diarios, 4 semanales y 6 mensuales, aplicada por las reglas de ciclo de vida del destino (el servidor no borra).
- **La clave privada de descifrado nunca está en el servidor.** `age` cifra para varios destinatarios a la vez:
  - **Clave de backup:** cifra todos los volcados. La custodia el propietario, con una copia en manos de un suplente que él designe (decisión 5).
  - **Clave de despliegue:** cifra además el volcado previo a cada despliegue (§7, paso 2). La guarda fuera del servidor quien despliega, para poder volver atrás en la ventana sin depender del propietario. Si el propietario prefiere no tenerla, cada despliegue con ventana exige que él o su suplente estén disponibles.

#### 8.3 RPO y RTO propuestos

- **RPO de 24 h** para la base de datos y para los objetos: lo escrito desde el último backup nocturno puede perderse. Si solo se pierde el servidor, el bucket externo sigue intacto y los objetos no se pierden.
- **RTO de 4 h** en un servidor nuevo: aprovisionar con el runbook, restaurar los secretos, crear los roles, restaurar el último volcado, desplegar los digests de la última versión, apuntar el registro A y pasar la puerta de salud. **Exige que el propietario o su suplente estén localizables en la primera hora:** traen la clave privada de backup y la copia de los secretos (§6.2). Sin ellos no hay restauración, y el RTO no se cumple.
- **Para bajar el RPO** a minutos: archivado continuo del WAL (pgBackRest o WAL-G) al mismo destino, o PostgreSQL gestionado con recuperación a un instante (opción 2). Cuesta más operación o más dinero; se propone revisarlo cuando el volumen de datos o el negocio lo pidan.

#### 8.4 Simulacro de restauración

**Obligatorio antes de la salida a producción** (F1-18, #760) y recomendado cada trimestre y tras cambiar el procedimiento: en un servidor nuevo, restaurar el último backup y comprobar que el job `migrate` no tiene nada que aplicar, que `/health/ready` responde, que una cuenta con segundo factor entra (lo que demuestra que las KEKs restauradas abren los secretos cifrados), que un archivo se descarga y que el número de filas por tabla coincide con el origen. Se mide el tiempo real frente al RTO y se anota la fecha y el resultado en el runbook (F1-16, #757).

Una alerta avisa si en 26 h no hay un backup correcto (F1-16).

### 9. Observabilidad

- **Logs estructurados** (ADR-011): JSON por la salida estándar, ya redactados por `core.redaction`. Docker los guarda con rotación por tamaño (`max-size`, `max-file`) para que no llenen el disco; un agregador externo es opcional y posterior.
- **Sentry, opcional:** con `SENTRY_DSN`, `SentryReporter` envía errores tras `scrub_sentry_event`; sin él, `NoopReporter` no hace llamadas de red. Es una decisión del propietario (decisión 7).
- **Comprobación externa** de `https://crm.<dominio>/` con cualquier servicio de monitorización de disponibilidad, más alertas de disco (> 80 %), del backup que falta (§8.4) y de reinicios repetidos de un contenedor. Las alertas de la ingesta de WhatsApp son de F7-11.
- ADR-002 pide además alertar si `crm_migrator` se conecta fuera de un despliegue: queda para F1-14 o F1-16.

### 10. Primer arranque

El runbook de F1-17 (#758) lo detalla: aprovisionar y endurecer el servidor, crear los archivos de entorno (generar `DJANGO_SECRET_KEY` y la primera KEK, ADR-022 _Operational implications_), apuntar el DNS, levantar el stack con el job `migrate`, crear la organización con `docker compose exec -T backend python manage.py bootstrap_organization … --password-stdin --reason …`, entrar como Owner y activar su segundo factor (ADR-023), y hacer el simulacro de §8.4. Mientras no se decida D-F2-14 (#759), solo el Owner puede entrar en producción.

### 11. Lo que este ADR no decide

- El proveedor del servidor, del almacenamiento, del correo y del destino de los backups: son del propietario.
- Los archivos de `infra/` (F1-13), los scripts de despliegue (F1-15) y de backup (F1-16).
- Alta disponibilidad, varios servidores o réplicas de lectura.
- La firma y la verificación de procedencia de las imágenes (§7): mejora posterior.
- El antivirus de los archivos (ClamAV, ADR-008): sigue pendiente antes de servir archivos de clientes (D-F7-04).

## Alternatives considered

| Opción                                       | Coste                                                                      | Operación                                                                          | Backups                                                        | Riesgos                                                                                                                                                                                                                                                                                                             |
| -------------------------------------------- | -------------------------------------------------------------------------- | ---------------------------------------------------------------------------------- | -------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **1. Un servidor con Compose (recomendada)** | El más bajo: una máquina, más almacenamiento, SMTP y el destino del backup | Sistema, Docker y PostgreSQL a cargo de quien opera; el mismo compose que en local | Propios (§8): RPO 24 h, RTO 4 h                                | Punto único de fallo; depende de que los backups y el simulacro se hagan de verdad                                                                                                                                                                                                                                  |
| 2. Servidor y PostgreSQL gestionado          | Medio: la base gestionada suele costar más que la máquina                  | Menos trabajo de base de datos; red privada entre la máquina y la base             | Del proveedor, con recuperación a un instante (RPO de minutos) | **Hoy no es viable sin código:** `DATABASE_URL` ignora en silencio `?sslmode=…` (OBS-F1-02-2), y una base remota exige TLS verificado. Hay que comprobar que el proveedor permita un rol con `BYPASSRLS` (`crm_migrator`) que no sea superusuario y funciones `SECURITY DEFINER` con ese propietario (ADR-002 §3.3) |
| 3. PaaS de contenedores                      | Medio o alto: se paga por servicio (seis procesos, más Redis y PostgreSQL) | Menos sistema que mantener                                                         | Del PostgreSQL gestionado de la plataforma                     | `FORWARDED_ALLOW_IPS` exige las IP o redes de los proxies, y los enrutadores de un PaaS no suelen tener un rango estable y estrecho; hace falta un job de migraciones con credencial propia que el runtime nunca vea; el WebSocket, el límite de cuerpo y la tasa en `/webhooks/*` dependen de la plataforma        |
| 4. Kubernetes (gestionado o no)              | Alto                                                                       | Alta: un clúster que mantener para un equipo pequeño                               | Según el almacenamiento elegido                                | **Descartada:** el coste de operación no compra nada que este tamaño necesite                                                                                                                                                                                                                                       |

## Consequences

- La serie R0 puede avanzar con su orden: F1-13 (`infra/prod`), F1-14, F1-15, F1-16, F1-17 y F1-18. Sin dominio, todo avanza salvo la salida (F1-18) y WhatsApp en vivo.
- D10-H y D-F7-03 quedan respondidas cuando el propietario acepte este ADR.
- El propietario asume la operación del servidor (parches, accesos, custodia de secretos y simulacros) o la delega en alguien con acceso.
- Una caída del servidor deja el CRM fuera de servicio hasta restaurar (RTO 4 h) y puede perder hasta 24 h de datos (RPO).
- Las migraciones nuevas tienen que seguir la regla de expandir y contraer, o declararse como despliegue con ventana (§7).

## Security implications

- No cambia ADR-001, ADR-002, ADR-003, ADR-013 ni ADR-014: confirma `crm_app` en el runtime y `crm_migrator` solo en `migrate`, cada uno con su archivo de entorno.
- Superficie pública mínima: 80 y 443 hacia Caddy; `/health/*` no se publica; PostgreSQL y Redis sin puertos.
- Quien es `root` o puede usar Docker en el servidor lee todos los secretos y la base de datos. El control es quién tiene SSH (§6.2), no una barrera técnica.
- Los backups están cifrados con una clave cuya parte privada no está en el servidor, y el servidor no puede borrarlos. Un backup de la base no basta para leer los secretos cifrados: hacen falta además las KEKs, que se guardan aparte (ADR-022).
- La ruta de webhooks es pública por necesidad: la protegen la firma (ADR-029), el tope de cuerpo y la tasa en el proxy.
- El módulo de limitación de tasa de Caddy es código de terceros que entra en la imagen del borde: se fija por versión y por digest y se revisa al actualizar.

## Operational implications

- Runbooks nuevos: primer arranque (F1-17), despliegue y vuelta atrás (F1-15), backup, restauración y simulacro (F1-16), rotación de secretos (§6.3, con ADR-022 §5).
- Tareas periódicas en el servidor: el backup nocturno, el job `migrate` mensual, las actualizaciones de seguridad, la revisión de alertas y el simulacro trimestral.
- Coste mensual: el servidor, el almacenamiento, el SMTP, el destino del backup y, si se elige, Sentry. Ninguno se fija aquí.

## Decisiones requeridas del propietario

| #   | Decisión                                      | Opciones                                                                                                                         | Recomendación                                                                                                                                                                                      |
| --- | --------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | Proveedor, tamaño y arquitectura del servidor | Cualquier proveedor con una máquina Linux e IP pública fija; `amd64` o `arm64`                                                   | Opción 1, 4 vCPU / 8 GB / 100 GB (mínimo 2 / 4 / 60), `amd64`, Ubuntu LTS o Debian estable                                                                                                         |
| 2   | Dominio                                       | Cualquier registrador con gestión de DNS                                                                                         | Un registro A `crm.<dominio>` con TTL corto; sin AAAA al principio (§4.4)                                                                                                                          |
| 3   | Almacenamiento S3 compatible                  | Cualquier proveedor S3 compatible por `https`                                                                                    | Bucket privado, credencial limitada al bucket, cifrado en reposo                                                                                                                                   |
| 4   | Proveedor SMTP                                | Cualquier proveedor transaccional con SMTP                                                                                       | STARTTLS en 587; dominio verificado con SPF, DKIM y DMARC                                                                                                                                          |
| 5   | Destino de los backups y custodia de la clave | Otro proveedor u otra cuenta; quién guarda la clave de backup y su suplente; si existe una clave de despliegue y quién la guarda | Fuera del servidor, con versionado o Object Lock y una credencial que escribe y no borra; retención 7 / 4 / 6; la clave de backup, el propietario y un suplente; la de despliegue, quien despliega |
| 6   | RPO y RTO                                     | 24 h / 4 h con `pg_dump` nocturno, o minutos con WAL continuo o base gestionada                                                  | 24 h / 4 h para R0                                                                                                                                                                                 |
| 7   | Sentry                                        | Sí (`SENTRY_DSN`) o no (solo logs)                                                                                               | Opcional; los logs bastan para empezar                                                                                                                                                             |
| 8   | Visibilidad de las imágenes en GHCR           | Públicas o privadas con un token de solo lectura                                                                                 | Públicas: no contienen secretos y el código ya es público                                                                                                                                          |
| 9   | Quién tiene acceso al servidor                | Las personas con SSH y `sudo`                                                                                                    | El propietario y quien él designe, con usuario nominal                                                                                                                                             |
| 10  | Límite de tasa de `/webhooks/*`               | Caddy con el módulo `caddy-ratelimit` o el cortafuegos del sistema                                                               | El módulo (por ruta y por IP); decisión técnica de F1-13 salvo objeción                                                                                                                            |
