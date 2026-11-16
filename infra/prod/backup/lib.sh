# shellcheck shell=bash
# Funciones comunes de backup.sh, restore.sh, check.sh y drill.sh (F1-16, ADR-033 §8). Sin efectos al
# cargarlo: retention_prefixes se prueba sola (.github/scripts/test_backup_retention.py).
# Configuración: /etc/crm/backup.env (root, 0600; plantilla en infra/prod/.env.production.example) y
# runtime.env (el bucket de la aplicación). Ningún valor de estos archivos se escribe en un log.

prod_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)

# Los prefijos de un volcado según el día UTC (ISO: 1 = lunes … 7 = domingo). Las reglas de ciclo de
# vida del destino (lifecycle.json) los borran: 7 días el diario, 28 el semanal y 184 el mensual.
retention_prefixes() { # <kind: nightly|predeploy> <día de la semana> <día del mes>
  if [ "$1" = predeploy ]; then echo predeploy; return; fi
  echo daily
  [ "$2" = 7 ] && echo weekly
  [ "$3" = 01 ] && echo monthly
  return 0
}

log() { # <evento> [clave=valor …]: una línea JSON en stderr (journald). Nunca datos ni secretos.
  local out pair
  out=$(printf '{"ts":"%s","event":"%s"' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1")
  shift
  for pair in "$@"; do out+=$(printf ',"%s":"%s"' "${pair%%=*}" "${pair#*=}"); done
  printf '%s}\n' "$out" >&2
}

# Aviso de fallo (ADR-033 §8.4): la línea del log y, con SENTRY_DSN, un evento a Sentry. Un fallo
# del envío no tapa el del backup.
alert() { # <evento> <motivo>
  log "$1" level=error "reason=$2"
  [ -n "${SENTRY_DSN:-}" ] || return 0
  local scheme=${SENTRY_DSN%%://*} rest=${SENTRY_DSN#*://} key host project id
  key=${rest%%@*} rest=${rest#*@} project=${rest##*/} host=${rest%/*}
  key=${key%%:*} id=$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')
  printf '{"event_id":"%s"}\n{"type":"event"}\n{"event_id":"%s","level":"error","logger":"crm.backup","platform":"other","environment":"%s","message":{"formatted":"%s: %s"}}\n' \
    "$id" "$id" "${SENTRY_ENVIRONMENT:-production}" "$1" "$2" |
    curl -fsS -m 10 -o /dev/null -H 'Content-Type: application/x-sentry-envelope' \
      -H "X-Sentry-Auth: Sentry sentry_version=7, sentry_key=$key, sentry_client=crm-backup/1" \
      --data-binary @- "$scheme://$host/api/$project/envelope/" || log alert.sentry_failed level=warning
}

load_env() { # <archivo>: KEY=valor literal, sin evaluar (como EnvironmentFile de systemd)
  local line
  while IFS= read -r line || [ -n "$line" ]; do
    if [[ $line =~ ^[A-Z_][A-Z0-9_]*= ]]; then export "${line%%=*}=${line#*=}"; fi
  done < "$1"
}

env_value() { sed -n "s/^$2=//p" "$1" | tail -n 1; } # <archivo> <clave>

# Un remoto de rclone por variables de entorno: ninguna credencial en argumentos ni en disco.
# Sin HEAD tras subir ni comprobación del bucket: la credencial del servidor solo escribe.
s3_remote() { # <NOMBRE> <endpoint (vacío = AWS)> <región> <access key> <secret>
  local provider=Other
  export RCLONE_CONFIG=/dev/null
  [ -n "$2" ] || provider=AWS
  export "RCLONE_CONFIG_$1_TYPE=s3" "RCLONE_CONFIG_$1_PROVIDER=$provider" "RCLONE_CONFIG_$1_ENDPOINT=$2" \
    "RCLONE_CONFIG_$1_REGION=$3" "RCLONE_CONFIG_$1_ACCESS_KEY_ID=$4" "RCLONE_CONFIG_$1_SECRET_ACCESS_KEY=$5" \
    "RCLONE_CONFIG_$1_NO_HEAD=true" "RCLONE_CONFIG_$1_NO_CHECK_BUCKET=true"
}

# DEST: el destino de los backups (backup.env). SRC: el bucket de la aplicación (runtime.env).
configure() {
  local runtime="${CRM_ENV_DIR:-/etc/crm}/runtime.env"
  load_env "${BACKUP_ENV:-${CRM_ENV_DIR:-/etc/crm}/backup.env}"
  : "${BACKUP_S3_BUCKET:?falta BACKUP_S3_BUCKET}" "${BACKUP_S3_ACCESS_KEY_ID:?falta BACKUP_S3_ACCESS_KEY_ID}"
  : "${BACKUP_S3_SECRET_ACCESS_KEY:?falta BACKUP_S3_SECRET_ACCESS_KEY}"
  s3_remote DEST "${BACKUP_S3_ENDPOINT:-}" "${BACKUP_S3_REGION:-us-east-1}" \
    "$BACKUP_S3_ACCESS_KEY_ID" "$BACKUP_S3_SECRET_ACCESS_KEY"
  storage_bucket=$(env_value "$runtime" STORAGE_BUCKET)
  # BACKUP_STORAGE_ENDPOINT: solo si el servidor llega al almacenamiento por otra URL (el simulacro).
  s3_remote SRC "${BACKUP_STORAGE_ENDPOINT:-$(env_value "$runtime" STORAGE_ENDPOINT_URL)}" \
    "$(env_value "$runtime" STORAGE_REGION)" \
    "$(env_value "$runtime" STORAGE_ACCESS_KEY_ID)" "$(env_value "$runtime" STORAGE_SECRET_ACCESS_KEY)"
  state_dir=${BACKUP_STATE_DIR:-/var/lib/crm-backup}
  export storage_bucket state_dir
}

compose() {
  local files=(-f "$prod_dir/compose.yaml")
  [ -z "${CRM_COMPOSE_OVERRIDE:-}" ] || files+=(-f "$CRM_COMPOSE_OVERRIDE")
  [ -z "${CRM_COMPOSE_PROJECT:-}" ] || files+=(-p "$CRM_COMPOSE_PROJECT")
  docker compose "${files[@]}" --env-file "${CRM_RELEASE_ENV:-/etc/crm/release.env}" "$@"
}

# El superusuario, solo por el socket local del contenedor (pg_hba.conf, ADR-033 §8.1).
psql_su() { compose exec -T postgres psql -X -q -v ON_ERROR_STOP=1 -U postgres -At "$@"; }

# Filas por tabla del esquema public, «tabla<TAB>filas» y ordenado: compara origen y restauración.
table_counts() {
  psql_su -d crm -c "SELECT c.relname || E'\t' || (xpath('/row/n/text()', query_to_xml(
    format('SELECT count(*) AS n FROM public.%I', c.relname), false, true, '')))[1]::text
    FROM pg_class c JOIN pg_namespace s ON s.oid = c.relnamespace
    WHERE s.nspname = 'public' AND c.relkind IN ('r', 'p') ORDER BY 1"
}

need() { local c; for c in "$@"; do command -v "$c" > /dev/null || { echo "falta el comando $c" >&2; return 1; }; done; }
