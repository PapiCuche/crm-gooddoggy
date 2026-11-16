#!/usr/bin/env bash
# Simulacro de backup y restauración con el compose de producción (F1-16; CI: infra-prod.yml). Monta
# un origen desechable con datos y archivos, hace el backup a un Garage local con una credencial que
# solo escribe, pierde el «servidor», restaura en un stack nuevo con la clave privada y comprueba las
# filas por tabla, las migraciones al día, /health/ready y que los archivos son idénticos. Mide el
# tiempo de la restauración frente al RTO de 4 h (ADR-033 §8.3). Claves age y contraseñas desechables.
# Uso: BACKEND_IMAGE=<imagen del backend del repo> bash infra/prod/backup/drill.sh
# Crea y borra al salir, por nombre: los proyectos $id-src y $id-dst (down -v), los contenedores
# $id-garage y $id-s3tls, la red $id-s3 y un directorio temporal.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../../.." && pwd)
id=${DRILL_ID:-crm-drill}
port=${DRILL_S3_PORT:-39470}
s3="http://127.0.0.1:$port"
: "${BACKEND_IMAGE:?imagen del backend construida del repo}"
work=$(mktemp -d)
# shellcheck source=infra/prod/backup/lib.sh
. "$here/lib.sh"
hex() { od -An -N"$1" -tx1 /dev/urandom | tr -d ' \n'; }
uuid() { local h; h=$(hex 16); echo "${h:0:8}-${h:8:4}-${h:12:4}-${h:16:4}-${h:20:12}"; }
fail() { echo "drill FAIL: $*" >&2; exit 1; }
stack() { # <src|dst> <comando de compose…>
  local p=$1
  shift
  CRM_COMPOSE_PROJECT="$id-$p" CRM_COMPOSE_OVERRIDE="$work/override.yaml" CRM_RELEASE_ENV="$work/$p/release.env" \
    CRM_ENV_DIR="$work/$p" BACKUP_STATE_DIR="$work/state-$p" "$@"
}
cleanup() {
  set +e
  for p in src dst; do
    [ -f "$work/$p/release.env" ] && stack "$p" compose down -v --remove-orphans > /dev/null 2>&1
  done
  docker rm -f "$id-garage" "$id-s3tls" > /dev/null 2>&1
  docker network rm "$id-s3" > /dev/null 2>&1
  rm -rf "$work"
}
trap cleanup EXIT

echo "== S3 local: Garage, buckets y claves (la del servidor solo escribe en el destino)"
docker network create "$id-s3" > /dev/null
app_key="GK$(hex 12)" app_secret=$(hex 32) writer="GK$(hex 12)" writer_secret=$(hex 32)
reader="GK$(hex 12)" reader_secret=$(hex 32)
docker run -d --name "$id-garage" --network "$id-s3" -p "127.0.0.1:$port:3900" \
  -e GARAGE_RPC_SECRET="$(hex 32)" -e GARAGE_DEFAULT_ACCESS_KEY="$app_key" -e GARAGE_DEFAULT_SECRET_KEY="$app_secret" \
  -e GARAGE_DEFAULT_BUCKET=prod-media -v "$repo/infra/docker/garage/garage.toml:/etc/garage.toml:ro" \
  dxflrs/garage:v2.4.1 /garage server --single-node --default-bucket > /dev/null
g() { docker exec -e RUST_LOG=warn "$id-garage" /garage "$@" > /dev/null; }
for _ in $(seq 60); do g bucket info prod-media 2> /dev/null && break; sleep 1; done
g bucket create backups
g bucket create restored-media
g key import --yes -n writer "$writer" "$writer_secret"
g key import --yes -n reader "$reader" "$reader_secret"
g bucket allow --write backups --key writer
g bucket allow --read backups --key reader
g bucket allow --read --write restored-media --key "$app_key"
# production exige https para el almacenamiento: Caddy con su CA interna delante de Garage.
docker run -d --name "$id-s3tls" --network "$id-s3" --network-alias s3.drill.internal caddy:2.11.7-alpine \
  caddy reverse-proxy --from s3.drill.internal --to "$id-garage:3900" --internal-certs > /dev/null
