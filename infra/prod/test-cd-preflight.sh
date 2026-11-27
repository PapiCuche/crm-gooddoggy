#!/usr/bin/env bash
# Prueba de cd-preflight.sh con un servidor simulado (F1-20d, CI: infra-prod.yml): un árbol temporal
# con etc/, el estado de los despliegues y del backup y un /proc falso, y delante en el PATH `docker`,
# `curl`, `date`, `df` y `nproc` falsos que anotan cada llamada y marcan VIOLATION ante cualquier orden
# que no sea de lectura. Cada condición tiene su prueba de bloqueo y la de lo que no se puede leer
# (falla cerrado). Tras cada ejecución, el árbol es idéntico (solo lectura), runtime.env (una FIFO con
# un canario) no se ha abierto y el canario no sale. Uso: bash infra/prod/test-cd-preflight.sh
set -euo pipefail
src=$(cd "$(dirname "$0")" && pwd)
script="$src/cd-preflight.sh"
work=$(mktemp -d)
writer=""
cleanup() {
  if [ -n "$writer" ]; then kill "$writer" 2> /dev/null || true; fi
  chmod -R u+rwx "$work" 2> /dev/null || true
  rm -rf "$work"
}
trap cleanup EXIT
srv="$work/srv" bin="$work/bin" out="$work/out"
mkdir -p "$srv/etc" "$srv/state" "$srv/backup" "$srv/proc" "$srv/gh" "$bin"
chmod 700 "$srv/state"
canary="canary-$$-not-for-output"
export CALLS="$work/calls" CONTAINERS="$srv/containers" GH="$srv/gh" PROBE="$work/probe"
export CRM_ENV_DIR="$srv/etc" CRM_DEPLOY_DIR="$srv/state" BACKUP_STATE_DIR="$srv/backup" CRM_PROC_DIR="$srv/proc"
REAL_TIMEOUT=$(command -v timeout) REAL_STAT=$(command -v stat)
export CRM_TEST_SECRET="$canary" REAL_TIMEOUT REAL_STAT PATH="$bin:$PATH"

# La sonda de la puerta de deploy.sh: el docker falso solo acepta exactamente esa en `docker exec`.
awk '/exec -T backend python -c .import sys/ { sub(/.*python -c ./, ""); on = 1 }
  on { if (sub(/'"'"' 2> \/dev\/null; do$/, "")) { print; exit } print }' "$src/deploy.sh" > "$PROBE"
grep -q '^import sys' "$PROBE" || { echo "test-cd-preflight FAIL: no encuentro la sonda de deploy.sh" >&2; exit 1; }

cat > "$bin/docker" <<'EOF'
#!/usr/bin/env bash
echo "docker $1 ${2:-}" >> "$CALLS"
violation() { echo "VIOLATION docker $*" >> "$CALLS"; exit 99; }
[ -n "${VIA_TIMEOUT:-}" ] || violation "sin timeout: $1"
[ "$FAKE_HANG" != "$1" ] || exec sleep 20 # docker colgado: lo corta el timeout
case $1 in
  info) [ "$*" = "info --format {{.DockerRootDir}}" ] || violation "$@"; [ -n "$FAKE_DOCKER_DIR" ] || exit 1; echo "$FAKE_DOCKER_DIR" ;;
  ps) [ "$*" = "ps -aq --filter label=com.docker.compose.project=crm" ] || violation "$@"; [ -z "$FAIL_PS" ] || exit 1; awk '{ print $4 }' "$CONTAINERS" ;;
  inspect)
    [ "$2" = --format ] || violation "$@"
    shift 3
    [ -z "$FAIL_INSPECT" ] || exit 1
    for id; do grep " $id\$" "$CONTAINERS" || exit 1; done ;;
  exec)
    [ $# = 5 ] && [ "$3" = python ] && [ "$4" = -c ] && [ "$5" = "$(cat "$PROBE")" ] || violation exec
    grep -q "^backend .* $2\$" "$CONTAINERS" || violation "exec en otro contenedor"
    [ -z "$FAIL_PROBE" ] || exit 1 ;;
  *) violation "$@" ;;
