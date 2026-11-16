#!/usr/bin/env bash
# Prueba de smoke.sh sin servidor ni Docker (F1-17, CI: infra-prod.yml): pone delante en el PATH un
# `docker` y un `curl` falsos que responden como producción sana, y comprueba que smoke.sh pasa y
# que falla, con su mensaje, si el certificado no vale (curl corta la conexión TLS, o con -k la
# cadena no verifica), si /demo o /health/* responden algo distinto de 404, o si HTTP no redirige.
# Uso: bash infra/prod/test-smoke.sh
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
mkdir -p "$work/bin"
touch "$work/release.env"

# docker falso: la lectura de PostgreSQL como crm_app y unos logs sin la query de /webhooks.
cat > "$work/bin/docker" <<'EOF'
#!/bin/sh
case "$*" in
  *" exec -T backend python -c "*) echo 'crm_app panic none 0 terse log superuser-rejected' ;;
  *" logs "*) echo '{"msg":"GET /webhooks/meta/?redacted"}' ;;
  *) echo "docker inesperado: $*" >&2; exit 1 ;;
esac
EOF
# curl falso: el formato de -w y la URL (el último argumento) eligen la respuesta. TLS_EXIT simula
# que curl corta la conexión (60), VERIFY el resultado de la verificación y CODE_<ruta> un estado.
cat > "$work/bin/curl" <<'EOF'
#!/bin/bash
format="" url="" body=""
while [ $# -gt 0 ]; do
  case $1 in
    -w) format=$2; shift ;;
    --data-binary) body=1; shift ;;
    -o | -X | -H) shift ;;
    *) url=$1 ;;
  esac
  shift
done
[ -z "$body" ] || cat > /dev/null
[ "${TLS_EXIT:-0}" = 0 ] || { echo "curl: (60) SSL certificate problem" >&2; exit "$TLS_EXIT"; }
if [ "$format" = '%{ssl_verify_result}' ]; then printf '%s' "${VERIFY:-0}"; exit 0; fi
case $url in
  http://*) status=${CODE_HTTP:-308} ;;
  */health/live) status=${CODE_HEALTH_LIVE:-404} ;;
  */health/ready) status=${CODE_HEALTH_READY:-404} ;;
  */demo) status=${CODE_DEMO:-404} ;;
  */smoke/) status=413 ;;
  *) status=200 ;;
esac
[ -z "$format" ] || printf '%s' "$status"
EOF
chmod +x "$work/bin/"*

failures=0
# <nombre> <texto esperado en la salida, o OK> [VAR=valor …]
check() {
  local name=$1 want=$2 out status=0
  shift 2
  out=$(env "$@" PATH="$work/bin:$PATH" CRM_RELEASE_ENV="$work/release.env" \
    sh "$here/smoke.sh" https://crm.example.invalid 2>&1) || status=$?
  if [ "$want" = OK ]; then
    [ "$status" = 0 ] && [[ $out == *"smoke OK"* ]] && { echo "ok   $name"; return; }
  else
    [ "$status" != 0 ] && [[ $out == *"$want"* ]] && { echo "ok   $name"; return; }
  fi
  echo "FAIL $name (salida $status): $out" >&2
  failures=$((failures + 1))
}

check "producción sana" OK
check "certificado que curl rechaza" "el certificado de https://crm.example.invalid no es válido (curl" TLS_EXIT=60
check "certificado con -k que no verifica" "no es válido (verificación 18)" VERIFY=18
check "/demo publicado" "/demo se publica" CODE_DEMO=200
check "/demo que redirige" "/demo se publica" CODE_DEMO=307
check "/health/ready publicado" "/health/ready se publica" CODE_HEALTH_READY=200
check "/health/live publicado" "/health/live se publica" CODE_HEALTH_LIVE=200
check "HTTP sin redirección" "HTTP no redirige a HTTPS" CODE_HTTP=200

[ "$failures" = 0 ] || { echo "test-smoke: $failures fallos" >&2; exit 1; }
echo "test-smoke: smoke.sh pasa con producción sana y falla en cada comprobación pública"