for _ in $(seq 60); do
  docker cp "$id-s3tls:/data/caddy/pki/authorities/local/root.crt" "$work/ca.crt" 2> /dev/null && break
  sleep 1
done
chmod 644 "$work/ca.crt"

cat > "$work/override.yaml" << EOF
services:
  backend:
    networks: { s3: {} }
    volumes: ["$work/ca.crt:/etc/drill/ca.crt:ro"]
networks:
  s3: { name: $id-s3, external: true }
EOF
kek="1:$(head -c 32 /dev/urandom | base64 | tr '+/' '-_')" secret_key=$(hex 32)
mkstack() { # <src|dst> <tercer octeto de la red del borde> <bucket>
  local d="$work/$1" mig app redis
  mig=$(hex 24) app=$(hex 24) redis=$(hex 16)
  mkdir -p "$d"
  printf '%s\n' "BACKEND_IMAGE=$BACKEND_IMAGE" FRONTEND_IMAGE=example.invalid/unused PROXY_IMAGE=example.invalid/unused \
    "POSTGRES_IMAGE=${DRILL_POSTGRES_IMAGE:-postgres:18.6}" "REDIS_IMAGE=${DRILL_REDIS_IMAGE:-redis:8.8.3-alpine}" \
    "CRM_ENV_DIR=$d" "STACK_SUBNET=172.31.$2.0/24" "STACK_IP_RANGE=172.31.$2.128/25" "STACK_PROXY_IP=172.31.$2.2" \
    > "$d/release.env"
  printf '%s\n' "POSTGRES_PASSWORD=$(hex 24)" "CRM_MIGRATOR_PASSWORD=$mig" "CRM_APP_PASSWORD=$app" > "$d/postgres.env"
  printf '%s\n' "DATABASE_MIGRATOR_URL=postgres://crm_migrator:$mig@postgres:5432/crm" > "$d/migrate.env"
  printf '%s\n' "REDIS_PASSWORD=$redis" > "$d/redis.env"
  printf '%s\n' CRM_DOMAIN=localhost > "$d/proxy.env"
  printf '%s\n' "DJANGO_SECRET_KEY=$secret_key" DJANGO_ALLOWED_HOSTS=crm.localhost,backend \
    DJANGO_CSRF_TRUSTED_ORIGINS=https://crm.localhost APP_ORIGIN=https://crm.localhost \
    "DATABASE_URL=postgres://crm_app:$app@postgres:5432/crm" "SECRETS_KEKS=$kek" \
    "CELERY_BROKER_URL=redis://:$redis@redis:6379/0" "CHANNEL_LAYER_REDIS_URL=redis://:$redis@redis:6379/1" \
    STORAGE_ENDPOINT_URL=https://s3.drill.internal STORAGE_REGION=garage "STORAGE_BUCKET=$3" \
    "STORAGE_ACCESS_KEY_ID=$app_key" "STORAGE_SECRET_ACCESS_KEY=$app_secret" AWS_CA_BUNDLE=/etc/drill/ca.crt \
    META_ENABLED=false > "$d/runtime.env"
}
mkstack src "${DRILL_SUBNET_SRC:-157}" prod-media
mkstack dst "${DRILL_SUBNET_DST:-158}" restored-media
age-keygen -o "$work/identity.txt" 2> /dev/null
age-keygen -y "$work/identity.txt" > "$work/recipients.txt"
printf '%s\n' "BACKUP_S3_ENDPOINT=$s3" BACKUP_S3_REGION=garage BACKUP_S3_BUCKET=backups \
  "BACKUP_STORAGE_ENDPOINT=$s3" "BACKUP_RECIPIENTS=$work/recipients.txt" > "$work/backup.env"
cp "$work/backup.env" "$work/restore.env"
printf '%s\n' "BACKUP_S3_ACCESS_KEY_ID=$writer" "BACKUP_S3_SECRET_ACCESS_KEY=$writer_secret" >> "$work/backup.env"
printf '%s\n' "BACKUP_S3_ACCESS_KEY_ID=$reader" "BACKUP_S3_SECRET_ACCESS_KEY=$reader_secret" >> "$work/restore.env"

