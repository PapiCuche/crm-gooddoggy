#!/usr/bin/env bash
# Vigilante de la otra aplicación del servidor (F1-20f, ADR-048 §8). Mientras dura un despliegue del
# CRM, cada 5 s: la URL pública de la otra aplicación (a través de su borde), sus contenedores por
# `docker inspect` (también el del borde público) y MemAvailable. Con 3 comprobaciones seguidas
# fallidas de la otra aplicación (15 s), o 3 muestras seguidas con menos de 512 MiB disponibles, pide
# la parada con la marca $CRM_DEPLOY_DIR/cd-halted (§9; deploy.sh la mira entre pasos desde F1-20e).
# Nunca con `kill`. Una marca que ya existe se deja como está; solo la quita el propietario.
# Con la otra aplicación, SOLO LECTURA: un GET a su URL pública y `docker inspect`. Nunca la reinicia,
# ni entra en sus contenedores o redes, ni lee sus secretos o su entorno.
# Qué vigila lo escribe el propietario en $CRM_ENV_DIR/neighbor-watch (de root, sin escritura de grupo
# ni otros; no va al repositorio). Una cosa por línea, con # para comentarios:
#   url https://<dominio>[/ruta]   una sola: responde 2xx (sin seguir redirecciones)
#   healthy <contenedor>           al menos uno: en marcha y con healthcheck «healthy»
#   running <contenedor>           los que no tienen healthcheck (el borde, si es el caso): en marcha
# Nada lo arranca solo en este PR: lo lanzará el agente de F1-20e junto a deploy.sh.
# Uso: sudo infra/prod/neighbor-watch.sh          vigila hasta la parada (sale con 1) o TERM/INT (0);
#                                                  2: no puede vigilar o no pudo poner la marca
#      sudo infra/prod/neighbor-watch.sh --once   una comprobación de la otra aplicación, sin escribir
#                                                  nada (cd-preflight.sh): 0 sana, 1 falla o no se sabe
set -uo pipefail
umask 077

# Umbrales de ADR-048 §8 tal como se proponen (decisión 6); no se cambian desde el entorno.
interval=5 fails_to_stop=3 mem_min_mib=512 net_timeout=4
env_dir=${CRM_ENV_DIR:-/etc/crm}
state=${CRM_DEPLOY_DIR:-/var/lib/crm-deploy}
proc=${CRM_PROC_DIR:-/proc}
conf="$env_dir/neighbor-watch"
url_re='^https://[A-Za-z0-9.-]+(:[0-9]{1,5})?(/[A-Za-z0-9._~/?=&%+-]*)?$'
name_re='^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$'
url="" want_healthy="" want_running="" why=""

say() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) vigilante: $*" >&2; }

# Falla cerrado: lo que no se puede leer o no se entiende deja $why y sale con 1.
load_config() {
  local info uid mode line key value extra n=0 var
  info=$(stat -c '%u %a' "$conf" 2> /dev/null || stat -f '%u %Lp' "$conf" 2> /dev/null) ||
    { why="no existe $conf (lo escribe el propietario)"; return 1; }
  uid=${info% *} mode=${info#* }
  [[ $mode =~ ^[0-7]{1,4}$ ]] || mode=777
  if [ -L "$conf" ] || [ ! -f "$conf" ] || [ ! -r "$conf" ] || { [ "$uid" != 0 ] && [ "$uid" != "$EUID" ]; } ||
    (((8#$mode & 8#022) != 0)); then
    why="$conf no es un archivo legible de root sin escritura de grupo ni otros"
    return 1
  fi
  while IFS= read -r line || [ -n "$line" ]; do
    n=$((n + 1))
    read -r key value extra <<< "${line%%#*}"
    [ -n "$key" ] || continue
    if [ -z "$value" ] || [ -n "$extra" ]; then why="línea $n de $conf: se espera «clave valor»"; return 1; fi
    case $key in
      url)
        if [ -n "$url" ] || ! [[ $value =~ $url_re ]]; then why="línea $n de $conf: url repetida o no es https://…"; return 1; fi
        url=$value
        ;;
      healthy | running)
        [[ $value =~ $name_re ]] || { why="línea $n de $conf: nombre de contenedor no válido"; return 1; }
        var=want_$key
        printf -v "$var" '%s' "${!var} $value"
        ;;
      *) why="línea $n de $conf: clave desconocida (url, healthy o running)"; return 1 ;;
    esac
  done < "$conf" || { why="no se puede leer $conf"; return 1; }
  [ -n "$url" ] && [ -n "$want_healthy" ] || { why="$conf necesita una url y al menos un contenedor healthy"; return 1; }
}

# Una comprobación de la otra aplicación. Si falla, $why dice qué (sin la URL ni datos suyos).
neighbor_ok() {
  local code rc rows name bad=""
  code=$(curl -q -sS --proto =https --max-time "$net_timeout" -o /dev/null -w '%{http_code}' "$url" 2> /dev/null)
  rc=$?
  [[ $code =~ ^2[0-9][0-9]$ ]] || bad="; url: http ${code:-000}, curl $rc"
  # shellcheck disable=SC2086 # un nombre por palabra, validados al leer la configuración
  if rows=$(timeout "$net_timeout" docker inspect --format \
    '{{.Name}} {{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' \
    $want_healthy $want_running 2> /dev/null); then
    rows=$'\n'$rows$'\n'
    for name in $want_healthy; do
      case $rows in *$'\n'"/$name running healthy"$'\n'*) ;; *) bad="$bad; $name no está running/healthy" ;; esac
    done
    for name in $want_running; do
      case $rows in *$'\n'"/$name running "*) ;; *) bad="$bad; $name no está running" ;; esac
    done
  else
    bad="$bad; docker inspect falla o no responde en $net_timeout s (¿falta un contenedor?)"
  fi
  why=${bad#; }
  [ -z "$bad" ]
}

