#!/usr/bin/env bash
# Backup de producción (F1-16, ADR-033 §8). Lo lanza el temporizador de root crm-backup.timer cada
# noche y, con `predeploy`, deploy.sh antes de migrar (F1-15). Uso: sudo backup.sh [nightly|predeploy]
#   1. pg_dump -Fc como el superusuario por el socket local, cifrado al vuelo con age para las claves
#      PÚBLICAS de backup-recipients.txt (propietario y suplente; con predeploy, también las de
#      backup-deploy-recipients.txt). El volcado nunca está en claro en el disco. Las filas por tabla,
#      contadas en la misma instantánea, van cifradas al lado (<volcado>.counts.age).
#   2. Lo sube a db/<prefijo>/ del destino con una credencial que escribe y no lee ni borra. La
#      retención la aplican las reglas de ciclo de vida del destino (lifecycle.json), no el servidor.
#      weekly y monthly, por periodo: la última subida nocturna está en $state_dir/last-period.
#   3. nightly: copia cifrada (un .age por objeto) de los objetos nuevos del bucket de la aplicación
#      a media/, sin los `.partial` de una restauración que falló. Lo ya copiado se recuerda en
#      $state_dir/media-copied.txt: el servidor no lista el destino.
#   4. Marca el éxito (check.sh avisa si en 26 h no hay otro) y avisa a BACKUP_HEARTBEAT_URL.
#      Si una noche falla, BACKUP_HEARTBEAT_FAIL_URL (heartbeat y alert en lib.sh).
# nightly toma antes el lock de los despliegues (deploy_lock en lib.sh): ni un despliegue ni
# `deploy.sh --migrate` corren a la vez que él (F1-15b).
# Si algo falla: log backup.failed y Sentry si SENTRY_DSN (alert en lib.sh); sale con error.
set -euo pipefail
# shellcheck source=infra/prod/backup/lib.sh
. "$(dirname "$0")/lib.sh"

kind=${1:-nightly}
case $kind in nightly | predeploy) ;; *) echo "uso: backup.sh [nightly|predeploy]" >&2; exit 2 ;; esac
stage=config tmp='' alerted='' # alerted: un valor heredado del entorno silenciaría el aviso
finish() {
  local status=$?
  [ -z "$tmp" ] || rm -f "$tmp" "$tmp.counts" "$tmp.list" "$tmp.new"
  [ "$status" = 0 ] || [ -n "$alerted" ] || alert backup.failed "kind=$kind stage=$stage"
}
trap finish EXIT
trap 'exit 143' TERM # TimeoutStartSec de systemd: también deja backup.failed

need docker age rclone curl
umask 077
configure backup.failed
mkdir -p "$state_dir"
held_lock=''
if [ "$kind" = nightly ]; then
  stage=deploy_lock
  trap 'finish; [ -z "$held_lock" ] || rmdir "$held_lock"' EXIT
  deploy_lock
fi
recipients=(-R "${BACKUP_RECIPIENTS:-${CRM_ENV_DIR:-/etc/crm}/backup-recipients.txt}")
deploy=${BACKUP_DEPLOY_RECIPIENTS:-${CRM_ENV_DIR:-/etc/crm}/backup-deploy-recipients.txt}
[ "$kind" = nightly ] || [ ! -s "$deploy" ] || recipients+=(-R "$deploy")
for file in "${recipients[@]}"; do [ "$file" = -R ] || check_file "$file"; done
tmp=$(mktemp "$state_dir/.backup.XXXXXX")

stage=dump
now=$(date -u +%s)
name="crm-$(date -u -d "@$now" +%Y%m%dT%H%M%SZ).dump.age"
# Las filas por tabla, en la instantánea del volcado: una transacción las cuenta, exporta su
# instantánea y sigue abierta mientras pg_dump la usa (--snapshot). Solo en memoria hasta cifrarlas.
coproc SNAP { compose exec -T postgres psql -X -q -v ON_ERROR_STOP=1 -U postgres -At -d crm; }
printf '%s\n' 'BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;' 'SELECT pg_export_snapshot();' "$counts_sql;" \
  "SELECT 'end-of-counts';" >&"${SNAP[1]}"
read -r snapshot <&"${SNAP[0]}"
counts='' line=''
while IFS= read -r line <&"${SNAP[0]}" && [ "$line" != end-of-counts ]; do counts+="$line"$'\n'; done
[ "$line" = end-of-counts ] || { echo "psql terminó antes de contar las filas" >&2; exit 1; }
printf '%s' "$counts" | age -e "${recipients[@]}" -o "$tmp.counts"
compose exec -T postgres pg_dump -U postgres -d crm -Fc --snapshot="$snapshot" | age -e "${recipients[@]}" -o "$tmp"
printf 'COMMIT;\n' >&"${SNAP[1]}"
fd=${SNAP[1]} pid=$SNAP_PID
exec {fd}>&-
wait "$pid"
size=$(wc -c < "$tmp" | tr -d ' ')

stage=upload
period=$(backup_period "$now" "$(date -u -d "@$now" +%Y-%m)")
prefixes=$(retention_prefixes "$kind" "$period" "$(cat "$state_dir/last-period" 2> /dev/null || true)")
for prefix in $prefixes; do
  rclone copyto --no-check-dest "$tmp" "DEST:$BACKUP_S3_BUCKET/db/$prefix/$name"
  rclone copyto --no-check-dest "$tmp.counts" "DEST:$BACKUP_S3_BUCKET/db/$prefix/${name%.dump.age}.counts.age"
done
[ "$kind" = predeploy ] || echo "$period" > "$state_dir/last-period" # este periodo ya tiene sus copias

copied=0
if [ "$kind" = nightly ]; then
  stage=media
  list="$state_dir/media-copied.txt"
  touch "$list"
  LC_ALL=C sort -u -o "$list" "$list" # una noche que falló a medias lo dejó sin ordenar: comm lo exige
  # Un .partial lo deja una restauración de objetos que falló (restore.sh): no es un objeto.
  rclone lsf -R --files-only --exclude '*.partial' "SRC:$storage_bucket" | LC_ALL=C sort > "$tmp.list"
  LC_ALL=C comm -13 "$list" "$tmp.list" > "$tmp.new"
  while IFS= read -r key <&3; do
    rclone cat "SRC:$storage_bucket/$key" < /dev/null | age -e "${recipients[@]}" -o "$tmp"
    rclone copyto --no-check-dest "$tmp" "DEST:$BACKUP_S3_BUCKET/media/$key.age"
    printf '%s\n' "$key" >> "$list"
    copied=$((copied + 1))
  done 3< "$tmp.new"
  LC_ALL=C sort -u -o "$list" "$list"
fi

stage=finished
[ "$kind" = predeploy ] || date -u +%s > "$state_dir/last-success"
log backup.ok "kind=$kind" "object=$name" "bytes=$size" "prefixes=$(echo "$prefixes" | tr '\n' ' ' | sed 's/ $//')" \
  "media_copied=$copied"
[ "$kind" = predeploy ] || heartbeat "${BACKUP_HEARTBEAT_URL:-}"