echo "== Origen: stack de producción, una organización y tres archivos"
stack src compose up -d --wait --quiet-pull backend
hex 16 | stack src compose exec -T backend python manage.py bootstrap_organization --slug drill --name Drill \
  --owner-email owner@drill.example.com --reason "simulacro F1-16" --password-stdin
s3_remote APP "$s3" garage "$app_key" "$app_secret"
org=$(uuid)
for size in 2048 300000 6000000; do
  head -c "$size" /dev/urandom | rclone rcat "APP:prod-media/org/$org/attachment/2026/11/$(uuid)"
done

echo "== Backup (la alerta primero: un backup que falla deja backup.failed)"
if stack src env CRM_COMPOSE_PROJECT="$id-missing" BACKUP_ENV="$work/backup.env" bash "$here/backup.sh" 2> "$work/failed.log"; then
  fail "un backup sin base no falló"
fi
grep -q '"event":"backup.failed"' "$work/failed.log" || fail "sin backup.failed en el log"
stack src env BACKUP_ENV="$work/backup.env" bash "$here/backup.sh" nightly
s3_remote WRITER "$s3" garage "$writer" "$writer_secret"
s3_remote READER "$s3" garage "$reader" "$reader_secret"
if rclone lsf WRITER:backups/db/daily/ > /dev/null 2>&1; then fail "la credencial del servidor lee el destino"; fi
object=$(rclone lsf READER:backups/db/daily/)
[ "$(rclone cat --count 21 "READER:backups/db/daily/$object")" = age-encryption.org/v1 ] || fail "el volcado no está cifrado"
[ "$(rclone lsf -R --files-only READER:backups/media | grep -c '\.age$')" = 3 ] || fail "faltan archivos en el destino"
if stack src env BACKUP_ENV="$work/backup.env" bash "$here/check.sh" 2> /dev/null; then :; else fail "check.sh tras un backup"; fi

echo "== Servidor nuevo: postgres vacío y restauración"
started=$(date +%s)
stack dst compose up -d --wait --quiet-pull postgres
restore() {
  stack dst env BACKUP_ENV="$work/restore.env" CRM_MIGRATOR_PASSWORD="$(sed -n 's/^CRM_MIGRATOR_PASSWORD=//p' "$work/dst/postgres.env")" \
    CRM_APP_PASSWORD="$(sed -n 's/^CRM_APP_PASSWORD=//p' "$work/dst/postgres.env")" bash "$here/restore.sh" \
    --identity "$work/identity.txt" --latest --expect-counts "$work/state-src/last-counts.tsv" "$@"
}
restore --media 2>&1 | tee "$work/restore.log"
rto=$(($(date +%s) - started))
grep -q '"migrations_at_head":"yes"' "$work/restore.log" || fail "quedaban migraciones por aplicar"
grep -q '"status": "ok"' "$work/restore.log" || fail "/health/ready no está ok"
rclone check --download APP:prod-media APP:restored-media || fail "los archivos restaurados no son idénticos"
[ "$(awk -F '\t' '$1 == "organizations" {print $2}' "$work/state-dst/restored-counts.tsv")" = 1 ] ||
  fail "la organización del origen no está"

echo "== Una base con datos no se pisa sin --replace-existing-data, ni con una clave equivocada"
if restore > /dev/null 2> "$work/refused.log"; then fail "restauró encima de una base con datos"; fi
grep -q 'no se restaura encima' "$work/refused.log" || fail "la negativa no se explica"
age-keygen -o "$work/wrong.txt" 2> /dev/null
if restore --replace-existing-data --identity "$work/wrong.txt" > /dev/null 2>&1; then fail "restauró con otra clave"; fi
stack dst table_counts | diff -q - "$work/state-src/last-counts.tsv" > /dev/null || fail "una clave equivocada tocó la base"
restore --replace-existing-data 2>&1 | grep -q '"event":"restore.ok"' || fail "--replace-existing-data no restauró"
echo "drill OK: restauración en ${rto}s (RTO de ADR-033: 14400 s), $(wc -l < "$work/state-dst/restored-counts.tsv" | tr -d ' ') tablas idénticas, 3 archivos idénticos"
