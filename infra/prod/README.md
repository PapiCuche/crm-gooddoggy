# infra/prod

Producción en un servidor Linux con Docker Compose, según [ADR-033](../../docs/adr/ADR-033-production-hosting.md). El primer arranque, el despliegue y los backups tienen sus runbooks: [production-first-boot.md](../../docs/runbooks/production-first-boot.md) (F1-17), F1-15 (#756) y F1-16 (#757). Aquí solo está lo que esos runbooks usan.

| Archivo | Qué es |
| --- | --- |
| `compose.yaml` | Los servicios de ADR-033 §3, sin Garage, Mailpit ni `build:`. Usa imágenes por digest y límites de memoria, y rota los logs |
| `Caddyfile` | El borde: HTTPS automático para `crm.<DOMINIO>`, las rutas del stack local, los topes de cuerpo, la tasa por IP en `/webhooks/*` y los registros sin query. `/health/*` responde 404 |
| `caddy/Dockerfile` | Caddy con `caddy-ratelimit` (decisión 10). CI la publica por digest en F1-15 |
| `.env.production.example` | Los nombres de las variables, sin valores, en una sección por archivo del servidor |
| `postgres/bootstrap-roles.sh` | Crea `crm_migrator` y `crm_app` (ADR-002 §1), sin `CREATEDB` ni BD de tests. Se puede repetir |
| `validate.sh` | `docker compose config` con valores ficticios. Comprueba que solo el proxy publica puertos. Con `--caddy`, también `caddy validate` (CI: `infra-prod.yml`) |
| `smoke.sh` | La prueba de humo tras `up --wait`, que la llama `deploy.sh` (F1-15) |
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

## Probar en local

```bash
sh infra/prod/validate.sh --caddy
bash infra/prod/test-smoke.sh   # smoke.sh con docker y curl falsos: certificado, /demo y /health/*
```

Para levantarlo entero en local, usa archivos de entorno desechables en un directorio temporal y `CRM_DOMAIN=localhost`. Caddy emite entonces `crm.localhost` con su CA interna. Añade un override que publique el proxy en `127.0.0.1` con otros puertos y un `-p` propio. Al terminar, `down -v`.
