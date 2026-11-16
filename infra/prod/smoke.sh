#!/bin/sh
# Prueba de humo de producción tras `up --wait` (F1-13; deploy.sh de F1-15 la llama). Comprueba lo
# que ninguna prueba estática ve en el servidor real, sin credenciales de la aplicación:
#   0. el certificado de crm.<DOMINIO> es válido: cadena, fechas y nombre (F1-17). Espera hasta
#      CRM_SMOKE_TLS_TIMEOUT s de reloj (120; cada intento corta a los 15 s, uno cada
#      CRM_SMOKE_TLS_INTERVAL s, 5) a que Caddy lo obtenga: en el primer arranque lo pide en segundo
#      plano y `up --wait` no lo espera;
#   1. PostgreSQL, leído con SHOW como crm_app, no copia valores de clientes en su log, y rechaza
#      al superusuario por red (solo por el socket local);
#   2. el proxy corta cuerpos de más de 11 MB en /api/*, 4 MB en /webhooks/* y 1 MB en el resto,
#      también sin Content-Length (chunked): Django (ASGI) y Next leerían el cuerpo entero;
#   3. /health/* y /demo no se publican (404), HTTP redirige a HTTPS y la query de /webhooks no llega
#      a los logs.
# Uso: sudo CRM_RELEASE_ENV=/etc/crm/release.env sh infra/prod/smoke.sh https://crm.<DOMINIO> [opciones de curl]
# La comprobación 0 quita -k y --insecure y no lee ~/.curlrc (-q): con una CA propia (Caddy en
# local), --cacert <raíz>; para otra IP, --resolve crm.<DOMINIO>:443:<IP>, y el nombre se verifica.
# Un -k dentro de un grupo (-sk) no se quita: ssl_verify_result sigue exigiendo la cadena, no el nombre.
# Prueba sin servidor: test-smoke.sh (CI: infra-prod.yml).
set -eu
origin=${1:?uso: smoke.sh https://crm.<DOMINIO> [opciones de curl]}
shift
here=$(cd "$(dirname "$0")" && pwd)
# Cada llamada con su plazo (F1-15b): un docker o un proxy colgado no cuelga la puerta de deploy.sh.
compose() { timeout 60 docker compose -f "$here/compose.yaml" --env-file "${CRM_RELEASE_ENV:?release.env del despliegue}" "$@"; }
fail() { echo "smoke FAIL: $*" >&2; exit 1; }
# Un curl que falla (o agota el plazo) dice por qué, también dentro de $(…), donde set -e sale sin más.
code() {
  for url do :; done # la URL, el último argumento: las opciones de curl del operador no se imprimen
  curl -sS --connect-timeout 5 --max-time 60 -o /dev/null -w '%{http_code}' "$@" || fail "curl sale con $? en $url"
}

# curl verifica la cadena, las fechas y el nombre; sin -k, y ssl_verify_result por si un -k se cuela.
tls() {
  for arg do
    shift
    case $arg in -k | --insecure) ;; *) set -- "$@" "$arg" ;; esac
  done
  # -q, el primero: solo ahí deja de leer ~/.curlrc.
  curl -q --connect-timeout 5 --max-time 15 -sS -o /dev/null -w '%{ssl_verify_result}' "$@" "$origin/"
}
seconds() { # <variable> <valor>: un entero mayor que 0
  case $2 in '' | *[!0-9]*) fail "$1 debe ser un número entero de segundos mayor que 0" ;; esac
  [ "$2" -gt 0 ] || fail "$1 debe ser un número entero de segundos mayor que 0"
}
tls_timeout=${CRM_SMOKE_TLS_TIMEOUT:-120} tls_every=${CRM_SMOKE_TLS_INTERVAL:-5}
seconds CRM_SMOKE_TLS_TIMEOUT "$tls_timeout"
seconds CRM_SMOKE_TLS_INTERVAL "$tls_every"
start=$(date +%s)
while :; do
  rc=0
  verify=$(tls "$@" 2> /dev/null) || rc=$?
  waited=$(($(date +%s) - start))
  [ "$rc" != 0 ] || [ "$verify" != 0 ] || break
  [ "$waited" -lt "$tls_timeout" ] ||
    fail "el certificado de $origin no es válido tras ${waited}s (curl sale con $rc, verificación ${verify:-?})"
  sleep "$tls_every"
done
[ "$waited" = 0 ] || echo "smoke: certificado válido tras ${waited}s"

got=$(compose exec -T backend python -c '
import os, psycopg
with psycopg.connect(os.environ["DATABASE_URL"]) as db:
    names = ("current_user", "log_min_error_statement", "log_statement",
             "log_parameter_max_length_on_error", "log_error_verbosity", "log_min_messages")
    print(*(db.execute(("SELECT " if n == "current_user" else "SHOW ") + n).fetchone()[0] for n in names), end=" ")
try:
    psycopg.connect(host="postgres", user="postgres", password="smoke", dbname="postgres", connect_timeout=5)
    print("superuser-accepted")
except psycopg.OperationalError as error:
    print("superuser-rejected" if "pg_hba.conf rejects" in str(error) else "superuser-other")
') || fail "PostgreSQL como crm_app: docker compose exec sale con $?"
[ "$got" = "crm_app panic none 0 terse log superuser-rejected" ] || fail "PostgreSQL como crm_app: $got"

for route in "api 11000001" "webhooks 4000001" "ws 1000001" "app 1000001"; do
  path=${route% *} size=${route#* }
  for mode in length chunked; do
    header="X-Smoke: $mode"
    [ "$mode" = chunked ] && header="Transfer-Encoding: chunked"
    status=$(head -c "$size" /dev/zero | code "$@" -X POST -H "$header" --data-binary @- "$origin/$path/smoke/")
    [ "$status" = 413 ] || fail "/$path/ con $size bytes ($mode): $status, se esperaba 413"
  done
done

for path in /health/live /health/ready /demo; do
  status=$(code "$@" "$origin$path")
  [ "$status" = 404 ] || fail "$path se publica"
done
status=$(code "$@" "http://${origin#https://}/")
[ "$status" = 308 ] || fail "HTTP no redirige a HTTPS"

token="smoke-$(od -An -N8 -tx1 /dev/urandom | tr -d ' \n')"
code "$@" "$origin/webhooks/meta/?hub.mode=subscribe&hub.challenge=1&hub.verify_token=$token" > /dev/null
logs=$(compose logs --since 5m proxy backend 2>&1) || fail "docker compose logs sale con $?"
if printf '%s\n' "$logs" | grep -q "$token"; then
  fail "la query de /webhooks aparece en los logs"
fi
echo "smoke OK: $origin"
