#!/usr/bin/env bash
# Aviso de backup que falta (F1-16, ADR-033 §8.4): si el último backup nocturno correcto tiene más de
# 26 h (o no hay ninguno), log backup.missing, Sentry si SENTRY_DSN y BACKUP_HEARTBEAT_FAIL_URL (alert
# en lib.sh). Lo lanza crm-backup-check.timer cada hora. Si el servidor entero cae, este aviso cae con
# él: para eso está BACKUP_HEARTBEAT_URL (un servicio externo que avisa cuando deja de recibir el
# ping de backup.sh).
set -euo pipefail
# shellcheck source=infra/prod/backup/lib.sh
. "$(dirname "$0")/lib.sh"

kind=nightly # vigila el backup nocturno: un kind heredado del entorno no calla su aviso (alert)
secure_config backup.missing # con permisos inseguros, también avisa
marker="${BACKUP_STATE_DIR:-/var/lib/crm-backup}/last-success"
max=$((${BACKUP_MAX_AGE_HOURS:-26} * 3600))
last=$(cat "$marker" 2> /dev/null || echo 0)
case $last in '' | *[!0-9]*)
  alert backup.missing "last_success=invalid"
  exit 1
  ;;
esac
age=$(($(date -u +%s) - last))
if [ "$age" -gt "$max" ]; then
  alert backup.missing "last_success_age_seconds=$age"
  exit 1
fi
log backup.fresh "last_success_age_seconds=$age"
