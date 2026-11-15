#!/usr/bin/env bash
# Prueba de deploy.sh sin servidor (F1-15, CI: infra-prod.yml): copia el script a un directorio
# temporal con un smoke.sh falso y pone delante en el PATH un `docker`, un `age` y un `id` falsos
# que anotan cada llamada. Comprueba el orden (pull, volcado, migrate, up, puerta), que un fallo de
# la puerta vuelve solo a la versión anterior, que un fallo de migrate no toca el runtime, y las
# negativas: sin root, imagen sin digest, variable ajena y despliegue a medias (lock).
# Uso: bash infra/prod/test-deploy.sh
set -euo pipefail
src=$(cd "$(dirname "$0")" && pwd)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
mkdir -p "$work/infra/prod" "$work/bin" "$work/etc"
cp "$src/deploy.sh" "$src/compose.yaml" "$work/infra/prod/"
cat > "$work/infra/prod/smoke.sh" <<'EOF'
#!/bin/sh
echo "smoke $CRM_RELEASE_ENV" >> "$CALLS"
EOF

# docker falso: anota «<letra del release> <subcomando>» (la letra sale del digest del backend).
cat > "$work/bin/docker" <<'EOF'
#!/usr/bin/env bash
set -eu
release="" args=()
while [ $# -gt 0 ]; do
  case $1 in
    compose) ;;
    -f) shift ;;
    --env-file) release=$(sed -n 's/^BACKEND_IMAGE=.*@sha256:\(.\).*/\1/p' "$2"); shift ;;
    *) args+=("$1") ;;
  esac
  shift
done
echo "$release ${args[*]}" >> "$CALLS"
case "$release ${args[*]}" in
  *"ps -q --status running postgres") echo pg-container ;;
  *"pg_dump"*) echo dump-bytes ;;
  "$FAIL_RUN "*"run --rm --no-deps migrate") exit 1 ;;
  "$FAIL_READY exec -T backend python"*) exit 1 ;;
esac
EOF
cat > "$work/bin/age" <<'EOF'
#!/bin/sh
echo "age $*" >> "$CALLS"
cat
EOF
cat > "$work/bin/id" <<'EOF'
#!/bin/sh
echo "${FAKE_UID:-0}"
EOF
chmod +x "$work/bin/"* "$work/infra/prod/"*.sh
echo "age1example" > "$work/etc/backup-recipients.txt"
echo "CRM_DOMAIN=example.invalid" > "$work/etc/proxy.env"

release() { # release <letra>: las cinco imágenes con un digest de esa letra
  for name in BACKEND FRONTEND PROXY POSTGRES REDIS; do
    echo "${name}_IMAGE=example.invalid/$(echo "$name" | tr '[:upper:]' '[:lower:]')@sha256:$(printf "$1%.0s" $(seq 64))"
  done
}
release a > "$work/a.env"
release b > "$work/b.env"
release c > "$work/c.env"

export CALLS="$work/calls" CRM_ENV_DIR="$work/etc" CRM_DEPLOY_DIR="$work/state" CRM_HEALTH_TIMEOUT=10
export PATH="$work/bin:$PATH" FAIL_RUN=none FAIL_READY=none
deploy() { : > "$CALLS"; "$work/infra/prod/deploy.sh" "$@" > "$work/out" 2>&1; }
fail() { echo "test-deploy FAIL: $*" >&2; cat "$work/out" >&2; exit 1; }
letter() { sed -n 's/^BACKEND_IMAGE=.*@sha256:\(.\).*/\1/p' "$1"; }
expect() { grep -qF -- "$1" "$work/out" || fail "falta en la salida: $1"; }

# Negativas, antes de tocar nada.
FAKE_UID=1000 deploy "$work/a.env" && fail "corre sin root"
expect "ejecútalo con sudo"
sed 's/@sha256:a*$/:latest/' "$work/a.env" > "$work/tag.env"
deploy "$work/tag.env" && fail "acepta una imagen por etiqueta"
expect "no va fijada por @sha256"
{ cat "$work/a.env"; echo "DATABASE_URL=postgres://x"; } > "$work/extra.env"
deploy "$work/extra.env" && fail "acepta una variable ajena"
expect "variable no permitida: DATABASE_URL"
grep -v '^REDIS' "$work/a.env" > "$work/short.env"
deploy "$work/short.env" && fail "acepta un release incompleto"
expect "falta REDIS_IMAGE"
[ ! -s "$CALLS" ] || fail "una negativa llamó a docker"

