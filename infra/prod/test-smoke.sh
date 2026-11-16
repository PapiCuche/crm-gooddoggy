#!/usr/bin/env bash
# Prueba de smoke.sh sin servidor ni Docker (F1-17, CI: infra-prod.yml): pone delante en el PATH un
# `docker`, un `curl` y un `sleep` falsos que responden como producción sana, y comprueba que smoke.sh
# pasa, también si el certificado tarda en llegar, y que falla con su mensaje en cada comprobación:
# el certificado (que no llega, o malo aunque se pase -k), PostgreSQL, los topes de cuerpo, /demo y
# /health/*, la redirección de HTTP y la query de /webhooks en los logs.
# Uso: bash infra/prod/test-smoke.sh
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
mkdir -p "$work/bin"
touch "$work/release.env"

# docker falso: la lectura de PostgreSQL (PG_OUT la cambia) y unos logs; con LEAK, las URL pedidas.
cat > "$work/bin/docker" <<'EOF'
#!/bin/sh
case "$*" in
  *" exec -T backend python -c "*) echo "${PG_OUT:-crm_app panic none 0 terse log superuser-rejected}" ;;
  *" logs "*) if [ -n "${LEAK:-}" ]; then cat "$CALLS"; else echo '{"uri":"/webhooks/meta/?redacted"}'; fi ;;
  *) echo "docker inesperado: $*" >&2; exit 1 ;;
esac
EOF
# curl falso: anota cada URL en $CALLS; el formato de -w y la URL eligen la respuesta.
#   TLS_PENDING=N: las N primeras comprobaciones del certificado fallan como sin certificado (35);
#   CERT_BAD: certificado malo, sin -k sale con 60 y con -k responde verificación 18 (como OpenSSL);
#   CODE_<RUTA>: el estado de esa ruta.
cat > "$work/bin/curl" <<'EOF'
#!/bin/bash
format="" url="" body="" insecure=""
while [ $# -gt 0 ]; do
  case $1 in
    -w) format=$2; shift ;;
    --data-binary) body=1; shift ;;
    -k | --insecure | -[a-zA-Z]*k*) insecure=1 ;;
    -o | -X | -H) shift ;;
    *) url=$1 ;;
  esac
  shift
done
echo "$url" >> "$CALLS"
[ -z "$body" ] || cat > /dev/null
if [ "$format" = '%{ssl_verify_result}' ]; then
  n=$(grep -c . "$TLS_COUNT" || true)
  echo x >> "$TLS_COUNT"
  [ "$n" -ge "${TLS_PENDING:-0}" ] || { echo "curl: (35) TLS connect error" >&2; exit 35; }
  if [ -n "${CERT_BAD:-}" ]; then
    [ -n "$insecure" ] || { echo "curl: (60) SSL certificate problem" >&2; exit 60; }
    printf 18
  else
    printf 0
  fi
  exit 0
fi
case $url in
  http://*) status=${CODE_HTTP:-308} ;;
  */health/live) status=${CODE_HEALTH_LIVE:-404} ;;
  */health/ready) status=${CODE_HEALTH_READY:-404} ;;
  */demo) status=${CODE_DEMO:-404} ;;
  */smoke/) status=${CODE_BODY:-413} ;;
  *) status=200 ;;
esac
[ -z "$format" ] || printf '%s' "$status"
EOF
printf '#!/bin/sh\n' > "$work/bin/sleep"
chmod +x "$work/bin/"*

failures=0
# <nombre> <texto esperado en la salida, u OK[texto]> [VAR=valor …] [-- opciones de curl]
check() {
  local name=$1 want=$2 out status=0 vars=()
  shift 2
  while [ $# -gt 0 ] && [ "$1" != -- ]; do vars+=("$1"); shift; done
  [ $# -eq 0 ] || shift
  : > "$work/calls"
  : > "$work/tls"
  out=$(env ${vars[@]+"${vars[@]}"} PATH="$work/bin:$PATH" CALLS="$work/calls" TLS_COUNT="$work/tls" \
    CRM_RELEASE_ENV="$work/release.env" sh "$here/smoke.sh" https://crm.example.invalid "$@" 2>&1) || status=$?
  case $want in
    OK*) if [ "$status" = 0 ] && [[ $out == *"smoke OK"* ]] && [[ $out == *"${want#OK}"* ]]; then
      echo "ok   $name"
      return
    fi ;;
    *) if [ "$status" != 0 ] && [[ $out == *"$want"* ]]; then
      echo "ok   $name"
      return
    fi ;;
  esac
  echo "FAIL $name (salida $status): $out" >&2
  failures=$((failures + 1))
}

check "producción sana" OK
check "el certificado llega a los 10 s" "OKcertificado válido tras 10s" TLS_PENDING=2
check "el certificado llega justo a los 120 s" OK TLS_PENDING=24
check "el certificado no llega en 120 s" "no es válido tras 120s (curl sale con 35" TLS_PENDING=25
check "límite de espera configurable" "no es válido tras 10s (curl sale con 35" TLS_PENDING=99 CRM_SMOKE_TLS_TIMEOUT=10
check "certificado malo" "no es válido tras 120s (curl sale con 60" CERT_BAD=1
check "certificado malo aunque se pase -k" "no es válido tras 120s (curl sale con 60" CERT_BAD=1 -- -k
check "certificado malo con --insecure" "(curl sale con 60" CERT_BAD=1 -- --insecure
check "certificado malo con -sk (ssl_verify_result)" "(curl sale con 0, verificación 18)" CERT_BAD=1 -- -sk
check "PostgreSQL registra las sentencias" "PostgreSQL como crm_app" PG_OUT="crm_app panic all 0 terse log superuser-rejected"
check "PostgreSQL acepta al superusuario" "PostgreSQL como crm_app" PG_OUT="crm_app panic none 0 terse log superuser-accepted"
check "sin tope de cuerpo" "/api/ con 11000001 bytes (length): 200" CODE_BODY=200
check "/demo publicado" "/demo se publica" CODE_DEMO=200
check "/demo que redirige" "/demo se publica" CODE_DEMO=307
check "/health/ready publicado" "/health/ready se publica" CODE_HEALTH_READY=200
check "/health/live publicado" "/health/live se publica" CODE_HEALTH_LIVE=200
check "HTTP sin redirección" "HTTP no redirige a HTTPS" CODE_HTTP=200
check "la query de /webhooks en los logs" "la query de /webhooks aparece en los logs" LEAK=1

[ "$failures" = 0 ] || { echo "test-smoke: $failures fallos" >&2; exit 1; }
echo "test-smoke: smoke.sh pasa con producción sana y falla en cada comprobación"
