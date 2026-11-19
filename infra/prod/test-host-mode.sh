#!/bin/sh
# Prueba de host-mode.sh (ADR-046, CI: infra-prod.yml): qué override de Compose sale de cada
# $CRM_ENV_DIR/host-mode, que CRM_COMPOSE_OVERRIDE manda, y que un modo desconocido para el script.
# Uso: sh infra/prod/test-host-mode.sh
set -eu
here=$(cd "$(dirname "$0")" && pwd)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
# shellcheck source=infra/prod/host-mode.sh
. "$here/host-mode.sh"
export CRM_ENV_DIR="$work"
failed=0

check() { # <descripción> <esperado> [contenido de host-mode | -sin-archivo-]
  rm -f "$work/host-mode"
  [ "$3" = -sin-archivo- ] || printf '%s' "$3" > "$work/host-mode"
  if got=$(crm_compose_override /prod 2> /dev/null); then :; else got="ERROR"; fi
  if [ "$got" = "$2" ]; then
    echo "ok   $1"
  else
    echo "FAIL $1: «$got», se esperaba «$2»"
    failed=1
  fi
}

unset CRM_COMPOSE_OVERRIDE || true
check "sin archivo: dedicado" "" -sin-archivo-
check "dedicated" "" "dedicated"
check "vacío: dedicado" "" ""
check "shared" "/prod/compose.shared.yaml" "shared"
check "shared con salto de línea y espacios" "/prod/compose.shared.yaml" " shared
"
check "modo desconocido: para" "ERROR" "compartido"
check "mayúsculas: para" "ERROR" "SHARED"
CRM_COMPOSE_OVERRIDE=/drill/override.yaml
export CRM_COMPOSE_OVERRIDE
check "CRM_COMPOSE_OVERRIDE manda sobre shared" "/drill/override.yaml" "shared"
check "CRM_COMPOSE_OVERRIDE manda sobre un modo inválido" "/drill/override.yaml" "xx"

unset CRM_COMPOSE_OVERRIDE
rm -f "$work/host-mode"
chmod 000 "$work"
if [ "$(id -u)" != 0 ]; then
  if crm_compose_override /prod > /dev/null 2>&1; then
    echo "FAIL directorio ilegible: tendría que parar"
    failed=1
  else
    echo "ok   directorio ilegible: para (no supone dedicado)"
  fi
fi
chmod 700 "$work"

[ "$failed" = 0 ] || exit 1
echo "test-host-mode OK"
