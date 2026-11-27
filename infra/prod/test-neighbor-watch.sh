#!/usr/bin/env bash
# Prueba de neighbor-watch.sh (F1-20f, CI: infra-prod.yml) con un servidor simulado: un árbol temporal
# con etc/, el estado de los despliegues y un /proc falso, y delante en el PATH `curl`, `docker`,
# `timeout` y `sleep` falsos. curl y docker solo aceptan la orden exacta de solo lectura (cualquier
# otra marca VIOLATION). Cada ronda la dicta una línea «http memoria contenedores [ro]» que aplica el
# `sleep` falso; tras la última, manda TERM al vigilante. Comprueba la parada justo en cada umbral,
# que una ronda buena pone la cuenta a cero, cada error de configuración (falla cerrado), que la marca
# no lleva la URL y que una marca anterior no se toca. Uso: bash infra/prod/test-neighbor-watch.sh
set -euo pipefail
src=$(cd "$(dirname "$0")" && pwd)
script="$src/neighbor-watch.sh"
work=$(mktemp -d)
cleanup() {
  chmod -R u+rwx "$work" 2> /dev/null || true
  rm -rf "$work"
}
trap cleanup EXIT
srv="$work/srv" bin="$work/bin" out="$work/out" conf="$work/srv/etc/neighbor-watch"
mkdir -p "$srv/etc" "$srv/state" "$srv/proc" "$bin"
url=https://store.example.invalid/health
REAL_TIMEOUT=$(command -v timeout) REAL_SLEEP=$(command -v sleep)
export CALLS="$work/calls" ROUNDS="$work/rounds" SEQ="$work/seq" HTTP="$srv/http" CONTAINERS="$srv/containers"
export CRM_ENV_DIR="$srv/etc" CRM_DEPLOY_DIR="$srv/state" CRM_PROC_DIR="$srv/proc" NEIGHBOR_URL="$url"
export REAL_TIMEOUT REAL_SLEEP APPLY="$bin/apply" PATH="$bin:$PATH"

cat > "$bin/curl" <<'EOF'
#!/bin/sh
echo "curl $*" >> "$CALLS"
[ "$*" = "-q -sS --proto =https --max-time 4 -o /dev/null -w %{http_code} $NEIGHBOR_URL" ] ||
  { echo "VIOLATION curl $*" >> "$CALLS"; exit 99; }
code=$(cat "$HTTP")
[ "$code" != hang ] || { printf 000; exit 28; }
printf '%s' "$code"
EOF
cat > "$bin/docker" <<'EOF'
#!/usr/bin/env bash
echo "docker $*" >> "$CALLS"
violation() { echo "VIOLATION docker $*" >> "$CALLS"; exit 99; }
[ -n "${VIA_TIMEOUT:-}" ] || violation "sin timeout: $*"
[ "$1 $2" = "inspect --format" ] || violation "$@"
[ "$3" = '{{.Name}} {{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' ] || violation "$@"
shift 3
[ "$(cat "$CONTAINERS")" != hang ] || exec "$REAL_SLEEP" 20
rc=0
for name; do grep "^/$name " "$CONTAINERS" || rc=1; done
exit $rc
EOF
cat > "$bin/timeout" <<'EOF'
#!/bin/sh
[ "$1" = 4 ] && [ "$2" = docker ] || { echo "VIOLATION timeout $*" >> "$CALLS"; exit 99; }
shift
VIA_TIMEOUT=1 exec "$REAL_TIMEOUT" 1 "$@"
EOF
# sleep: cierra la ronda; aplica la siguiente línea de $SEQ o, si no quedan, manda TERM al vigilante.
cat > "$bin/sleep" <<'EOF'
#!/usr/bin/env bash
n=$(($(cat "$ROUNDS") + 1))
echo "$n" > "$ROUNDS"
case $1 in [1-5]) ;; *) echo "VIOLATION sleep $*" >> "$CALLS" ;; esac
line=$(sed -n "$((n + 1))p" "$SEQ")
if [ -z "$line" ]; then kill -TERM "$PPID"; exit 0; fi
# shellcheck disable=SC2086
"$APPLY" $line
EOF
# apply <http|hang> <MemAvailable en KiB|none|garbage> <contenedores> [ro]
cat > "$bin/apply" <<'EOF'
#!/usr/bin/env bash
echo "$1" > "$HTTP"
case $2 in
  none) rm -f "$CRM_PROC_DIR/meminfo" ;;
  garbage) echo "MemAvailable: lots" > "$CRM_PROC_DIR/meminfo" ;;
  *) printf 'MemTotal: 8000000 kB\nMemAvailable: %s kB\n' "$2" > "$CRM_PROC_DIR/meminfo" ;;