# Primer despliegue: sin volcado (no hay versión en marcha); orden pull → migrate → up → puerta.
deploy "$work/a.env" || fail "primer despliegue"
[ "$(letter "$work/etc/release.env")" = a ] && [ ! -f "$work/etc/release.env.previous" ] || fail "estado tras a"
if grep -q pg_dump "$CALLS"; then fail "volcado sin versión previa"; fi
events=$(grep -oE '^a (pull --quiet|up -d --wait postgres redis|run --rm --no-deps migrate|up -d --wait --wait-timeout|exec -T backend)|^smoke' "$CALLS" | tr '\n' '|')
[ "$events" = "a pull --quiet|a up -d --wait postgres redis|a run --rm --no-deps migrate|a up -d --wait --wait-timeout|a exec -T backend|smoke|" ] ||
  fail "orden: $events"

# Segundo despliegue: volcado cifrado de la versión en marcha (a) antes de migrar.
deploy "$work/b.env" || fail "despliegue de b"
grep -q '^a exec -T postgres pg_dump -U postgres -d crm -Fc' "$CALLS" || fail "sin volcado previo"
grep -q '^age -R .*/backup-recipients.txt' "$CALLS" || fail "volcado sin cifrar"
[ "$(sed -n '/pg_dump/=' "$CALLS")" -lt "$(sed -n '/run --rm --no-deps migrate/=' "$CALLS")" ] || fail "volcado tras migrar"
ls "$work/state/predeploy/"predeploy-*.dump.age > /dev/null || fail "no se guardó el volcado"
[ "$(letter "$work/etc/release.env")" = b ] && [ "$(letter "$work/etc/release.env.previous")" = a ] || fail "rotación"

# La puerta falla con c: vuelve sola a b, sale con 1 y no cambia release.env.
FAIL_READY=c deploy "$work/c.env" && fail "c pasa con la puerta rota"
expect "ROLLBACK OK"
grep -q '^b up -d --wait --wait-timeout 10 --remove-orphans' "$CALLS" || fail "no levantó b"
[ "$(letter "$work/etc/release.env")" = b ] && [ "$(letter "$work/etc/release.env.previous")" = a ] || fail "estado tras rollback"
[ ! -e "$work/etc/release.env.next" ] && [ ! -e "$work/state/lock" ] || fail "quedan restos"
grep -q "ROLLBACK OK" "$work/state/deploy.log" || fail "el registro no cuenta el rollback"

# migrate falla con c: para antes del up; ni rollback ni cambio de release.env.
FAIL_RUN=c deploy "$work/c.env" && fail "c pasa con migrate roto"
expect "fallaron las migraciones"
! grep -q -- '--remove-orphans' "$CALLS" && [ "$(letter "$work/etc/release.env")" = b ] || fail "migrate roto tocó el runtime"

# Idempotente: repetir b no pierde la versión anterior.
deploy "$work/b.env" || fail "repetir b"
[ "$(letter "$work/etc/release.env.previous")" = a ] || fail "repetir b perdió a"

# Vuelta atrás manual: a pasa a ser la actual y b la anterior.
deploy --rollback || fail "--rollback"
[ "$(letter "$work/etc/release.env")" = a ] && [ "$(letter "$work/etc/release.env.previous")" = b ] || fail "estado tras --rollback"

# Un despliegue cortado deja el lock: el siguiente se niega.
mkdir "$work/state/lock"
deploy "$work/b.env" && fail "despliega con el lock puesto"
expect "hay otro despliegue en curso"
[ -d "$work/state/lock" ] || fail "borró un lock ajeno"
if grep -qiE 'password|secret|postgres://' "$work/state/deploy.log"; then fail "el registro lleva secretos"; fi
echo "test-deploy OK"
