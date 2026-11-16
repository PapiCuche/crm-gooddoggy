#!/usr/bin/env bash
# Aviso de un temporizador que falló (F1-15b): crm-migrate.service lo llama desde ExecStopPost=.
# Una línea en el journal y, con SENTRY_DSN en backup.env, un evento a Sentry (alert en lib.sh).
# Uso: alert.sh <evento> <motivo>
set -euo pipefail
# shellcheck source=infra/prod/backup/lib.sh
. "$(dirname "$0")/lib.sh"

secure_config "$1" # con permisos inseguros, también avisa
alert "$1" "$2"