esac
case $3 in
  ok) printf '%s\n' "/shop-web running healthy" "/shop-db running healthy" "/shop-caddy running none" ;;
  unhealthy) printf '%s\n' "/shop-web running unhealthy" "/shop-db running healthy" "/shop-caddy running none" ;;
  nohealth) printf '%s\n' "/shop-web running none" "/shop-db running healthy" "/shop-caddy running none" ;;
  exited) printf '%s\n' "/shop-web running healthy" "/shop-db running healthy" "/shop-caddy exited none" ;;
  missing) printf '%s\n' "/shop-web running healthy" "/shop-caddy running none" ;;
  hang) echo hang ;;
esac > "$CONTAINERS"
[ "${4:-}" != ro ] || chmod 500 "$CRM_DEPLOY_DIR"
EOF
chmod +x "$bin/"*

good_conf() {
  rm -rf "$conf"
  printf '# la tienda\nurl %s\nhealthy shop-web\nhealthy shop-db # base\n\nrunning shop-caddy\n' "$url" > "$conf"
  chmod 644 "$conf"
}
snap() {
  python3 -I - "$srv" <<'EOF'
import os, stat, sys
for base, dirs, files in sorted(os.walk(sys.argv[1])):
    for name in sorted(dirs + files):
        p = os.path.join(base, name)
        if name in ("http", "containers", "meminfo", "cd-halted"):
            continue  # los escribe la prueba; la marca se mira aparte
        st = os.lstat(p)
        print(p, oct(st.st_mode), "" if stat.S_ISDIR(st.st_mode) else (st.st_size, st.st_mtime_ns))
EOF
}
fail() { echo "test-neighbor-watch FAIL: $*" >&2; cat "$out" >&2; exit 1; }
reset() {
  chmod 700 "$srv/state"
  rm -f "$srv/state/cd-halted"
  good_conf
}
run() { # run [argumentos]: la ronda 1 es la línea 1 de $SEQ
  : > "$CALLS"
  echo 0 > "$ROUNDS"
  # shellcheck disable=SC2046
  "$APPLY" $(sed -n 1p "$SEQ")
  snap > "$work/before"
  if bash "$script" "$@" > "$out" 2>&1; then rc=0; else rc=$?; fi
  if grep -q ' ro$' "$SEQ"; then chmod 700 "$srv/state"; fi # lo quitó una ronda «ro»
  snap > "$work/after"
  cmp -s "$work/before" "$work/after" || fail "cambió el árbol: $(diff "$work/before" "$work/after" | head -5)"
  if grep -q VIOLATION "$CALLS"; then fail "$(grep VIOLATION "$CALLS")"; fi
  if grep -qF store.example.invalid "$out" "$srv/state/cd-halted" 2> /dev/null; then fail "la URL sale en el log o en la marca"; fi
}
watch() { # watch <ronda>…: vigila; tras la última ronda llega TERM
  printf '%s\n' "$@" > "$SEQ"
  run
}
stops() { # stops <rondas hasta la parada> <texto de la marca> <ronda>…: sale con 1 tras justo esas rondas
  local n=$1 want=$2
  shift 2
  watch "$@"
  [ "$rc" = 1 ] || fail "«$want»: exit $rc, se esperaba 1"
  [ "$(cat "$ROUNDS")" = $((n - 1)) ] || fail "«$want»: paró tras $(($(cat "$ROUNDS") + 1)) rondas, se esperaban $n"
  grep -q "^[0-9T:-]*Z parada ADR-048 §8 (vigilante): .*$want" "$srv/state/cd-halted" || fail "la marca no dice «$want»"
  echo "ok   para en la ronda $n: $want"
  reset
}
keeps() { # keeps <contexto> <ronda>…: no para; TERM tras la última sale con 0 sin marca
  local what=$1
  shift
  watch "$@"
  [ "$rc" = 0 ] || fail "$what: exit $rc"
  [ ! -e "$srv/state/cd-halted" ] || fail "$what: puso la marca"
  [ "$(cat "$ROUNDS")" = $# ] || fail "$what: $(cat "$ROUNDS") rondas"
  echo "ok   sigue: $what"
  reset
}

reset
# Solo lectura con la otra aplicación: nada de kill, restart, exec, compose ni redes en el código.
if grep -v '^[[:space:]]*#' "$script" | grep -nE '\bkill\b|docker (restart|stop|start|exec|rm|network|compose|logs)|\.env'; then
  fail "orden que no es de solo lectura"
fi
echo "ok   el código solo lee de la otra aplicación"

ok="200 4194304 ok"
keeps "todo sano" "$ok" "$ok" "$ok" "$ok"
keeps "dos fallos y una buena ponen la cuenta a cero" "503 4194304 ok" "503 4194304 ok" "$ok" "503 4194304 ok" "503 4194304 ok" "$ok"
keeps "running sin healthcheck en el borde, 204 y 299" "204 4194304 ok" "299 4194304 ok" "$ok"
stops 3 "falla en 3 comprobaciones seguidas: url: http 503, curl 0" "503 4194304 ok" "503 4194304 ok" "503 4194304 ok" "$ok"
stops 4 "url: http 301" "$ok" "301 4194304 ok" "301 4194304 ok" "301 4194304 ok" "$ok"
stops 3 "url: http 000, curl 28" "hang 4194304 ok" "hang 4194304 ok" "hang 4194304 ok"
stops 3 "shop-web no está running/healthy" "200 4194304 unhealthy" "200 4194304 unhealthy" "200 4194304 unhealthy"
stops 3 "shop-web no está running/healthy" "200 4194304 nohealth" "200 4194304 nohealth" "200 4194304 nohealth"
stops 3 "shop-caddy no está running" "200 4194304 exited" "200 4194304 exited" "200 4194304 exited"
stops 3 "docker inspect falla o no responde en 4 s" "200 4194304 missing" "200 4194304 missing" "200 4194304 missing"
stops 3 "docker inspect falla o no responde en 4 s" "200 4194304 hang" "200 4194304 hang" "200 4194304 hang"
stops 3 "falla en 3 comprobaciones seguidas" "503 4194304 ok" "200 4194304 unhealthy" "hang 4194304 missing" # causas distintas

# Memoria: 512 MiB = 524288 KiB es el mínimo; por debajo, 3 muestras seguidas paran.
keeps "512 MiB justos" "200 524288 ok" "200 524288 ok" "200 524288 ok" "200 524288 ok"
keeps "memoria baja dos veces y bien" "200 524287 ok" "200 524287 ok" "$ok" "200 524287 ok" "200 524287 ok" "$ok"
stops 3 "MemAvailable por debajo de 512 MiB en 3 muestras seguidas" "200 524287 ok" "200 524287 ok" "200 524287 ok" "$ok"
stops 3 "MemAvailable por debajo" "200 none ok" "200 none ok" "200 none ok"
stops 3 "MemAvailable por debajo" "200 garbage ok" "200 524287 ok" "200 none ok"

# Una marca anterior no se toca; si la marca no se puede poner, sale con 2.
printf 'motivo del propietario\n' > "$srv/state/cd-halted"
cp "$srv/state/cd-halted" "$work/kept"
watch "503 4194304 ok" "503 4194304 ok" "503 4194304 ok"
if [ "$rc" != 1 ] || ! cmp -s "$work/kept" "$srv/state/cd-halted"; then fail "tocó una marca anterior (exit $rc)"; fi
grep -q "ya existía" "$out" || fail "no dice que la marca ya existía"
echo "ok   una marca anterior se deja como está"
reset
if [ "$(id -u)" != 0 ]; then
  watch "503 4194304 ok" "503 4194304 ok ro" "503 4194304 ok"
  if [ "$rc" != 2 ] || ! grep -q "NO SE PUDO PONER" "$out"; then fail "sin poder poner la marca: exit $rc"; fi
  echo "ok   sin poder poner la marca sale con 2 y lo dice"
  reset
  chmod 500 "$srv/state"
  watch "$ok"
  [ "$rc" = 2 ] && [ ! -s "$CALLS" ] || fail "con $srv/state sin escritura vigila igual (exit $rc)"
  echo "ok   sin poder escribir en el estado no empieza"
  reset
fi

# --once (cd-preflight.sh): una comprobación, sin escribir nada ni mirar la memoria.
printf '%s\n' "200 none ok" > "$SEQ"
run --once
[ "$rc" = 0 ] && [ ! -e "$srv/state/cd-halted" ] && [ "$(cat "$ROUNDS")" = 0 ] || fail "--once sano: exit $rc"
printf '%s\n' "503 4194304 unhealthy" > "$SEQ"
run --once
[ "$rc" = 1 ] && [ ! -e "$srv/state/cd-halted" ] || fail "--once con fallo: exit $rc o puso la marca"
grep -q "url: http 503, curl 0; shop-web no está running/healthy" "$out" || fail "--once no dice por qué"
for args in "--twice" "--once --once"; do
  # shellcheck disable=SC2086
  run $args
  [ "$rc" = 2 ] && [ ! -s "$CALLS" ] || fail "uso «$args»: exit $rc"
done
echo "ok   --once no escribe; un uso incorrecto sale con 2 sin comprobar nada"

# Configuración: cada error falla cerrado. --once sale con 1; vigilando, pide la parada y sale con 2.
bad_conf() { # bad_conf <texto esperado>: el $conf ya está roto
  printf '%s\n' "$ok" > "$SEQ"
  run --once
  if [ "$rc" != 1 ] || [ -s "$CALLS" ] || ! grep -qF -- "$1" "$out"; then fail "--once con configuración rota «$1»: exit $rc"; fi
  run
  [ "$rc" = 2 ] && [ ! -s "$CALLS" ] || fail "vigilando con configuración rota «$1»: exit $rc"
  grep "(vigilante): no se puede vigilar: " "$srv/state/cd-halted" | grep -qF -- "$1" || fail "la marca no dice «$1»"
  echo "ok   configuración rota: $1"
  reset
}
rm "$conf" && bad_conf "no existe $conf"
chmod 664 "$conf" && bad_conf "no es un archivo legible de root"
chmod 646 "$conf" && bad_conf "no es un archivo legible de root"
mv "$conf" "$work/real" && ln -s "$work/real" "$conf" && bad_conf "no es un archivo legible de root"
rm "$conf" && mkdir "$conf" && bad_conf "no es un archivo legible de root"
if [ "$(id -u)" != 0 ]; then chmod 000 "$conf" && bad_conf "no es un archivo legible de root"; fi
echo "port 443" >> "$conf" && bad_conf "línea 7 de $conf: clave desconocida"
echo "url $url" >> "$conf" && bad_conf "línea 7 de $conf: url repetida"
sed -i.bak "s|^url .*|url http://store.example.invalid/health|" "$conf" && rm "$conf.bak" && bad_conf "línea 2 de $conf: url repetida o no es https"
sed -i.bak "s|^url .*|url https://store.example.invalid/h\$(id)|" "$conf" && rm "$conf.bak" && bad_conf "no es https"
echo "healthy ../x" >> "$conf" && bad_conf "línea 7 de $conf: nombre de contenedor no válido"
echo "healthy a b" >> "$conf" && bad_conf "línea 7 de $conf: se espera «clave valor»"
echo "running" >> "$conf" && bad_conf "línea 7 de $conf: se espera «clave valor»"
sed -i.bak '/^healthy/d' "$conf" && rm "$conf.bak" && bad_conf "necesita una url y al menos un contenedor healthy"
sed -i.bak '/^url/d' "$conf" && rm "$conf.bak" && bad_conf "necesita una url"
echo "test-neighbor-watch OK"
