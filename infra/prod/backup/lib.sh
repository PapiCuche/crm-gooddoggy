# shellcheck shell=bash
# Funciones comunes de backup.sh, restore.sh, check.sh y drill.sh (F1-16, ADR-033 §8). Sin efectos al
# cargarlo: retention_prefixes se prueba sola (.github/scripts/test_backup_retention.py).
# Configuración: /etc/crm/backup.env (root, 0600; plantilla en infra/prod/.env.production.example) y
# runtime.env (el bucket de la aplicación). Ningún valor de estos archivos se escribe en un log.

prod_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)

# El periodo de una noche, «semana mes»: la semana empieza en domingo (el día 0 de la época, el
# 1970-01-01, fue jueves) y el mes, el día 1.
backup_period() { echo "$((($1 / 86400 + 4) / 7)) $2"; } # <segundos UTC> <AAAA-MM>

# Los prefijos de un volcado. nightly: daily siempre, y weekly y monthly si su periodo aún no tiene
# copia; así, si falla la noche del domingo o la del día 1, la siguiente noche buena hace la copia.
# Las reglas de ciclo de vida del destino (lifecycle.json) los borran: 7 días el diario, 28 el
# semanal y 184 el mensual.
retention_prefixes() { # <kind: nightly|predeploy> <periodo de esta noche> <el de la última subida nocturna>
  if [ "$1" = predeploy ]; then echo predeploy; return; fi
  echo daily
  [ "${2% *}" = "${3% *}" ] || echo weekly
  [ "${2#* }" = "${3#* }" ] || echo monthly
  return 0
}

