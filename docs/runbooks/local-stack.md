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