esac
EOF
cat > "$bin/curl" <<'EOF'
#!/bin/sh
echo "curl $*" >> "$CALLS"
[ "$1" = -q ] || echo "VIOLATION curl sin -q (lee ~/.curlrc)" >> "$CALLS"
for a; do
  case $a in
    -u | --user | -X | --request | -d | --data* | -F | -T | -o | -K | --config | -n | --netrc* | -b | *uthorization* | *oken*)
      echo "VIOLATION curl $a" >> "$CALLS"; exit 99 ;;
  esac
  url=$a
done
case $url in https://api.github.com/repos/PapiCuche/crm-gooddoggy/issues\?state=open\&labels=*\&per_page=1) ;; *) echo "VIOLATION curl $url" >> "$CALLS"; exit 99 ;; esac
label=${url#*labels=} label=${label%%&*}
[ -f "$GH/$label" ] || exit 7
cat "$GH/$label"
EOF
cat > "$bin/date" <<'EOF'
#!/bin/sh
[ "$*" = "-u +%s" ] || { echo "VIOLATION date $*" >> "$CALLS"; exit 99; }
[ -n "$FAKE_NOW" ] || exit 1
echo "$FAKE_NOW"
EOF
cat > "$bin/df" <<'EOF'
#!/bin/sh
case $2 in /) v=$FAKE_DF_ROOT ;; "$FAKE_DOCKER_DIR") v=$FAKE_DF_DOCKER ;; *) echo "VIOLATION df $*" >> "$CALLS"; exit 99 ;; esac
[ "$1" = -Pk ] || { echo "VIOLATION df $*" >> "$CALLS"; exit 99; }
[ -n "$v" ] || exit 1
echo "Filesystem 1024-blocks Used Available Capacity Mounted on"
echo "/dev/fake $v $2"
EOF
# nproc sin --all obedece a OMP_NUM_THREADS, como el de coreutils.
cat > "$bin/nproc" <<'EOF'
#!/bin/sh
[ "$1" = --all ] || { echo "${OMP_NUM_THREADS:-$FAKE_NPROC}"; exit 0; }
[ -n "$FAKE_NPROC" ] || exit 1
echo "$FAKE_NPROC"
EOF
# timeout: el script tiene que envolver cada docker en `timeout 30`; aquí corta a los 2 s.
cat > "$bin/timeout" <<'EOF'
#!/bin/sh
[ "$1" = 30 ] && [ "$2" = docker ] || { echo "VIOLATION timeout $*" >> "$CALLS"; exit 99; }
shift
VIA_TIMEOUT=1 exec "$REAL_TIMEOUT" 2 "$@"
EOF
# stat: FAKE_STAT, si viene, sustituye la salida (un modo que no es octal).
cat > "$bin/stat" <<'EOF'
#!/bin/sh
[ -z "$FAKE_STAT" ] || { echo "$FAKE_STAT"; exit 0; }
exec "$REAL_STAT" "$@"
EOF
chmod +x "$bin/"*

# runtime.env es una FIFO: abrirla para leer desbloquea al escritor, que deja la marca «opened».
mkfifo "$srv/etc/runtime.env"
(printf 'SECRET=%s\n' "$canary" > "$srv/etc/runtime.env" && touch "$work/opened") &
writer=$!
printf 'DATABASE_URL=%s\n' "$canary" > "$srv/etc/migrate.env"

# El árbol del servidor: rutas, tipo, modo, tamaño, mtime y hash (sin abrir la FIFO).
snap() {
  python3 -I - "$srv" <<'EOF'
import hashlib, os, stat, sys
for base, dirs, files in sorted(os.walk(sys.argv[1])):
    for name in sorted(dirs + files):
        p = os.path.join(base, name)
        st = os.lstat(p)
        digest = ""
        if stat.S_ISREG(st.st_mode) and st.st_mode & 0o400:
            with open(p, "rb") as f:
                digest = hashlib.sha256(f.read()).hexdigest()
        print(p, oct(st.st_mode), st.st_size, st.st_mtime_ns, digest)
EOF
}

