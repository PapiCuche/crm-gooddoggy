#!/usr/bin/env bash
# Prueba de deploy.sh sin servidor (F1-15, CI: infra-prod.yml): copia el script a un directorio
# temporal con un smoke.sh falso y pone delante en el PATH `docker`, `age`, `curl` e `id` falsos
# que anotan cada llamada y fallan a demanda (FAIL_* con las letras de los releases que fallan).
# Comprueba el orden de los pasos, la vuelta atrás automática ante cada fallo de la puerta (ready,
# smoke.sh, la página de entrada), que migrate y el volcado no tocan el runtime al fallar, que una
# señal deja el lock, y las negativas antes de tocar nada. Uso: bash infra/prod/test-deploy.sh
set -euo pipefail
src=$(cd "$(dirname "$0")" && pwd)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
mkdir -p "$work/infra/prod" "$work/bin" "$work/etc"
cp "$src/deploy.sh" "$src/compose.yaml" "$work/infra/prod/"

# docker falso: anota «<letra del release> <argumentos>» (la letra sale del digest del backend).
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
fails() { [ -n "$release" ] && case $1 in *"$release"*) true ;; *) false ;; esac; }
case "${args[*]}" in
  "ps -q --status running postgres") echo pg-container ;;
  "exec -T postgres pg_dump"*) echo dump-bytes; if fails "$FAIL_DUMP"; then exit 1; fi ;;
  "run --rm --no-deps migrate") if fails "$FAIL_RUN"; then exit 1; fi ;;
  "exec -T backend python"*) if fails "$FAIL_READY"; then exit 1; fi ;;
  "pull --quiet") if [ -n "$KILL_ON_PULL" ]; then kill -TERM "$PPID"; fi ;;
esac
EOF
cat > "$work/infra/prod/smoke.sh" <<'EOF'
#!/bin/sh
echo "smoke $CRM_RELEASE_ENV" >> "$CALLS"
l=$(sed -n 's/^BACKEND_IMAGE=.*@sha256:\(.\).*/\1/p' "$CRM_RELEASE_ENV")
case $FAIL_SMOKE in *"$l"*) exit 1 ;; esac
EOF
# curl falso: las FAIL_ENTRY primeras llamadas fallan (la puerta del release nuevo, no la del rollback).
cat > "$work/bin/curl" <<'EOF'
#!/bin/sh
echo "curl $*" >> "$CALLS"
n=$(grep -c '^curl ' "$CALLS")
[ "$n" -gt "$FAIL_ENTRY" ] || exit 22
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

release() { # release <letra> [mayor de postgres]: las cinco imágenes con un digest de esa letra
  local hex name
  hex=$(printf "$1%.0s" $(seq 64))
  for name in backend frontend proxy; do
    echo "$(echo "$name" | tr '[:lower:]' '[:upper:]')_IMAGE=ghcr.io/papicuche/crm-gooddoggy/$name@sha256:$hex"
  done
  echo "POSTGRES_IMAGE=docker.io/library/postgres:${2:-18}.6@sha256:$hex"
  echo "REDIS_IMAGE=docker.io/library/redis:8.8.3-alpine@sha256:$hex"
}
release a > "$work/a.env"
release b > "$work/b.env"
release c > "$work/c.env"

