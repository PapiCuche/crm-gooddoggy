#!/usr/bin/env bash
# Restauración (F1-16, ADR-033 §8.1 y §8.4; runbook: docs/runbooks/backup-restore.md). En el servidor
# nuevo, con `postgres` arriba y el runtime parado:
#   sudo BACKUP_ENV=<archivo con la credencial de LECTURA del propietario> \
#     [CRM_MIGRATOR_PASSWORD=… CRM_APP_PASSWORD=…] restore.sh --identity <clave privada age> \
#     (--latest | --object db/<prefijo>/crm-….dump.age) [--media] [--expect-counts <tsv>] [--replace-existing-data]
#   1. Nunca restaura encima de una base con tablas sin --replace-existing-data (borra la base `crm`).
#   2. Crea los roles con bootstrap-roles.sh si recibe sus contraseñas; si no, exige que ya existan.
#   3. Baja el volcado, lo descifra y lo pasa a pg_restore en una transacción: nada en claro en disco.
#   4. Filas por tabla en $state_dir/restored-counts.tsv; con --expect-counts, deben coincidir.
#   5. Informa si las migraciones están al día (`migrate --check`), levanta el backend (el job migrate
#      corre antes) y exige /health/ready 200. Con --media, devuelve los objetos al bucket de runtime.env.
# La clave privada solo está en el servidor durante la restauración: bórrala al terminar (runbook).
set -euo pipefail
# shellcheck source=infra/prod/backup/lib.sh
. "$(dirname "$0")/lib.sh"

usage() { sed -n '3,6p' "$0" >&2; exit 2; }
identity='' object='' latest='' media='' expect='' replace=''
while [ $# -gt 0 ]; do
  case $1 in
    --identity) identity=${2:?}; shift ;;
    --object) object=${2:?}; shift ;;
    --latest) latest=1 ;;
    --media) media=1 ;;
    --expect-counts) expect=${2:?}; shift ;;
    --replace-existing-data) replace=1 ;;
    *) usage ;;
  esac
  shift
done
if [ ! -r "$identity" ] || [ -z "$object$latest" ]; then usage; fi
stage=config
trap '[ $? = 0 ] || alert restore.failed "stage=$stage"' EXIT
need docker age rclone
umask 077
configure
mkdir -p "$state_dir"
started=$(date +%s)
[ -z "$latest" ] || object="db/daily/$(rclone lsf --files-only "DEST:$BACKUP_S3_BUCKET/db/daily/" | LC_ALL=C sort | tail -n 1)"
[ "$object" != db/daily/ ] || { echo "no hay volcados en db/daily/" >&2; exit 1; }

stage=empty # antes de tocar los roles: una base con datos no se toca sin --replace-existing-data
tables=0
if [ "$(psql_su -d postgres -c "SELECT count(*) FROM pg_database WHERE datname = 'crm'")" = 1 ]; then
  tables=$(psql_su -d crm -c "SELECT count(*) FROM pg_class c JOIN pg_namespace s ON s.oid = c.relnamespace
    WHERE s.nspname = 'public' AND c.relkind IN ('r', 'p')")
fi
if [ "$tables" != 0 ] && [ -z "$replace" ]; then
  echo "la base crm tiene $tables tablas: no se restaura encima sin --replace-existing-data" >&2
  exit 1
fi

stage=roles
if [ -n "${CRM_MIGRATOR_PASSWORD:-}" ] && [ -n "${CRM_APP_PASSWORD:-}" ]; then
  bootstrap() { compose exec -T -e CRM_MIGRATOR_PASSWORD -e CRM_APP_PASSWORD postgres /docker-entrypoint-initdb.d/10-roles.sh; }
else
  bootstrap() { echo "faltan CRM_MIGRATOR_PASSWORD y CRM_APP_PASSWORD para crear los roles" >&2; return 1; }
fi
if [ "$tables" != 0 ]; then
  # Antes de borrar nada: hay contraseñas para recrear los roles y el volcado se baja y se descifra
  # entero (age comprueba cada bloque y el final), así una clave equivocada no deja la base vacía.
  [ -n "${CRM_APP_PASSWORD:-}" ] && [ -n "${CRM_MIGRATOR_PASSWORD:-}" ] || bootstrap
  rclone cat "DEST:$BACKUP_S3_BUCKET/$object" | age -d -i "$identity" > /dev/null
  log restore.replacing "tables=$tables"
  compose stop backend ws worker worker-media beat
  psql_su -d postgres -c "DROP DATABASE crm WITH (FORCE)"
  bootstrap
else
  roles=$(psql_su -d postgres -c "SELECT count(*) FROM pg_roles WHERE rolname IN ('crm_migrator', 'crm_app')")
  if [ "$roles" != 2 ] || [ -n "${CRM_APP_PASSWORD:-}" ]; then bootstrap; fi
fi

stage=restore
rclone cat "DEST:$BACKUP_S3_BUCKET/$object" | age -d -i "$identity" |
  compose exec -T postgres pg_restore -U postgres -d crm --exit-on-error --single-transaction

stage=counts
table_counts > "$state_dir/restored-counts.tsv"
[ -z "$expect" ] || diff "$expect" "$state_dir/restored-counts.tsv" > /dev/null ||
  { echo "las filas por tabla no coinciden con $expect" >&2; exit 1; }

stage=migrate
at_head=yes
compose run --rm --no-deps migrate python manage.py migrate --check --no-input > /dev/null || at_head=no
compose up -d --wait backend

stage=ready
compose exec -T backend python -c 'import urllib.request
print(urllib.request.urlopen("http://127.0.0.1:8000/health/ready", timeout=10).read().decode())'

restored=0
if [ -n "$media" ]; then
  stage=media
  rclone lsf -R --files-only "DEST:$BACKUP_S3_BUCKET/media" > "$state_dir/.media.list"
  while IFS= read -r rel <&3; do
    rclone cat "DEST:$BACKUP_S3_BUCKET/media/$rel" < /dev/null | age -d -i "$identity" |
      rclone rcat "SRC:$storage_bucket/${rel%.age}"
    printf '%s\n' "${rel%.age}" >> "$state_dir/media-copied.txt" # el próximo backup no la vuelve a subir
    restored=$((restored + 1))
  done 3< "$state_dir/.media.list"
  rm -f "$state_dir/.media.list"
  LC_ALL=C sort -u -o "$state_dir/media-copied.txt" "$state_dir/media-copied.txt"
fi
stage=finished
log restore.ok "object=$object" "tables=$(wc -l < "$state_dir/restored-counts.tsv" | tr -d ' ')" \
  "migrations_at_head=$at_head" "media_restored=$restored" "seconds=$(($(date +%s) - started))"