log() { # <evento> [clave=valor …]: una línea JSON en stderr (journald). Nunca datos ni secretos.
  local out pair
  out=$(printf '{"ts":"%s","event":"%s"' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1")
  shift
  for pair in "$@"; do out+=$(printf ',"%s":"%s"' "${pair%%=*}" "${pair#*=}"); done
  printf '%s}\n' "$out" >&2
}

# Señal a un servicio externo de latidos (F1-16c): una petición GET sin cuerpo ni datos. La URL lleva
# el token del servicio: a curl por stdin (--config -), nunca en sus argumentos (ps), y sin la salida
# de error de curl en el log. Si no llega, solo un aviso: ni el código de salida ni el estado cambian.
heartbeat() { # <URL; vacía: nada>
  [ -n "$1" ] || return 0
  printf '%s\n' "$1" | sed 's/[\\"]/\\&/g; s/.*/url = "&"/' |
    curl -fs -m 10 --retry 2 --proto =http,https -o /dev/null --config - 2> /dev/null ||
    log backup.heartbeat_failed level=warning "curl_exit=$?"
}

# Aviso de fallo (ADR-033 §8.4): la línea del log; con SENTRY_DSN, un evento a Sentry; y si falla o
# falta el backup nocturno, BACKUP_HEARTBEAT_FAIL_URL (no el de predeploy: deploy.sh ya se para).
# Un fallo del envío no tapa el del backup.
alert() { # <evento> <motivo>
  log "$1" level=error "reason=$2"
  case $1 in backup.failed | backup.missing) [ "${kind:-}" = predeploy ] || heartbeat "${BACKUP_HEARTBEAT_FAIL_URL:-}" ;; esac
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

# Un archivo que los scripts leen: de root (o de quien ejecuta, para el simulacro sin root) y sin
# escritura de grupo ni otros; con `secret`, además sin lectura (0600 o más estricto).
check_file() { # <archivo> [secret]
  local info uid mode mask=022
  info=$(stat -c '%u %a' "$1" 2> /dev/null || stat -f '%u %Lp' "$1" 2> /dev/null) || { echo "no existe $1" >&2; return 1; }
  uid=${info% *} mode=${info#* }
  [ "${2:-}" != secret ] || mask=077
  if [ "$uid" != 0 ] && [ "$uid" != "$(id -u)" ]; then echo "$1 no es de root" >&2; return 1; fi
  if (((8#$mode & 8#$mask) != 0)); then echo "$1 tiene modo $mode: se espera 0600 o sin escritura ajena" >&2; return 1; fi
}

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

# backup.env, de root y 0600. Si no lo es, el aviso sale igual (reason=config_permissions) y el script
# termina. A Sentry y a BACKUP_HEARTBEAT_FAIL_URL solo si nadie más puede escribir el archivo: si no,
# sus URL no son de fiar, y tampoco las heredadas del entorno.
secure_config() { # <evento del aviso>
  local config="${BACKUP_ENV:-${CRM_ENV_DIR:-/etc/crm}/backup.env}"
  if check_file "$config" secret; then load_env "$config"; return 0; fi
  SENTRY_DSN='' BACKUP_HEARTBEAT_FAIL_URL=''
  if check_file "$config" 2> /dev/null; then
    SENTRY_DSN=$(env_value "$config" SENTRY_DSN) SENTRY_ENVIRONMENT=$(env_value "$config" SENTRY_ENVIRONMENT)
    BACKUP_HEARTBEAT_FAIL_URL=$(env_value "$config" BACKUP_HEARTBEAT_FAIL_URL)
  fi
  # shellcheck disable=SC2034 # lo leen los traps de salida de backup.sh y restore.sh: no repiten el aviso
  alerted=1
  alert "$1" config_permissions
  exit 1
}

# DEST: el destino de los backups (backup.env). SRC: el bucket de la aplicación (runtime.env).
configure() { # <evento del aviso si backup.env no es seguro>
  local runtime="${CRM_ENV_DIR:-/etc/crm}/runtime.env"
  secure_config "$1"
  check_file "$runtime" secret
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

# shellcheck source=infra/prod/host-mode.sh
. "$prod_dir/host-mode.sh"
# shellcheck source=infra/prod/host-lock.sh
. "$prod_dir/host-lock.sh"
compose() {
  local files=(-f "$prod_dir/compose.yaml") override
  # CRM_COMPOSE_OVERRIDE (el simulacro) o, si no, el modo del servidor (ADR-046): host-mode.sh.
  override=$(crm_compose_override "$prod_dir") || return 1
  [ -z "$override" ] || files+=(-f "$override")
  [ -z "${CRM_COMPOSE_PROJECT:-}" ] || files+=(-p "$CRM_COMPOSE_PROJECT")
  docker compose "${files[@]}" --env-file "${CRM_RELEASE_ENV:-/etc/crm/release.env}" "$@"
}

# El superusuario, solo por el socket local del contenedor (pg_hba.conf, ADR-033 §8.1).
psql_su() { compose exec -T postgres psql -X -q -v ON_ERROR_STOP=1 -U postgres -At "$@"; }

# Filas por tabla del esquema public, «tabla<TAB>filas» y ordenado: compara origen y restauración.
counts_sql="SELECT c.relname || E'\\t' || (xpath('/row/n/text()', query_to_xml(
  format('SELECT count(*) AS n FROM public.%I', c.relname), false, true, '')))[1]::text
  FROM pg_class c JOIN pg_namespace s ON s.oid = c.relnamespace
  WHERE s.nspname = 'public' AND c.relkind IN ('r', 'p') ORDER BY 1"
table_counts() { psql_su -d crm -c "$counts_sql"; }

# El backup nocturno y deploy.sh no se pisan (F1-15b): los dos toman /var/lib/crm-deploy/lock, un
# directorio (mkdir es atómico). Si está puesto, el nocturno espera hasta BACKUP_LOCK_WAIT s (3600)
# a que el despliegue termine; si sigue (un despliegue cortado lo deja), falla y avisa. `predeploy`
# no lo toma: lo lanza deploy.sh, que ya lo tiene. Deja en lock_wait_left lo que queda de la espera,
# para el lock común (host-lock.sh): las dos esperas juntas no pasan de BACKUP_LOCK_WAIT.
deploy_lock() {
  local dir=${CRM_DEPLOY_DIR:-/var/lib/crm-deploy} waited=0
  mkdir -p "$dir"
  chmod 700 "$dir"
  until mkdir "$dir/lock" 2> /dev/null; do
    if [ "$waited" -ge "${BACKUP_LOCK_WAIT:-3600}" ]; then
      echo "$dir/lock sigue puesto tras ${waited}s: un despliegue en curso o cortado ($dir/deploy.log)" >&2
      return 1
    fi
    sleep 30
    waited=$((waited + 30))
  done
  # shellcheck disable=SC2034 # lo suelta release_lock de backup.sh; lock_wait_left, para host_lock
  held_lock=$dir/lock lock_wait_left=$((${BACKUP_LOCK_WAIT:-3600} - waited))
}

need() { local c; for c in "$@"; do command -v "$c" > /dev/null || { echo "falta el comando $c" >&2; return 1; }; done; }