export CALLS="$work/calls" CRM_ENV_DIR="$work/etc" CRM_DEPLOY_DIR="$work/state" CRM_HEALTH_TIMEOUT=5
export PATH="$work/bin:$PATH" FAIL_RUN="" FAIL_READY="" FAIL_DUMP="" FAIL_SMOKE="" FAIL_ENTRY=0 KILL_ON_PULL=""
deploy() {
  : > "$CALLS"
  if "$work/infra/prod/deploy.sh" "$@" > "$work/out" 2>&1; then rc=0; else rc=$?; fi
}
fail() { echo "test-deploy FAIL: $*" >&2; cat "$work/out" >&2; exit 1; }
letter() { sed -n 's/^BACKEND_IMAGE=.*@sha256:\(.\).*/\1/p' "$1"; }
expect() { grep -qF -- "$1" "$work/out" || fail "falta en la salida: $1"; }
state() { # state <actual> <anterior> <contexto>: letras de release.env y release.env.previous
  [ "$(letter "$work/etc/release.env")" = "$1" ] || fail "$3: release.env"
  [ "$(letter "$work/etc/release.env.previous" 2> /dev/null)" = "$2" ] || fail "$3: release.env.previous"
}
refused() { # refused <release> <mensaje>: sale con 1 sin llamar a docker
  deploy "$1"
  [ "$rc" = 1 ] || fail "acepta $1 (exit $rc)"
  expect "$2"
  if grep -q '^[a-z]* ' "$CALLS"; then fail "una negativa llamó a docker: $1"; fi
}
rolled_back() { # rolled_back <contexto>: el release nuevo (c) falló y se volvió a b, con la puerta
  [ "$rc" = 1 ] || fail "$1: exit $rc, se esperaba 1"
  expect "ROLLBACK OK"
  grep -q '^b up -d --wait --wait-timeout 5 --remove-orphans' "$CALLS" || fail "$1: no levantó b"
  grep -q '^b exec -T backend python' "$CALLS" || fail "$1: el rollback no pasó /health/ready"
  grep -q '^smoke .*/release\.env$' "$CALLS" || fail "$1: el rollback no pasó smoke.sh"
  [ "$(grep '^curl ' "$CALLS" | tail -n 1)" = "curl -fsS -o /dev/null https://crm.example.invalid/login" ] ||
    fail "$1: el rollback no probó la entrada"
  state b a "$1"
  if [ -e "$work/etc/release.env.next" ] || [ -e "$work/state/lock" ]; then fail "$1: quedan restos"; fi
}
variant() { sed "$1" "$work/a.env" > "$work/v.env"; }

# Negativas, antes de tocar nada.
FAKE_UID=1000 refused "$work/a.env" "ejecútalo con sudo"
variant 's/@sha256:a*$/:latest/'
refused "$work/v.env" "no es ghcr.io/papicuche/crm-gooddoggy/backend@sha256"
variant '1s/$/0/' # 65 hex
refused "$work/v.env" "BACKEND_IMAGE no es"
variant '1s/a$//' # 63 hex
refused "$work/v.env" "BACKEND_IMAGE no es"
variant '4s/$/0/'
refused "$work/v.env" "POSTGRES_IMAGE no va fijada"
variant '5s/a$//'
refused "$work/v.env" "REDIS_IMAGE no va fijada"
variant '2s|papicuche/crm-gooddoggy|otro/repo|'
refused "$work/v.env" "FRONTEND_IMAGE no es"
variant '4s|postgres:18.6@|postgres@|'
refused "$work/v.env" "POSTGRES_IMAGE sin versión"
{ cat "$work/a.env"; echo "DATABASE_URL=postgres://x"; } > "$work/v.env"
refused "$work/v.env" "variable no permitida: DATABASE_URL"
{ cat "$work/a.env"; echo "CRM_ENV_DIR=/otro"; } > "$work/v.env"
refused "$work/v.env" "CRM_ENV_DIR distinto"
variant '/^REDIS/d'
refused "$work/v.env" "falta REDIS_IMAGE"
echo 'CRM_DOMAIN="example.invalid"' > "$work/etc/proxy.env"
refused "$work/a.env" "no es un nombre de dominio"
echo "CRM_DOMAIN=example.invalid" > "$work/etc/proxy.env"

# Primer despliegue: sin volcado; postgres y redis con el release nuevo; la puerta completa.
deploy "$work/a.env"
[ "$rc" = 0 ] || fail "primer despliegue"
state a "" "tras a"
if grep -q pg_dump "$CALLS"; then fail "volcado sin versión previa"; fi
events=$(grep -oE '^a (pull --quiet|up -d --wait postgres redis|run --rm --no-deps migrate|up -d --wait --wait-timeout|exec -T backend)|^smoke|^curl .*/login$' "$CALLS" |
  sed 's/^curl .*/curl/' | tr '\n' '|')
[ "$events" = "a pull --quiet|a up -d --wait postgres redis|a run --rm --no-deps migrate|a up -d --wait --wait-timeout|a exec -T backend|smoke|curl|" ] ||
  fail "orden: $events"

