#!/bin/sh
# Prueba de humo de producción tras `up --wait` (F1-13; deploy.sh de F1-15 la llama). Comprueba lo
# que ninguna prueba estática ve en el servidor real:
#   1. PostgreSQL, leído con SHOW como crm_app, no copia valores de clientes en su log;
#   2. el proxy corta cuerpos de más de 11 MB en /api/* y 4 MB en /webhooks/*, también sin
#      Content-Length (chunked): con ASGI, Django leería el cuerpo entero antes de la vista;
#   3. /health/* no se publica, y HTTP redirige a HTTPS;
#   4. la query de /webhooks (el verify token de Meta) no aparece en los logs.
# Uso: sudo CRM_RELEASE_ENV=/etc/crm/release.env sh infra/prod/smoke.sh https://crm.<DOMINIO> [opciones de curl]
set -eu
origin=${1:?uso: smoke.sh https://crm.<DOMINIO> [opciones de curl]}
shift
here=$(cd "$(dirname "$0")" && pwd)
compose() { docker compose -f "$here/compose.yaml" --env-file "${CRM_RELEASE_ENV:?release.env del despliegue}" "$@"; }
fail() { echo "smoke FAIL: $*" >&2; exit 1; }
code() { curl -sS -o /dev/null -w '%{http_code}' "$@"; }

got=$(compose exec -T backend python -c '
import os, psycopg
with psycopg.connect(os.environ["DATABASE_URL"]) as db:
    names = ("current_user", "log_min_error_statement", "log_statement", "log_parameter_max_length_on_error")
    print(*(db.execute(("SELECT " if n == "current_user" else "SHOW ") + n).fetchone()[0] for n in names))
')
[ "$got" = "crm_app panic none 0" ] || fail "PostgreSQL como crm_app: $got"

for route in "api 11000001" "webhooks 4000001"; do
  path=${route% *} size=${route#* }
  for mode in length chunked; do
    header="X-Smoke: $mode"
    [ "$mode" = chunked ] && header="Transfer-Encoding: chunked"
    status=$(head -c "$size" /dev/zero | code "$@" -X POST -H "$header" --data-binary @- "$origin/$path/smoke/")
    [ "$status" = 413 ] || fail "/$path/ con $size bytes ($mode): $status, se esperaba 413"
  done
done

for path in /health/live /health/ready; do
  [ "$(code "$@" "$origin$path")" = 404 ] || fail "$path se publica"
done
[ "$(code "$@" "http://${origin#https://}/")" = 308 ] || fail "HTTP no redirige a HTTPS"

token="smoke-$(od -An -N8 -tx1 /dev/urandom | tr -d ' \n')"
code "$@" "$origin/webhooks/meta/?hub.mode=subscribe&hub.challenge=1&hub.verify_token=$token" > /dev/null
if compose logs --since 5m proxy backend 2>&1 | grep -q "$token"; then
  fail "la query de /webhooks aparece en los logs"
fi
echo "smoke OK: $origin"
