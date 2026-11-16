#!/usr/bin/env bash
# Restauración (F1-16, ADR-033 §8.1 y §8.4; runbook: docs/runbooks/backup-restore.md). En el servidor
# nuevo, con `postgres` arriba y el runtime parado:
#   sudo BACKUP_ENV=/dev/shm/restore.env restore.sh --identity /dev/shm/<clave privada age> \
#     (--latest | --object db/<prefijo>/crm-….dump.age | --media-only) [--roles-env /etc/crm/postgres.env] \
#     [--media] [--overwrite-media] [--expect-counts <tsv>] [--replace-existing-data]
#   1. Nunca restaura encima de una base con tablas sin --replace-existing-data (borra la base `crm`).
#   2. Crea los roles con bootstrap-roles.sh si recibe --roles-env (CRM_MIGRATOR_PASSWORD y
#      CRM_APP_PASSWORD, root y 0600); si no, exige que ya existan. Ninguna contraseña en argumentos.
#   3. Baja el volcado, lo descifra y lo pasa a pg_restore en una transacción: nada en claro en disco.
#   4. Filas por tabla en $state_dir/restored-counts.tsv; con --expect-counts, deben coincidir.
#   5. Informa si las migraciones están al día (`migrate --check`), levanta el backend (el job migrate
#      corre antes) y exige /health/ready 200.
#   6. --media (o solo eso, --media-only): devuelve al bucket de runtime.env los objetos que no estén
#      ya (todos con --overwrite-media), por un `.partial` que solo pasa a su nombre si todo fue bien.
# La clave privada solo está en el servidor durante la restauración: bórrala al terminar (runbook).
set -euo pipefail
# shellcheck source=infra/prod/backup/lib.sh
. "$(dirname "$0")/lib.sh"

usage() { sed -n '3,6p' "$0" >&2; exit 2; }
identity='' object='' latest='' media='' media_only='' overwrite='' expect='' replace='' roles_env=''
while [ $# -gt 0 ]; do
  case $1 in
    --identity) identity=${2:?}; shift ;;
    --object) object=${2:?}; shift ;;
    --latest) latest=1 ;;
    --media) media=1 ;;
    --media-only) media=1 media_only=1 ;;
    --overwrite-media) overwrite=1 ;;
    --expect-counts) expect=${2:?}; shift ;;
    --replace-existing-data) replace=1 ;;
    --roles-env) roles_env=${2:?}; shift ;;
    *) usage ;;
  esac
  shift
done
if [ ! -r "$identity" ] || [ -z "$object$latest$media_only" ]; then usage; fi
stage=config
trap '[ $? = 0 ] || [ -n "${alerted:-}" ] || alert restore.failed "stage=$stage"' EXIT
need docker age rclone
umask 077
configure restore.failed
check_file "$identity" secret
[ -z "$roles_env" ] || check_file "$roles_env" secret
mkdir -p "$state_dir"
started=$(date +%s)
bootstrap() { # las contraseñas, del archivo al entorno de `docker compose exec -e NOMBRE`: nunca en argv
  [ -n "$roles_env" ] || { echo "faltan las contraseñas de los roles: --roles-env <archivo 0600>" >&2; return 1; }
  CRM_MIGRATOR_PASSWORD=$(env_value "$roles_env" CRM_MIGRATOR_PASSWORD) CRM_APP_PASSWORD=$(env_value "$roles_env" CRM_APP_PASSWORD) \
    compose exec -T -e CRM_MIGRATOR_PASSWORD -e CRM_APP_PASSWORD postgres /docker-entrypoint-initdb.d/10-roles.sh
}