hex() { printf "$1%.0s" $(seq 64); }
service() { echo "$1 $2 $3 $(hex "$4")"; } # service <servicio> <estado> <salud> <letra del id>
lima() { python3 -I -c 'import calendar, sys; y, m, d, h, mi = map(int, sys.argv[1:]); print(calendar.timegm((y, m, d, h + 5, mi, 0)))' "$@"; }
at() { # at <año> <mes> <día> <hora> <min>: la hora de Lima y un backup de hace 1 h
  FAKE_NOW=$(lima "$@")
  echo $((FAKE_NOW - 3600)) > "$srv/backup/last-success"
}
baseline() {
  mkdir -p "$srv/state" "$srv/backup"
  chmod 700 "$srv/state" "$srv/backup"
  rm -rf "$srv/state/lock" "$srv/state/cd-halted" "$srv/etc/deploy-freeze" "$srv/proc/meminfo" "$srv/proc/loadavg"
  export FAKE_DOCKER_DIR=/var/lib/docker FAKE_DF_ROOT="104857600 52428800 52428800 50%" FAKE_DF_DOCKER="104857600 52428800 52428800 50%"
  export FAKE_NPROC=2 FAIL_PS="" FAIL_INSPECT="" FAIL_PROBE="" FAKE_HANG="" FAKE_STAT=""
  at 2026 12 1 10 0 # martes
  export FAKE_NOW
  printf 'MemTotal: 8000000 kB\nMemAvailable: 4194304 kB\nSwapTotal: 2097152 kB\nSwapFree: 2097152 kB\n' > "$srv/proc/meminfo"
  echo "0.50 0.60 0.70 1/200 1234" > "$srv/proc/loadavg"
  {
    service backend running healthy a; service ws running healthy b; service worker running none c
    service worker-media running none d; service beat running none e; service frontend running healthy f
    service proxy running none 1; service postgres running healthy 2; service redis running healthy 3
    service migrate exited none 4
  } > "$CONTAINERS"
  echo "[]" > "$GH/incident"
  printf '[ ]\n' > "$GH/stop-the-line"
  printf '# campañas de la otra aplicación\n\n2026-11-27 # campaña\n2026-12-20..2027-01-02\n' > "$srv/etc/deploy-freeze"
  chmod 644 "$srv/etc/deploy-freeze"
}

fail() { echo "test-cd-preflight FAIL: $*" >&2; cat "$out" >&2; exit 1; }
run() {
  : > "$CALLS"
  snap > "$work/before"
  if bash "$script" "$@" > "$out" 2>&1; then rc=0; else rc=$?; fi
  snap > "$work/after"
  cmp -s "$work/before" "$work/after" || fail "cambió el árbol del servidor: $(diff "$work/before" "$work/after" | head -5)"
  if grep -q VIOLATION "$CALLS"; then fail "$(grep VIOLATION "$CALLS")"; fi
  if grep -qF "$canary" "$out"; then fail "el canario sale en la salida"; fi
  [ ! -e "$work/opened" ] || fail "abrió $CRM_ENV_DIR/runtime.env"
}
blocks() { # blocks <condición> <texto> [n.º de condiciones que bloquean]: sale con 1, con ese motivo
  run
  [ "$rc" = 1 ] || fail "$1 «$2»: exit $rc, se esperaba 1"
  [ "$(grep -c '^BLOQUEA ' "$out")" = "${3:-1}" ] || fail "$1 «$2»: bloquean $(grep -c '^BLOQUEA ' "$out"), se esperaba ${3:-1}"
  grep "^BLOQUEA $1: " "$out" | grep -qF -- "$2" || fail "$1: falta «$2»"
  grep -q '^PREFLIGHT BLOQUEADO' "$out" || fail "$1: sin el resumen"
  echo "ok   bloquea $1: $2"
  baseline
}
passes() { # passes <contexto>
  run
  [ "$rc" = 0 ] || fail "$1: exit $rc"
  if [ "$(grep -c '^ok ' "$out")" != 11 ] || ! grep -q '^PREFLIGHT OK$' "$out"; then fail "$1: no están las 11 en verde"; fi
  echo "ok   pasa: $1"
  baseline
}
unreadable() { # unreadable <ruta> <condición> <texto> [n]: sin permiso de lectura (sin root)
  [ "$(id -u)" != 0 ] || return 0
  chmod 000 "$1"
  blocks "$2" "$3" "${4:-1}"
}

