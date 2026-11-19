#!/usr/bin/env bash
# Prueba de deploy.sh sin servidor (F1-15, CI: infra-prod.yml): copia el script a un directorio
# temporal con un smoke.sh y un backup/backup.sh falsos y pone delante en el PATH `docker`, `curl` e `id` falsos
# que anotan cada llamada y fallan a demanda (FAIL_* con las letras de los releases que fallan).
# Comprueba el orden de los pasos, la vuelta atrás automática ante cada fallo de la puerta (ready,
# smoke.sh, la página de entrada), que migrate y el volcado no tocan el runtime al fallar, que una
# señal deja el lock, y las negativas antes de tocar nada. F1-15b: REVISION frente al checkout, el
# volcado previo con postgres parado, un fallo de `up postgres redis`, `--migrate` y el lock que
# comparte con el backup nocturno. Uso: bash infra/prod/test-deploy.sh
set -euo pipefail
src=$(cd "$(dirname "$0")" && pwd)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
mkdir -p "$work/infra/prod" "$work/bin" "$work/etc"
cp "$src/deploy.sh" "$src/compose.yaml" "$src/host-mode.sh" "$work/infra/prod/"

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
  "up -d --no-recreate --wait --wait-timeout 5 postgres redis") if fails "$FAIL_STORES"; then exit 1; fi ;;
  "run --rm --no-deps migrate") if fails "$FAIL_RUN"; then exit 1; fi ;;
  "exec -T backend python"*) if fails "$FAIL_READY"; then exit 1; fi ;;
  "pull --quiet") if [ -n "$KILL_ON_PULL" ]; then kill -"$KILL_ON_PULL" "$PPID"; fi ;;
  "up -d --wait --wait-timeout"*) if fails "$KILL_ON_UP"; then kill -TERM "$PPID"; fi ;;
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
# backup.sh falso (F1-16): anota «backup <tipo> <letra del release que vuelca>».
mkdir -p "$work/infra/prod/backup"
cat > "$work/infra/prod/backup/backup.sh" <<'EOF'
#!/bin/sh
l=$(sed -n 's/^BACKEND_IMAGE=.*@sha256:\(.\).*/\1/p' "$CRM_RELEASE_ENV")
echo "backup $1 $l" >> "$CALLS"
case $FAIL_DUMP in *"$l"*) exit 1 ;; esac
EOF
cat > "$work/bin/id" <<'EOF'
#!/bin/sh
echo "${FAKE_UID:-0}"
EOF
chmod +x "$work/bin/"* "$work/infra/prod/"*.sh "$work/infra/prod/backup/backup.sh"
echo "CRM_DOMAIN=example.invalid" > "$work/etc/proxy.env"
# infra/prod es un checkout de git: REVISION de cada release es su HEAD.
git -C "$work" init -q
git -C "$work" add infra/prod
git -C "$work" -c user.name=t -c user.email=t@example.invalid commit -qm t
head=$(git -C "$work" rev-parse HEAD)

release() { # release <letra> [mayor de postgres]: las cinco imágenes con un digest de esa letra
  local hex name
  hex=$(printf "$1%.0s" $(seq 64))
  for name in backend frontend proxy; do
    echo "$(echo "$name" | tr '[:lower:]' '[:upper:]')_IMAGE=ghcr.io/papicuche/crm-gooddoggy/$name@sha256:$hex"
  done
  echo "POSTGRES_IMAGE=docker.io/library/postgres:${2:-18}.6@sha256:$hex"
  echo "REDIS_IMAGE=docker.io/library/redis:8.8.3-alpine@sha256:$hex"
  echo "REVISION=${3:-$head}"
}
release a > "$work/a.env"
release b > "$work/b.env"
release c > "$work/c.env"