at_head='' tables=0
if [ -z "$media_only" ]; then
  [ -z "$latest" ] || object="db/daily/$(rclone lsf --files-only "DEST:$BACKUP_S3_BUCKET/db/daily/" | LC_ALL=C sort | tail -n 1)"
  [ "$object" != db/daily/ ] || { echo "no hay volcados en db/daily/" >&2; exit 1; }

  stage=empty # antes de tocar los roles: una base con datos no se toca sin --replace-existing-data
  if [ "$(psql_su -d postgres -c "SELECT count(*) FROM pg_database WHERE datname = 'crm'")" = 1 ]; then
    tables=$(psql_su -d crm -c "SELECT count(*) FROM pg_class c JOIN pg_namespace s ON s.oid = c.relnamespace
      WHERE s.nspname = 'public' AND c.relkind IN ('r', 'p')")
  fi
  if [ "$tables" != 0 ] && [ -z "$replace" ]; then
    echo "la base crm tiene $tables tablas: no se restaura encima sin --replace-existing-data" >&2
    exit 1
  fi

  stage=roles
  if [ "$tables" != 0 ]; then
    # Antes de borrar nada: hay contraseñas para recrear los roles y el volcado se baja y se descifra
    # entero (age comprueba cada bloque y el final), así una clave equivocada no deja la base vacía.
    [ -n "$roles_env" ] || bootstrap
    rclone cat "DEST:$BACKUP_S3_BUCKET/$object" | age -d -i "$identity" > /dev/null
    log restore.replacing "tables=$tables"
    compose stop backend ws worker worker-media beat
    psql_su -d postgres -c "DROP DATABASE crm WITH (FORCE)"
    bootstrap
  else
    roles=$(psql_su -d postgres -c "SELECT count(*) FROM pg_roles WHERE rolname IN ('crm_migrator', 'crm_app')")
    if [ "$roles" != 2 ] || [ -n "$roles_env" ]; then bootstrap; fi
  fi

  stage=restore
  rclone cat "DEST:$BACKUP_S3_BUCKET/$object" | age -d -i "$identity" |
    compose exec -T postgres pg_restore -U postgres -d crm --exit-on-error --single-transaction

  stage=counts
  table_counts > "$state_dir/restored-counts.tsv"
  tables=$(wc -l < "$state_dir/restored-counts.tsv" | tr -d ' ')
  [ -z "$expect" ] || diff "$expect" "$state_dir/restored-counts.tsv" > /dev/null ||
    { echo "las filas por tabla no coinciden con $expect" >&2; exit 1; }

  stage=migrate
  at_head=yes
  compose run --rm --no-deps migrate python manage.py migrate --check --no-input > /dev/null || at_head=no
  compose up -d --wait backend

  stage=ready
  compose exec -T backend python -c 'import urllib.request
print(urllib.request.urlopen("http://127.0.0.1:8000/health/ready", timeout=10).read().decode())'
fi

restored=0 skipped=0
if [ -n "$media" ]; then
  stage=media
  list="$state_dir/media-copied.txt" work="$state_dir/.media"
  touch "$list"
  LC_ALL=C sort -u -o "$list" "$list"
  rclone lsf -R --files-only "DEST:$BACKUP_S3_BUCKET/media" | sed 's/\.age$//' | LC_ALL=C sort > "$work.backup"
  if [ -n "$overwrite" ]; then : > "$work.target"; else
    rclone lsf -R --files-only "SRC:$storage_bucket" | LC_ALL=C sort > "$work.target"
  fi
  LC_ALL=C comm -23 "$work.backup" "$work.target" > "$work.todo"
  skipped=$(($(wc -l < "$work.backup") - $(wc -l < "$work.todo")))
  while IFS= read -r key <&3; do
    # rcat guarda lo que recibe, también un flujo vacío o cortado: primero a .partial, y solo si
    # todo el pipeline terminó bien, al nombre final. Nunca pisa un objeto bueno con uno roto.
    if ! { rclone cat "DEST:$BACKUP_S3_BUCKET/media/$key.age" < /dev/null | age -d -i "$identity" |
      rclone rcat "SRC:$storage_bucket/$key.partial"; }; then
      rclone deletefile "SRC:$storage_bucket/$key.partial" 2> /dev/null || true
      exit 1
    fi
    rclone moveto "SRC:$storage_bucket/$key.partial" "SRC:$storage_bucket/$key"
    printf '%s\n' "$key" >> "$list" # el próximo backup no lo vuelve a subir
    restored=$((restored + 1))
  done 3< "$work.todo"
  rm -f "$work.backup" "$work.target" "$work.todo"
  LC_ALL=C sort -u -o "$list" "$list"
fi
stage=finished
log restore.ok "object=${object:-none}" "tables=$tables" "migrations_at_head=${at_head:-skipped}" \
  "media_restored=$restored" "media_skipped=$skipped" "seconds=$(($(date +%s) - started))"