baseline
# Las listas de servicios del script son las de compose.yaml (con y sin healthcheck; sin migrate).
read -r want_healthy want_running <<< "$(awk '
  /^services:/ { on = 1; next } /^[a-z]/ { on = 0 } !on { next }
  /^  [a-z-]+:$/ { if (svc != "" && svc != "migrate") { if (hc) h = h " " svc; else r = r " " svc } svc = substr($1, 1, length($1) - 1); hc = 0; pend = 0; next }
  /^    healthcheck:/ { hc = 1; pend = 1; next } pend && /disable: true/ { hc = 0 } { pend = 0 }
  END { if (svc != "migrate") { if (hc) h = h " " svc; else r = r " " svc } gsub(/ /, ",", h); gsub(/ /, ",", r); print h, r }' "$src/compose.yaml")"
got() { sed -n "s/^$1=\"\(.*\)\".*/\1/p" "$script" | tr ' ' '\n' | sort | tr '\n' ' '; }
[ "$(got services_healthy)" = "$(echo "$want_healthy" | tr ',' '\n' | sed '/^$/d' | sort | tr '\n' ' ')" ] || fail "services_healthy no es el de compose.yaml ($want_healthy)"
[ "$(got services_running)" = "$(echo "$want_running" | tr ',' '\n' | sed '/^$/d' | sort | tr '\n' ' ')" ] || fail "services_running no es el de compose.yaml ($want_running)"
echo "ok   los servicios son los de compose.yaml"
# Sin `docker compose` ni archivos de entorno en el código (los comentarios no cuentan).
if grep -v '^[[:space:]]*#' "$script" | grep -nE 'compose |\.env|source |^[[:space:]]*\. '; then fail "usa compose o un .env"; fi
echo "ok   sin docker compose ni .env"

passes "servidor sano, martes 10:00 en Lima"
grep -q '^docker exec ' "$CALLS" || fail "no probó /health/ready"
[ "$(grep -c '^curl -q -fsS --proto =https ' "$CALLS")" = 2 ] || fail "no consultó las dos etiquetas por https"
run --force
if [ "$rc" != 2 ] || [ -s "$CALLS" ]; then fail "con argumentos: exit $rc o llamó a algo"; fi
echo "ok   uso: un argumento sale con 2 sin comprobar nada"

# Backup (26 h).
echo $((FAKE_NOW - 26 * 3600)) > "$srv/backup/last-success"
passes "backup de hace 26 h justas"
echo $((FAKE_NOW - 26 * 3600 - 1)) > "$srv/backup/last-success"
blocks backup "hace 26 h (máximo 26 h)"
rm "$srv/backup/last-success"
blocks backup "no se puede leer"
echo "ayer" > "$srv/backup/last-success"
blocks backup "no es una marca de tiempo"
echo $((FAKE_NOW + 60)) > "$srv/backup/last-success"
blocks backup "en el futuro"
unreadable "$srv/backup" backup "no se puede leer"

# Disco (80 %, 20 GiB) en / y en el directorio de Docker.
FAKE_DF_ROOT="104857600 83886080 20971520 80%" passes "/ al 80 % con 20 GiB libres"
FAKE_DF_ROOT="209715200 169869312 39845888 81%" blocks disco "/ al 81 % con 38 GiB"
FAKE_DF_ROOT="41943040 20971520 20971519 50%" blocks disco "/ al 50 % con 19 GiB libres"
FAKE_DF_DOCKER="104857600 94371840 10485760 90%" blocks disco "/var/lib/docker al 90 %"
FAKE_DOCKER_DIR="" blocks disco "docker info no dice"
FAKE_DF_ROOT="" blocks disco "df no da el uso de /"
FAKE_DF_DOCKER="x y z" blocks disco "df no da el uso de /var/lib/docker"

# Memoria (2 GiB disponibles, swap al 50 %).
sed -i.bak 's/^MemAvailable:.*/MemAvailable: 2097151 kB/' "$srv/proc/meminfo" && rm "$srv/proc/meminfo.bak"
blocks memoria "2047 MiB disponibles"
sed -i.bak 's/^SwapFree:.*/SwapFree: 1048576 kB/' "$srv/proc/meminfo" && rm "$srv/proc/meminfo.bak"
passes "swap al 50 %"
sed -i.bak 's/^SwapFree:.*/SwapFree: 1048575 kB/' "$srv/proc/meminfo" && rm "$srv/proc/meminfo.bak"
blocks memoria "swap en uso al 50 %"
printf 'MemAvailable: 4194304 kB\nSwapTotal: 0 kB\nSwapFree: 0 kB\n' > "$srv/proc/meminfo"
passes "sin swap"
printf 'MemAvailable: 4194304 kB\nSwapFree: 0 kB\n' > "$srv/proc/meminfo"
blocks memoria "no se puede leer MemAvailable"
rm "$srv/proc/meminfo"
blocks memoria "no se puede leer MemAvailable"
unreadable "$srv/proc/meminfo" memoria "no se puede leer MemAvailable"