export CALLS="$work/calls" CRM_ENV_DIR="$work/etc" CRM_DEPLOY_DIR="$work/state" CRM_HEALTH_TIMEOUT=5
export PATH="$work/bin:$PATH" FAIL_RUN="" FAIL_READY="" FAIL_DUMP="" FAIL_SMOKE="" FAIL_ENTRY=0 KILL_ON_PULL="" KILL_ON_UP=""
export FAIL_STORES=""
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
refused_args() { # refused_args <mensaje> <argumentos…>: sale con 1 sin llamar a docker
  local want=$1
  shift
  deploy "$@"
  [ "$rc" = 1 ] || fail "acepta $* (exit $rc)"
  expect "$want"
  if grep -q '^[a-z]* ' "$CALLS"; then fail "una negativa llamó a docker: $*"; fi
}
rolled_back() { # rolled_back <contexto>: el release nuevo (c) falló y se volvió a b, con la puerta
  [ "$rc" = 1 ] || fail "$1: exit $rc, se esperaba 1"
  expect "ROLLBACK OK"
  grep -q '^b up -d --wait --wait-timeout 5 --remove-orphans' "$CALLS" || fail "$1: no levantó b"
  grep -q '^b exec -T backend python' "$CALLS" || fail "$1: el rollback no pasó /health/ready"
  grep -q '^smoke .*/release\.env$' "$CALLS" || fail "$1: el rollback no pasó smoke.sh"
  [ "$(grep '^curl ' "$CALLS" | tail -n 1)" = "curl -fsS --connect-timeout 5 --max-time 30 -o /dev/null https://crm.example.invalid/login" ] ||
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
variant '/^REVISION/d'
refused "$work/v.env" "falta REVISION"
variant 's/^REVISION=.*/REVISION=main/'
refused "$work/v.env" "REVISION no es el SHA de un commit"
{ cat "$work/a.env"; echo "REVISION=$head"; } > "$work/v.env"
refused "$work/v.env" "REVISION repetida"
other=$(printf 'f%.0s' $(seq 40))
release a 18 "$other" > "$work/v.env"
refused "$work/v.env" "el release es de $other e infra/prod está en $head: git checkout $other"
refused --migrate "no hay $work/etc/release.env"
# --allow-revision-mismatch solo como primer argumento; cualquier otro --allow… se rechaza.
refused_args "opción desconocida o fuera de sitio: --allow-revision-mismatch" "$work/a.env" --allow-revision-mismatch
refused --allow-revison-mismatch "opción desconocida o fuera de sitio: --allow-revison-mismatch"
echo 'CRM_DOMAIN="example.invalid"' > "$work/etc/proxy.env"
refused "$work/a.env" "no es un nombre de dominio"
echo "CRM_DOMAIN=example.invalid" > "$work/etc/proxy.env"

# Primer despliegue con la puerta rota: no hay a qué volver; exit 2, sin release.env y sin lock.
FAIL_READY=a deploy "$work/a.env"
[ "$rc" = 2 ] || fail "primer despliegue roto: exit $rc, se esperaba 2"
expect "ROLLBACK IMPOSIBLE"
if [ -e "$work/etc/release.env" ] || [ -e "$work/etc/release.env.next" ] || [ -e "$work/state/lock" ]; then
  fail "primer despliegue roto: quedan restos"
fi

# Primer despliegue: sin volcado; postgres y redis con el release nuevo; la puerta completa.
deploy "$work/a.env"
[ "$rc" = 0 ] || fail "primer despliegue"
state a "" "tras a"
if grep -q '^backup' "$CALLS"; then fail "volcado sin versión previa"; fi
events=$(grep -oE '^a (pull --quiet|up -d --no-recreate --wait --wait-timeout 5 postgres redis|run --rm --no-deps migrate|up -d --wait --wait-timeout|exec -T backend)|^smoke|^curl .*/login$' "$CALLS" |
  sed 's/^curl .*/curl/' | tr '\n' '|')
[ "$events" = "a pull --quiet|a up -d --no-recreate --wait --wait-timeout 5 postgres redis|a run --rm --no-deps migrate|a up -d --wait --wait-timeout|a exec -T backend|smoke|curl|" ] ||
  fail "orden: $events"

# Segundo despliegue (postgres parado: el docker falso no lo da por arrancado): postgres y redis de a,
# después backup.sh predeploy de a y después migrate. Siguen en a hasta `up`.
deploy "$work/b.env"
[ "$rc" = 0 ] || fail "despliegue de b"
events=$(grep -oE '^a up -d --no-recreate --wait --wait-timeout 5 postgres redis|^backup predeploy a$|^b run --rm --no-deps migrate' "$CALLS" | tr '\n' '|')
[ "$events" = "a up -d --no-recreate --wait --wait-timeout 5 postgres redis|backup predeploy a|b run --rm --no-deps migrate|" ] ||
  fail "postgres y redis de a, volcado y migrate: $events"
if grep -q '^b up -d --no-recreate --wait --wait-timeout 5 postgres redis' "$CALLS"; then fail "postgres y redis cambian antes de migrar"; fi
state b a "rotación"

# postgres o redis no arrancan: sale sin volcar ni migrar ni tocar el runtime.
FAIL_STORES=b deploy "$work/c.env"
[ "$rc" = 1 ] || fail "postgres o redis rotos: exit $rc"
expect "postgres o redis no arrancan"
if grep -q -e '^backup' -e 'run --rm' -e '^c up' "$CALLS"; then fail "siguió tras fallar postgres o redis"; fi
state b a "postgres o redis rotos"

# --migrate (crm-migrate.timer): solo migrate de la versión en marcha, con el lock.
deploy --migrate
[ "$rc" = 0 ] || fail "--migrate"
[ "$(grep -v '^ *$' "$CALLS")" = "b run --rm --no-deps migrate" ] || fail "--migrate hizo más: $(cat "$CALLS")"
FAIL_RUN=b deploy --migrate
[ "$rc" = 1 ] || fail "--migrate roto: exit $rc"
expect "falló migrate de la versión en marcha"
[ ! -e "$work/state/lock" ] || fail "--migrate dejó el lock"
grep -qx 'ExecStart=/opt/crm/infra/prod/deploy.sh --migrate' "$src/crm-migrate.service" || fail "crm-migrate.service"
# --migrate toma el lock antes que nada: con un despliegue o el nocturno en curso, se niega y no lo toca.
mkdir "$work/state/lock"
refused --migrate "hay otro despliegue o el backup nocturno en curso"
[ -d "$work/state/lock" ] || fail "--migrate quitó un lock ajeno"
rmdir "$work/state/lock"
# Un fallo del servicio avisa: ExecStopPost llama a backup/alert.sh (journal y Sentry).
grep -qE '^ExecStopPost=/bin/sh -c .* \|\| exec /opt/crm/infra/prod/backup/alert\.sh migrate\.failed ' "$src/crm-migrate.service" ||
  fail "crm-migrate.service no avisa de un fallo"
grep -qx 'OnCalendar=\*-\*-15 12:00:00 UTC' "$src/crm-migrate.timer" || fail "crm-migrate.timer no es mensual"

# Con --allow-revision-mismatch, un release de otro commit se despliega y queda anotado.
release c 18 "$other" > "$work/v.env"
deploy --allow-revision-mismatch "$work/v.env"
[ "$rc" = 0 ] || fail "--allow-revision-mismatch"
grep -q "AVISO: release de $other con infra/prod en $head" "$work/state/deploy.log" || fail "sin aviso de la revisión"
state c b "--allow-revision-mismatch"
deploy "$work/b.env"
[ "$rc" = 0 ] || fail "volver a b"
cp "$work/a.env" "$work/etc/release.env.previous"

# Versión mayor distinta de PostgreSQL, y una versión en marcha manipulada: se niega.
release c 19 > "$work/v.env"
refused "$work/v.env" "cambia de versión mayor (18 → 19)"
cp "$work/etc/release.env" "$work/keep.env"
echo "EXTRA=1" >> "$work/etc/release.env"
refused "$work/c.env" "release.env: variable no permitida: EXTRA"
cp "$work/keep.env" "$work/etc/release.env"

# Falla el volcado: sale sin migrar ni arrancar nada.
FAIL_DUMP=b deploy "$work/c.env"
[ "$rc" = 1 ] || fail "el volcado roto sigue (exit $rc)"
expect "falló el volcado previo"
if grep -q -e 'run --rm' -e '^c up' "$CALLS"; then fail "migró o arrancó tras un volcado roto"; fi
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

# Una señal mientras la vuelta atrás levanta b: el stack queda a medias, así que el lock se queda.
FAIL_READY=c KILL_ON_UP=b deploy "$work/c.env"
[ "$rc" = 143 ] || fail "TERM en el rollback: exit $rc, se esperaba 143"
expect "INTERRUMPIDO"
[ -d "$work/state/lock" ] || fail "TERM en el rollback quitó el lock"
[ -f "$work/etc/release.env.next" ] || fail "TERM en el rollback quitó release.env.next"
state b a "TERM en el rollback"
rmdir "$work/state/lock"
rm "$work/etc/release.env.next"

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

# SIGHUP (sesión SSH perdida) no corta el despliegue.
KILL_ON_PULL=HUP deploy "$work/c.env"
[ "$rc" = 0 ] || fail "SIGHUP cortó el despliegue (exit $rc)"
state c a "tras SIGHUP"

# Otra señal a mitad deja el lock y release.env.next; el siguiente despliegue se niega.
KILL_ON_PULL=TERM deploy "$work/b.env"
[ "$rc" = 143 ] || fail "señal: exit $rc"
[ -d "$work/state/lock" ] || fail "la señal quitó el lock"
[ -f "$work/etc/release.env.next" ] || fail "la señal quitó release.env.next"
grep -q "INTERRUMPIDO" "$work/state/deploy.log" || fail "el registro no cuenta la interrupción"
refused "$work/b.env" "hay otro despliegue o el backup nocturno en curso"
[ -d "$work/state/lock" ] || fail "borró un lock ajeno"
rmdir "$work/state/lock"
rm "$work/etc/release.env.next"

# El backup nocturno (backup.sh real, con docker, age y rclone falsos) y deploy.sh comparten el lock.
mkdir -p "$work/backup-bin" "$work/sleep-bin"
# mktemp falso: backup.sh lo llama justo antes de volcar; anota si el lock está puesto.
cat > "$work/backup-bin/mktemp" <<'EOF'
#!/bin/sh
[ ! -d "$CRM_DEPLOY_DIR/lock" ] || echo "lock held" >> "$LOCK_PROBE"
exec /usr/bin/mktemp "$@"
EOF
cat > "$work/backup-bin/age" <<'EOF'
#!/bin/sh
while [ $# -gt 0 ]; do [ "$1" = -o ] && cat > "$2"; shift; done
EOF
printf '#!/bin/sh\nexit 0\n' > "$work/backup-bin/rclone"
# sleep falso: el despliegue en curso termina mientras el nocturno espera.
cat > "$work/sleep-bin/sleep" <<'EOF'
#!/bin/sh
echo "sleep $1" >> "$CALLS"
rmdir "$CRM_DEPLOY_DIR/lock"
EOF
chmod +x "$work/backup-bin/"* "$work/sleep-bin/"*
printf '%s\n' BACKUP_S3_BUCKET=b BACKUP_S3_ACCESS_KEY_ID=k BACKUP_S3_SECRET_ACCESS_KEY=s > "$work/etc/backup.env"
printf '%s\n' STORAGE_BUCKET=m > "$work/etc/runtime.env"
echo "age1example" > "$work/etc/backup-recipients.txt"
chmod 600 "$work/etc/backup.env" "$work/etc/runtime.env"
nightly() { # nightly <tipo> <espera máxima> [PATH extra]: backup.sh real; sale en $rc, la salida en $work/out
  : > "$CALLS"
  : > "$work/probe"
  if PATH="${3:-}$work/backup-bin:$PATH" FAKE_UID="$(command -p id -u)" LOCK_PROBE="$work/probe" \
    BACKUP_STATE_DIR="$work/backup-state" CRM_RELEASE_ENV="$work/etc/release.env" BACKUP_LOCK_WAIT=$2 \
    bash "$src/backup/backup.sh" "$1" > "$work/out" 2>&1; then
    rc=0
  else
    rc=$?
  fi
}
mkdir "$work/state/lock" # un despliegue en curso (o cortado) que no termina en el plazo: falla con aviso
alerted=1 nightly nightly 0 # un `alerted` heredado no silencia el aviso (F1-16b)
[ "$rc" != 0 ] || fail "el nocturno corrió con el lock de un despliegue"
expect '"event":"backup.failed","level":"error","reason":"kind=nightly stage=deploy_lock"'
[ ! -e "$work/backup-state/last-period" ] || fail "un nocturno sin lock marcó su periodo como copiado"
[ ! -s "$work/probe" ] || fail "el nocturno siguió con el lock ajeno"
[ -d "$work/state/lock" ] || fail "el nocturno quitó el lock de un despliegue"
nightly nightly 30 "$work/sleep-bin:" # el despliegue termina: el nocturno espera, lo toma y lo suelta
grep -qx 'sleep 30' "$CALLS" || fail "el nocturno no esperó"
grep -qx 'lock held' "$work/probe" || fail "el nocturno vuelca sin el lock"
[ ! -e "$work/state/lock" ] || fail "el nocturno no soltó el lock (salida $rc)"
mkdir "$work/state/lock" # predeploy lo lanza deploy.sh con su lock: no espera
nightly predeploy 0
if grep -q 'deploy_lock' "$work/out"; then fail "predeploy esperó al lock de deploy.sh"; fi
grep -qx 'lock held' "$work/probe" || fail "predeploy no siguió hasta el volcado"
rmdir "$work/state/lock"

# backup/alert.sh (ExecStopPost de crm-migrate.service): el aviso va al journal y a Sentry.
{ cat "$work/etc/backup.env"; echo "SENTRY_DSN=https://k@sentry.example.invalid/7"; } > "$work/etc/alert.env"
chmod 600 "$work/etc/alert.env"
: > "$CALLS"
FAKE_UID="$(command -p id -u)" BACKUP_ENV="$work/etc/alert.env" \
  bash "$src/backup/alert.sh" migrate.failed "result=exit-code status=1" > "$work/out" 2>&1 || fail "alert.sh"
expect '"event":"migrate.failed","level":"error","reason":"result=exit-code status=1"'
grep -q '^curl .*https://sentry.example.invalid/api/7/envelope/$' "$CALLS" || fail "alert.sh no llamó a Sentry"

# Un checkout con cambios sin commit en infra/prod no despliega; uno que no es de git, tampoco sin
# --allow-revision-mismatch (no se puede comprobar REVISION).
echo "# cambio" >> "$work/infra/prod/compose.yaml"
refused "$work/b.env" "infra/prod tiene cambios sin commit"
rm -rf "$work/.git"
refused "$work/b.env" "infra/prod está en (no es un checkout de git)"

if grep -qiE 'password|secret|postgres://' "$work/state/deploy.log"; then fail "el registro lleva secretos"; fi
echo "test-deploy OK"