# MemAvailable en KiB en $avail; sin poder leerlo cuenta como poca memoria.
memory_low() {
  avail=$(awk '$1 == "MemAvailable:" { print $2 }' "$proc/meminfo" 2> /dev/null)
  [[ $avail =~ ^[0-9]{1,15}$ ]] || { avail=""; return 0; }
  [ "$avail" -lt $((mem_min_mib * 1024)) ]
}

# La marca con noclobber: si ya existe (la puso otro), se deja tal cual.
request_stop() {
  say "PARADA: $1"
  if (set -o noclobber && printf '%s parada ADR-048 §8 (vigilante): %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" > "$state/cd-halted") 2> /dev/null; then
    say "marca $state/cd-halted puesta: solo la quita el propietario (ADR-048 §9)"
  elif [ -e "$state/cd-halted" ] || [ -L "$state/cd-halted" ]; then
    say "ya existía $state/cd-halted: se deja como está"
  else
    say "NO SE PUDO PONER $state/cd-halted: para el despliegue a mano"
    return 1
  fi
}

case $# in
  0) mode=watch ;;
  1) [ "$1" = --once ] || { echo "uso: neighbor-watch.sh [--once]" >&2; exit 2; }; mode=once ;;
  *) echo "uso: neighbor-watch.sh [--once]" >&2; exit 2 ;;
esac
if ! load_config; then
  if [ "$mode" = once ]; then echo "$why"; exit 1; fi
  request_stop "no se puede vigilar: $why"
  exit 2
fi
if [ "$mode" = once ]; then
  if neighbor_ok; then echo "la otra aplicación responde y sus contenedores están sanos"; exit 0; fi
  echo "$why"
  exit 1
fi

if [ ! -d "$state" ] || [ ! -w "$state" ]; then say "no se puede escribir en $state: no podría pedir la parada"; exit 2; fi
trap 'say "terminado (TERM/INT)"; exit 0' TERM INT
say "vigila la otra aplicación ($want_healthy$want_running ) y la memoria cada $interval s"
neighbor_fails=0 memory_fails=0
while :; do
  start=$SECONDS
  if neighbor_ok; then
    neighbor_fails=0
  else
    neighbor_fails=$((neighbor_fails + 1))
    say "la otra aplicación falla ($neighbor_fails/$fails_to_stop): $why"
  fi
  if memory_low; then
    memory_fails=$((memory_fails + 1))
    say "memoria baja ($memory_fails/$fails_to_stop): MemAvailable ${avail:-ilegible} KiB (mínimo $mem_min_mib MiB)"
  else
    memory_fails=0
  fi
  if [ "$neighbor_fails" -ge "$fails_to_stop" ]; then
    request_stop "la otra aplicación falla en $neighbor_fails comprobaciones seguidas: $why" || exit 2
    exit 1
  fi
  if [ "$memory_fails" -ge "$fails_to_stop" ]; then
    request_stop "MemAvailable por debajo de $mem_min_mib MiB en $memory_fails muestras seguidas" || exit 2
    exit 1
  fi
  left=$((interval - (SECONDS - start)))
  [ "$left" -le 0 ] || sleep "$left"
done