# Carga (1,5 × vCPU).
echo "2.00 3.00 1.00 1/200 1234" > "$srv/proc/loadavg"
passes "carga 3,00 con 2 vCPU"
echo "2.00 3.01 1.00 1/200 1234" > "$srv/proc/loadavg"
blocks carga "media de 5 min 3.01 con 2 vCPU"
echo "garbage" > "$srv/proc/loadavg"
blocks carga "no se puede leer la carga"
rm "$srv/proc/loadavg"
blocks carga "no se puede leer la carga"
FAKE_NPROC="" blocks carga "número de vCPU"
FAKE_NPROC=0 blocks carga "número de vCPU"
echo "2.00 3.01 1.00 1/200 1234" > "$srv/proc/loadavg"
OMP_NUM_THREADS=64 blocks carga "con 2 vCPU" # el entorno no sube el umbral
unreadable "$srv/proc/loadavg" carga "no se puede leer la carga"

# Producción actual: contenedores sanos y /health/ready.
sed -i.bak 's/^backend running healthy/backend running unhealthy/' "$CONTAINERS" && rm "$CONTAINERS.bak"
blocks produccion "backend(running/unhealthy)"
sed -i.bak 's/^backend running healthy/backend running none/' "$CONTAINERS" && rm "$CONTAINERS.bak"
blocks produccion "backend(running/none)"
sed -i.bak 's/^redis running healthy/redis running starting/' "$CONTAINERS" && rm "$CONTAINERS.bak"
blocks produccion "redis(running/starting)"
sed -i.bak 's/^worker running/worker exited/' "$CONTAINERS" && rm "$CONTAINERS.bak"
blocks produccion "worker(exited/none)"
sed -i.bak '/^frontend /d' "$CONTAINERS" && rm "$CONTAINERS.bak"
blocks produccion "falta: frontend"
sed -i.bak 's/^migrate exited/migrate running/' "$CONTAINERS" && rm "$CONTAINERS.bak"
blocks produccion "migrate(en marcha)"
: > "$CONTAINERS"
blocks produccion "no hay contenedores"
FAIL_PROBE=1 blocks produccion "/health/ready no responde 200"
FAIL_PS=1 blocks produccion "docker ps falla"
FAIL_INSPECT=1 blocks produccion "docker inspect falla"
FAKE_HANG="info" blocks disco "no responde en 30 s"
FAKE_HANG="ps" blocks produccion "docker ps falla o no responde en 30 s"
FAKE_HANG="inspect" blocks produccion "docker inspect falla o no responde en 30 s"
FAKE_HANG="exec" blocks produccion "/health/ready no responde 200"

# Lock de los despliegues y del backup, y la marca de parada: bloquean y siguen ahí.
mkdir "$srv/state/lock"
run
[ -d "$srv/state/lock" ] || fail "borró el lock"
blocks lock "existe $srv/state/lock"
printf 'motivo: %s\n' "$canary" > "$srv/state/cd-halted"
run
[ -f "$srv/state/cd-halted" ] || fail "borró la marca de parada"
blocks parada "existe $srv/state/cd-halted"
ln -s "$srv/nowhere" "$srv/state/lock"
blocks lock "existe $srv/state/lock" # enlace roto: -e dice que no existe
ln -s "$srv/nowhere" "$srv/state/cd-halted"
blocks parada "existe $srv/state/cd-halted"
unreadable "$srv/state" lock "no se puede leer $srv/state" 2
if [ "$(id -u)" != 0 ]; then chmod 600 "$srv/state" && blocks lock "no se puede leer $srv/state" 2; fi # sin x, -e diría «no hay»
rmdir "$srv/state"
blocks parada "no se puede leer $srv/state" 2

# Incidente abierto en GitHub (sin credenciales): un issue, la red caída o una respuesta rara.
echo '[{"number": 1, "labels": [{"name": "incident"}]}]' > "$GH/incident"
blocks incidente "etiqueta incident"
echo '[ {"number": 2} ]' > "$GH/stop-the-line"
blocks incidente "etiqueta stop-the-line"
rm "$GH/incident"
blocks incidente "no se puede consultar GitHub (etiqueta incident)"
echo '{"message": "API rate limit exceeded"}' > "$GH/stop-the-line"
blocks incidente "respuesta inesperada de GitHub (etiqueta stop-the-line)"