# Segundo despliegue: volcado cifrado de a antes de migrar; postgres y redis siguen en a hasta `up`.
deploy "$work/b.env"
[ "$rc" = 0 ] || fail "despliegue de b"
grep -q '^a exec -T postgres pg_dump -U postgres -d crm -Fc' "$CALLS" || fail "sin volcado previo"
grep -q '^age -R .*/backup-recipients.txt' "$CALLS" || fail "volcado sin cifrar"
[ "$(sed -n '/pg_dump/=' "$CALLS")" -lt "$(sed -n '/run --rm --no-deps migrate/=' "$CALLS")" ] || fail "volcado tras migrar"
grep -q '^a up -d --wait postgres redis' "$CALLS" || fail "postgres y redis no siguen en a"
if grep -q '^b up -d --wait postgres redis' "$CALLS"; then fail "postgres y redis cambian antes de migrar"; fi
[ "$(find "$work/state/predeploy" -type f -name 'predeploy-*.dump.age' -perm 600 | wc -l)" -eq 1 ] || fail "volcado .age 0600"
if grep -rl dump-bytes "$work/state" | grep -v '\.dump\.age$'; then fail "copia del volcado en claro"; fi
state b a "rotación"

# Versión mayor distinta de PostgreSQL, y una versión en marcha manipulada: se niega.
release c 19 > "$work/v.env"
refused "$work/v.env" "cambia de versión mayor (18 → 19)"
cp "$work/etc/release.env" "$work/keep.env"
echo "EXTRA=1" >> "$work/etc/release.env"
refused "$work/c.env" "release.env: variable no permitida: EXTRA"
cp "$work/keep.env" "$work/etc/release.env"

# Falla el volcado (pipefail): sale sin migrar ni dejar un .age a medias.
FAIL_DUMP=b deploy "$work/c.env"
[ "$rc" = 1 ] || fail "el volcado roto sigue (exit $rc)"
expect "falló el volcado previo"
if grep -q -e 'run --rm' -e '^c up' "$CALLS"; then fail "migró o arrancó tras un volcado roto"; fi
[ "$(find "$work/state/predeploy" -type f | wc -l)" -eq 1 ] || fail "quedó un volcado a medias"
state b a "volcado roto"

# Cada fallo de la puerta con c vuelve solo a b, que vuelve a pasar la puerta entera.
FAIL_READY=c deploy "$work/c.env"
rolled_back "ready"
grep -q "ROLLBACK OK" "$work/state/deploy.log" || fail "el registro no cuenta el rollback"
FAIL_SMOKE=c deploy "$work/c.env"
rolled_back "smoke.sh"
expect "smoke.sh falló"
FAIL_ENTRY=1 deploy "$work/c.env"
rolled_back "entrada"
expect "la entrada https://crm.example.invalid/login no responde 200"

# Si la vuelta atrás tampoco pasa la puerta: exit 2.
FAIL_READY=cb deploy "$work/c.env"
[ "$rc" = 2 ] || fail "rollback fallido: exit $rc, se esperaba 2"
expect "ROLLBACK FALLIDO"
state b a "rollback fallido"

# migrate falla con c: para antes del up; ni rollback ni cambio de release.env.
FAIL_RUN=c deploy "$work/c.env"
[ "$rc" = 1 ] || fail "migrate roto: exit $rc"
expect "fallaron las migraciones"
if grep -q '^c up' "$CALLS"; then fail "migrate roto tocó el runtime"; fi
state b a "tras migrate roto"

# Idempotente: repetir b no pierde la versión anterior. Vuelta atrás manual: a actual, b anterior.
deploy "$work/b.env"
[ "$rc" = 0 ] || fail "repetir b"
state b a "repetir b"
deploy --rollback
[ "$rc" = 0 ] || fail "--rollback"
state a b "tras --rollback"

# Una señal a mitad deja el lock y release.env.next; el siguiente despliegue se niega.
KILL_ON_PULL=1 deploy "$work/c.env"
[ "$rc" = 130 ] || fail "señal: exit $rc"
[ -d "$work/state/lock" ] || fail "la señal quitó el lock"
[ -f "$work/etc/release.env.next" ] || fail "la señal quitó release.env.next"
grep -q "INTERRUMPIDO" "$work/state/deploy.log" || fail "el registro no cuenta la interrupción"
refused "$work/b.env" "hay otro despliegue en curso"
[ -d "$work/state/lock" ] || fail "borró un lock ajeno"
rmdir "$work/state/lock"
rm "$work/etc/release.env.next"

# Un checkout con cambios sin commit en infra/prod no despliega.
git -C "$work" init -q
git -C "$work" add infra/prod
git -C "$work" -c user.name=t -c user.email=t@example.invalid commit -qm t
echo "# cambio" >> "$work/infra/prod/compose.yaml"
refused "$work/b.env" "infra/prod tiene cambios sin commit"

if grep -qiE 'password|secret|postgres://' "$work/state/deploy.log"; then fail "el registro lleva secretos"; fi
echo "test-deploy OK"
