#!/usr/bin/env bash
# Backup de producción (F1-16, ADR-033 §8). Lo lanza el temporizador de root crm-backup.timer cada
# noche y, con `predeploy`, deploy.sh antes de migrar (F1-15). Uso: sudo backup.sh [nightly|predeploy]
#   1. Filas por tabla en $state_dir/last-counts.tsv (para comparar al restaurar).
#   2. pg_dump -Fc como el superusuario por el socket local, cifrado al vuelo con age para las claves
#      PÚBLICAS de backup-recipients.txt (propietario y suplente; con predeploy, también las de
#      backup-deploy-recipients.txt). El volcado nunca está en claro en el disco.
#   3. Lo sube a db/<prefijo>/ del destino con una credencial que escribe y no lee ni borra. La
#      retención la aplican las reglas de ciclo de vida del destino (lifecycle.json), no el servidor.
#   4. nightly: copia cifrada (un .age por objeto) de los objetos nuevos del bucket de la aplicación
#      a media/. Lo ya copiado se recuerda en $state_dir/media-copied.txt: el servidor no lista el destino.
#   5. Marca el éxito (check.sh avisa si en 26 h no hay otro) y avisa a BACKUP_HEARTBEAT_URL.
# Si algo falla: log backup.failed y Sentry si SENTRY_DSN (alert en lib.sh); sale con error.
set -euo pipefail
# shellcheck source=infra/prod/backup/lib.sh
. "$(dirname "$0")/lib.sh"

kind=${1:-nightly}
case $kind in nightly | predeploy) ;; *) echo "uso: backup.sh [nightly|predeploy]" >&2; exit 2 ;; esac
stage=config tmp=
finish() {
  local status=$?
  [ -z "$tmp" ] || rm -f "$tmp" "$tmp.list" "$tmp.new"
  [ "$status" = 0 ] || alert backup.failed "kind=$kind stage=$stage"
}
trap finish EXIT
trap 'exit 143' TERM # TimeoutStartSec de systemd: también deja backup.failed

need docker age rclone curl
umask 077
configure
mkdir -p "$state_dir"
recipients=(-R "${BACKUP_RECIPIENTS:-${CRM_ENV_DIR:-/etc/crm}/backup-recipients.txt}")
deploy=${BACKUP_DEPLOY_RECIPIENTS:-${CRM_ENV_DIR:-/etc/crm}/backup-deploy-recipients.txt}
[ "$kind" = nightly ] || [ ! -s "$deploy" ] || recipients+=(-R "$deploy")
for file in "${recipients[@]}"; do [ "$file" = -R ] || check_file "$file"; done
tmp=$(mktemp "$state_dir/.backup.XXXXXX")

stage=counts
table_counts > "$tmp"
mv "$tmp" "$state_dir/last-counts.tsv"

stage=dump
name="crm-$(date -u +%Y%m%dT%H%M%SZ).dump.age"
compose exec -T postgres pg_dump -U postgres -d crm -Fc | age -e "${recipients[@]}" -o "$tmp"
size=$(wc -c < "$tmp" | tr -d ' ')

stage=upload
prefixes=$(retention_prefixes "$kind" "$(date -u +%u)" "$(date -u +%d)")
for prefix in $prefixes; do
  rclone copyto --no-check-dest "$tmp" "DEST:$BACKUP_S3_BUCKET/db/$prefix/$name"
done

copied=0
if [ "$kind" = nightly ]; then
  stage=media
  list="$state_dir/media-copied.txt"
  touch "$list"
  LC_ALL=C sort -u -o "$list" "$list" # una noche que falló a medias lo dejó sin ordenar: comm lo exige
  rclone lsf -R --files-only "SRC:$storage_bucket" | LC_ALL=C sort > "$tmp.list"
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
if [ -n "${BACKUP_HEARTBEAT_URL:-}" ] && [ "$kind" = nightly ]; then
  curl -fsS -m 10 --retry 3 -o /dev/null "$BACKUP_HEARTBEAT_URL" || log backup.heartbeat_failed level=warning
fi
