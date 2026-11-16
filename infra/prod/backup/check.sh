#!/usr/bin/env bash
# Aviso de backup que falta (F1-16, ADR-033 §8.4): si el último backup nocturno correcto tiene más de
# 26 h (o no hay ninguno), log backup.missing y Sentry si SENTRY_DSN. Lo lanza crm-backup-check.timer
# cada hora. Si el servidor entero cae, este aviso cae con él: para eso está BACKUP_HEARTBEAT_URL
# (un servicio externo que avisa cuando deja de recibir el ping de backup.sh).
set -euo pipefail
# shellcheck source=infra/prod/backup/lib.sh
. "$(dirname "$0")/lib.sh"

load_env "${BACKUP_ENV:-${CRM_ENV_DIR:-/etc/crm}/backup.env}"
marker="${BACKUP_STATE_DIR:-/var/lib/crm-backup}/last-success"
max=$((${BACKUP_MAX_AGE_HOURS:-26} * 3600))
last=$(cat "$marker" 2> /dev/null || echo 0)
age=$(($(date -u +%s) - last))
if [ "$age" -gt "$max" ]; then
  alert backup.missing "last_success_age_seconds=$age"
  exit 1
fi
log backup.fresh "last_success_age_seconds=$age"
