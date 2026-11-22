#!/bin/sh
# `docker compose` con los archivos de Compose de ESTE servidor (ADR-046): compose.yaml y, si
# /etc/crm/host-mode dice `shared`, también compose.shared.yaml. Los runbooks lo usan para toda orden
# a mano: en un servidor compartido, un `up` solo con compose.yaml vería la red `data` distinta,
# pararía PostgreSQL y no podría recrearla.
# Uso: sudo infra/prod/compose.sh [--env-file <release.env>] <orden de compose…>
#      (por omisión, --env-file /etc/crm/release.env)
set -eu
here=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=infra/prod/host-mode.sh
. "$here/host-mode.sh"
override=$(crm_compose_override "$here")
release=/etc/crm/release.env
if [ "${1:-}" = --env-file ]; then
  release=${2:?uso: compose.sh [--env-file <release.env>] <orden>}
  shift 2
fi
if [ -n "$override" ]; then
  exec docker compose -f "$here/compose.yaml" -f "$override" --env-file "$release" "$@"
fi
exec docker compose -f "$here/compose.yaml" --env-file "$release" "$@"