# Ventana de la fase 1 en hora de Lima (UTC−5).
at 2026 11 30 9 0 && passes "lunes 09:00"
at 2026 12 1 8 59 && blocks ventana "son las 0859 en Lima; la ventana es de 0900 a 1800"
at 2026 12 3 17 59 && passes "jueves 17:59"
at 2026 12 3 18 0 && blocks ventana "son las 1800"
at 2026 12 4 12 59 && passes "viernes 12:59"
at 2026 12 4 13 0 && blocks ventana "son las 1300"
at 2026 12 5 10 0 && blocks ventana "fin de semana en Lima (2026-12-05)"
at 2026 12 6 10 0 && blocks ventana "fin de semana en Lima (2026-12-06)"
at 2026 12 1 23 30 && blocks ventana "son las 2330" # 04:30 UTC del miércoles: en Lima sigue siendo martes
FAKE_NOW="" blocks ventana "no se sabe la hora" 4
FAKE_NOW="ayer" blocks feriado "no se sabe la fecha" 4

# Feriados del Perú: fijos y Jueves y Viernes Santo (Pascua: 2026-04-05, 2027-03-28, 2028-04-16;
# 2032-03-28 y 2038-04-25, la más tardía posible, ejercitan los extremos del algoritmo).
for day in 2026-12-08 2026-04-02 2026-04-03 2027-03-25 2027-03-26 2028-04-13 2028-04-14 2027-07-28 2027-06-07 2032-03-26 2038-04-22; do
  IFS=- read -r y m d <<< "$day"
  at "$y" "$((10#$m))" "$((10#$d))" 10 0
  blocks feriado "$day es feriado nacional en el Perú"
done
at 2027 3 24 10 0 && passes "miércoles anterior al Jueves Santo de 2027"
at 2028 4 12 10 0 && passes "miércoles anterior al Jueves Santo de 2028"

# Calendario de congelación (de root, sin escritura ajena, una fecha o un rango por línea).
echo "2026-12-01 # despliegue de la otra aplicación" >> "$srv/etc/deploy-freeze"
blocks congelacion "2026-12-01 está congelado (línea 5"
echo "2026-11-30..2026-12-02" >> "$srv/etc/deploy-freeze"
blocks congelacion "2026-12-01 está congelado (línea 5"
echo "2026-12-02..2026-11-30" >> "$srv/etc/deploy-freeze"
blocks congelacion "el rango va al revés"
echo "2026-02-30" >> "$srv/etc/deploy-freeze"
blocks congelacion "línea 5"
echo "1 de diciembre" >> "$srv/etc/deploy-freeze"
blocks congelacion "se espera AAAA-MM-DD"
echo "2026-12-01..2026-12-0x" >> "$srv/etc/deploy-freeze"
blocks congelacion "se espera AAAA-MM-DD"
rm "$srv/etc/deploy-freeze"
blocks congelacion "no existe"
rm "$srv/etc/deploy-freeze" && mkdir "$srv/etc/deploy-freeze"
blocks congelacion "no es un archivo legible"
chmod 664 "$srv/etc/deploy-freeze"
blocks congelacion "no es un archivo legible"
chmod 646 "$srv/etc/deploy-freeze"
blocks congelacion "no es un archivo legible"
FAKE_STAT="$(id -u) 6x4" blocks congelacion "no es un archivo legible" # modo que no es octal
unreadable "$srv/etc/deploy-freeze" congelacion "no es un archivo legible"
at 2026 12 31 10 0 && blocks congelacion "2026-12-31 está congelado (línea 4"

# Nadie abrió runtime.env: se abre aquí para soltar al escritor y comprobar que seguía esperando.
[ ! -e "$work/opened" ] || fail "alguien abrió runtime.env"
cat "$srv/etc/runtime.env" > /dev/null
wait "$writer" || true
writer=""
[ -e "$work/opened" ] || fail "la FIFO del canario no funciona"
echo "ok   runtime.env no se abrió en ninguna ejecución"
echo "test-cd-preflight OK"
