#!/usr/bin/env bash
# Condiciones locales que bloquean un despliegue (F1-20d, ADR-048 §6, §7 y §9). De SOLO LECTURA:
# no arranca, para, descarga, migra ni borra nada (tampoco los locks ni la marca de parada), no usa
# `docker compose` (cargaría los env_file con secretos) y no abre ningún archivo de $CRM_ENV_DIR salvo
# el calendario de congelación. Falla cerrado: lo que no puede comprobar, bloquea.
# Lo usa el operador a mano y, cuando el propietario habilite la fase 1, el agente del servidor.
# Comprueba todo y escribe una línea por condición: «ok <condición>» o «BLOQUEA <condición>: <motivo>».
# Los motivos solo llevan rutas, fechas, números y umbrales: nunca contenidos de archivos ni del entorno.
# Uso: sudo infra/prod/cd-preflight.sh   → 0: todo en verde; 1: alguna bloquea; 2: uso incorrecto.
set -uo pipefail

# --- Umbrales de ADR-048 §7 tal como se proponen (decisión 6): revisarlos tras los 10 primeros
# despliegues. No se pueden cambiar desde el entorno.
backup_max_hours=26      # el aviso de backup/check.sh
disk_max_pct=80          # el aviso de disco de ADR-033 §9
disk_min_free_gib=20
mem_min_available_mib=2048
swap_max_pct=50
load_max_per_cpu=1.5     # media de 5 min frente al número de vCPU
docker_timeout=30        # segundos por llamada a docker: si no responde, bloquea
# Ventana de la fase 1 (§6), en hora de Lima, HHMM: lunes a jueves de 09:00 a 18:00, viernes hasta 13:00.
window_open=900 window_close=1800 friday_close=1300
# Lima es UTC−5 fija (Perú no cambia de hora): se calcula sin tzdata, que si falta cae en UTC callando.
lima_offset=$((-5 * 3600))
# Feriados nacionales del Perú (MM-DD), más Jueves y Viernes Santo, que salen de la Pascua (abajo).
# Fuente: artículo 6 del Decreto Legislativo 713 y las leyes que le añaden días (lista vigente en 2026).
# Revisar cada año: el Congreso añade feriados por ley. Los «días no laborables» que declara el
# Gobierno y las operaciones de la otra aplicación del servidor van en deploy-freeze (decisión 5).
peru_holidays="01-01 05-01 06-07 06-29 07-23 07-28 07-29 08-06 08-30 10-08 11-01 12-08 12-09 12-25"
# El stack de compose.yaml (name: crm). migrate es un job: no tiene que estar en marcha.
project=crm
services_healthy="backend ws frontend postgres redis" # con healthcheck en compose.yaml
services_running="worker worker-media beat proxy"     # sin healthcheck: basta con que estén en marcha
incident_repo=PapiCuche/crm-gooddoggy
incident_labels="incident stop-the-line"
# La misma sonda de /health/ready que la puerta de deploy.sh (test-cd-preflight.sh lo comprueba).
probe='import sys, urllib.request as u
try:
    sys.exit(0 if u.urlopen("http://127.0.0.1:8000/health/ready", timeout=5).status == 200 else 1)
except Exception:
    sys.exit(1)'

env_dir=${CRM_ENV_DIR:-/etc/crm}
state=${CRM_DEPLOY_DIR:-/var/lib/crm-deploy}
backup_state=${BACKUP_STATE_DIR:-/var/lib/crm-backup}
proc=${CRM_PROC_DIR:-/proc}
blocked=0

ok() { echo "ok      $1"; }
block() {
  echo "BLOQUEA $1: $2"
  blocked=1
}
num() { [[ ${1:-} =~ ^[0-9]{1,15}$ ]]; }

# Días desde 1970-01-01 de una fecha civil, y al revés (algoritmos de H. Hinnant, sin `date`).
days_from() { # <año> <mes> <día> → $days
  local y=$1 m=$2 d=$3 era yoe
  [ "$m" -gt 2 ] || y=$((y - 1))
  era=$((y / 400)) yoe=$((y % 400))
  days=$((era * 146097 + yoe * 365 + yoe / 4 - yoe / 100 + (153 * ((m + 9) % 12) + 2) / 5 + d - 1 - 719468))
}
civil_from() { # <días> → $ymd (AAAA-MM-DD)
  local z=$(($1 + 719468)) era doe yoe doy mp y m
  era=$((z / 146097)) doe=$((z % 146097))
  yoe=$(((doe - doe / 1460 + doe / 36524 - doe / 146096) / 365))
  doy=$((doe - (365 * yoe + yoe / 4 - yoe / 100))) mp=$(((5 * doy + 2) / 153))
  m=$((mp < 10 ? mp + 3 : mp - 9)) y=$((yoe + era * 400 + (m <= 2)))
  printf -v ymd '%04d-%02d-%02d' "$y" "$m" $((doy - (153 * mp + 2) / 5 + 1))
}
easter_days() { # <año> → $days del Domingo de Pascua (algoritmo gregoriano anónimo)
  local y=$1 a b c d e f g h i k l m
  a=$((y % 19)) b=$((y / 100)) c=$((y % 100)) d=$((b / 4)) e=$((b % 4)) f=$(((b + 8) / 25))
  g=$(((b - f + 1) / 3)) h=$(((19 * a + b - d - g + 15) % 30)) i=$((c / 4)) k=$((c % 4))
  l=$(((32 + 2 * e + 2 * i - h - k) % 7)) m=$(((a + 11 * h + 22 * l) / 451))
  days_from "$y" $(((h + l - 7 * m + 114) / 31)) $(((h + l - 7 * m + 114) % 31 + 1))
}

check_backup() {
  local marker="$backup_state/last-success" last age
  last=$(cat "$marker" 2> /dev/null) || { block backup "no se puede leer $marker"; return; }
  num "$last" || { block backup "$marker no es una marca de tiempo"; return; }
  [ -n "$now" ] || { block backup "no se sabe la hora"; return; }
  age=$((now - 10#$last))
  [ "$age" -ge 0 ] || { block backup "$marker está en el futuro"; return; }
  [ "$age" -le $((backup_max_hours * 3600)) ] ||
    { block backup "el último backup correcto es de hace $((age / 3600)) h (máximo $backup_max_hours h)"; return; }
  ok backup
}

check_disk() {
  local docker_dir dir line size avail pct fail=""
  docker_dir=$(timeout "$docker_timeout" docker info --format '{{.DockerRootDir}}' 2> /dev/null) && [[ $docker_dir == /* ]] ||
    { block disco "docker info no dice el directorio de Docker (o no responde en $docker_timeout s)"; return; }
  for dir in / "$docker_dir"; do
    line=$(df -Pk "$dir" 2> /dev/null | awk 'NR == 2 { print $2, $4, $5 }')
    read -r size avail pct <<< "$line"
    pct=${pct%\%}
    if ! num "$size" || ! num "$avail" || ! num "$pct"; then
      fail="$fail; df no da el uso de $dir"
    elif [ "$pct" -gt "$disk_max_pct" ] || [ "$avail" -lt $((disk_min_free_gib * 1024 * 1024)) ]; then
      fail="$fail; $dir al $pct % con $((avail / 1024 / 1024)) GiB libres (máximo $disk_max_pct %, mínimo $disk_min_free_gib GiB)"
    fi
  done
  if [ -n "$fail" ]; then block disco "${fail#; }"; else ok disco; fi
}

check_memory() {
  local avail total free
  read -r avail total free <<< "$(awk '$1 == "MemAvailable:" { a = $2 } $1 == "SwapTotal:" { t = $2 }
    $1 == "SwapFree:" { f = $2 } END { print a, t, f }' "$proc/meminfo" 2> /dev/null)"
  if ! num "$avail" || ! num "$total" || ! num "$free" || [ "$free" -gt "$total" ]; then
    block memoria "no se puede leer MemAvailable, SwapTotal y SwapFree de $proc/meminfo"
  elif [ "$avail" -lt $((mem_min_available_mib * 1024)) ]; then
    block memoria "$((avail / 1024)) MiB disponibles (mínimo $mem_min_available_mib MiB)"
  elif [ "$total" -gt 0 ] && [ $(((total - free) * 100)) -gt $((swap_max_pct * total)) ]; then
    block memoria "swap en uso al $(((total - free) * 100 / total)) % (máximo $swap_max_pct %)"
  else
    ok memoria
  fi
}

check_load() {
  local load cpus
  read -r _ load _ 2> /dev/null < "$proc/loadavg" || load=""
  cpus=$(nproc --all 2> /dev/null) || cpus="" # --all: sin --all, OMP_NUM_THREADS lo cambia
  if ! [[ $load =~ ^[0-9]+(\.[0-9]+)?$ ]] || ! num "$cpus" || [ "$cpus" -lt 1 ]; then
    block carga "no se puede leer la carga de $proc/loadavg o el número de vCPU"
  elif awk -v l="$load" -v c="$cpus" -v k="$load_max_per_cpu" 'BEGIN { exit !(l > k * c) }'; then
    block carga "media de 5 min $load con $cpus vCPU (máximo $load_max_per_cpu × vCPU)"
  else
    ok carga
  fi
}

check_production() {
  local ids rows svc status health id backend="" seen=" " bad="" missing="" want
  command -v docker > /dev/null || { block produccion "falta docker"; return; }
  ids=$(timeout "$docker_timeout" docker ps -aq --filter "label=com.docker.compose.project=$project" 2> /dev/null) ||
    { block produccion "docker ps falla o no responde en $docker_timeout s"; return; }
  [ -n "$ids" ] || { block produccion "no hay contenedores del proyecto $project"; return; }
  # shellcheck disable=SC2086 # un id por palabra
  rows=$(timeout "$docker_timeout" docker inspect --format '{{index .Config.Labels "com.docker.compose.service"}} {{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}} {{.Id}}' $ids 2> /dev/null) ||
    { block produccion "docker inspect falla o no responde en $docker_timeout s"; return; }
  while read -r svc status health id; do
    if [ "$svc" = migrate ]; then
      [ "$status" != running ] || bad="$bad migrate(en marcha)"
      continue
    fi
    case " $services_healthy " in *" $svc "*) want=healthy ;; *) want="none healthy" ;; esac
    if [ "$status" != running ] || [[ " $want " != *" $health "* ]]; then
      bad="$bad $svc($status/$health)"
      continue
    fi
    seen="$seen$svc "
    if [ "$svc" = backend ] && [[ $id =~ ^[0-9a-f]{64}$ ]]; then backend=$id; fi
  done <<< "$rows"
  for svc in $services_healthy $services_running; do
    case $seen in *" $svc "*) ;; *) missing="$missing $svc" ;; esac
  done
  if [ -n "$bad$missing" ]; then
    block produccion "no está sano:${bad}${missing:+ falta:$missing} (es un incidente, no un despliegue)"
  elif ! timeout "$docker_timeout" docker exec "$backend" python -c "$probe" > /dev/null 2>&1; then
    block produccion "/health/ready no responde 200 desde el backend"
  else
    ok produccion
  fi
}

# $CRM_DEPLOY_DIR sin permiso de lectura hace que -e diga «no existe»: eso no es «no hay lock».
state_readable() { [ -d "$state" ] && [ -r "$state" ] && [ -x "$state" ]; }
check_lock() {
  state_readable || { block lock "no se puede leer $state (¿sin sudo?)"; return; }
  if [ -e "$state/lock" ] || [ -L "$state/lock" ]; then
    block lock "existe $state/lock: despliegue, migrate o backup en curso, o uno cortado (README.md); nunca se borra desde aquí"
  else
    ok lock
  fi
}
check_halt() {
  state_readable || { block parada "no se puede leer $state (¿sin sudo?)"; return; }
  if [ -e "$state/cd-halted" ] || [ -L "$state/cd-halted" ]; then
    block parada "existe $state/cd-halted: la línea está parada; solo la quita el propietario (ADR-048 §9)"
  else
    ok parada
  fi
}

# GitHub, sin credenciales: el repositorio es público. -q: sin ~/.curlrc, que podría añadir cabeceras.
check_incident() {
  local label body fail=""
  for label in $incident_labels; do
    if ! body=$(curl -q -fsS --proto =https --max-time 10 -H 'Accept: application/vnd.github+json' \
      "https://api.github.com/repos/$incident_repo/issues?state=open&labels=$label&per_page=1" 2> /dev/null); then
      fail="$fail; no se puede consultar GitHub (etiqueta $label)"
      continue
    fi
    body=$(printf '%s' "$body" | tr -d ' \t\r\n')
    case $body in
      '[]') ;;
      '[{'*) fail="$fail; hay un issue abierto con la etiqueta $label" ;;
      *) fail="$fail; respuesta inesperada de GitHub (etiqueta $label)" ;;
    esac
  done
  if [ -n "$fail" ]; then block incidente "${fail#; }"; else ok incidente; fi
}

check_window() {
  local wd secs hhmm close=$window_close
  [ -n "$now" ] || { block ventana "no se sabe la hora"; return; }
  wd=$(((today + 4) % 7)) # 0 = domingo: el 1970-01-01 fue jueves
  secs=$(((now + lima_offset) % 86400))
  hhmm=$((secs / 3600)) && hhmm=$((hhmm * 100 + secs % 3600 / 60))
  [ "$wd" != 5 ] || close=$friday_close
  if [ "$wd" = 0 ] || [ "$wd" = 6 ]; then
    block ventana "es fin de semana en Lima ($today_ymd)"
  elif [ "$hhmm" -lt "$window_open" ] || [ "$hhmm" -ge "$close" ]; then
    block ventana "son las $(printf '%04d' "$hhmm") en Lima; la ventana es de $(printf '%04d' "$window_open") a $close"
  else
    ok ventana
  fi
}

check_holiday() {
  local y
  [ -n "$now" ] || { block feriado "no se sabe la fecha"; return; }
  y=${today_ymd%%-*}
  easter_days "$((10#$y))"
  if [[ " $peru_holidays " == *" ${today_ymd#*-} "* ]] || [ "$today" = $((days - 3)) ] || [ "$today" = $((days - 2)) ]; then
    block feriado "$today_ymd es feriado nacional en el Perú"
  else
    ok feriado
  fi
}

# deploy-freeze: de root (o de quien ejecuta, como check_file de backup/lib.sh, para las pruebas sin
# root), sin escritura de grupo ni otros. Líneas: vacía, # comentario, AAAA-MM-DD o AAAA-MM-DD..AAAA-MM-DD,
# con un # comentario detrás si se quiere (p. ej., una campaña de la otra aplicación).
check_freeze() {
  local file="$env_dir/deploy-freeze" info uid mode line n=0 from to d hit="" re
  re='^([0-9]{4})-([0-9]{2})-([0-9]{2})$'
  info=$(stat -c '%u %a' "$file" 2> /dev/null || stat -f '%u %Lp' "$file" 2> /dev/null) ||
    { block congelacion "no existe $file (el propietario lo mantiene; vacío si no hay fechas)"; return; }
  uid=${info% *} mode=${info#* }
  [[ $mode =~ ^[0-7]{1,4}$ ]] || mode=777 # un modo que no es octal no se cree: bloquea
  if { [ "$uid" != 0 ] && [ "$uid" != "$(id -u)" ]; } || (((8#$mode & 8#022) != 0)) || [ ! -f "$file" ] || [ ! -r "$file" ]; then
    block congelacion "$file no es un archivo legible de root sin escritura de grupo ni otros"
    return
  fi
  [ -n "$now" ] || { block congelacion "no se sabe la fecha"; return; }
  while IFS= read -r line || [ -n "$line" ]; do
    n=$((n + 1))
    line=${line%%#*}
    line=$(printf '%s' "$line" | tr -d ' \t\r')
    [ -n "$line" ] || continue
    from=${line%%..*} to=${line#*..}
    for d in from to; do
      if [[ ${!d} =~ $re ]]; then
        days_from "$((10#${BASH_REMATCH[1]}))" "$((10#${BASH_REMATCH[2]}))" "$((10#${BASH_REMATCH[3]}))"
        civil_from "$days"
        [ "$ymd" = "${!d}" ] && printf -v "$d" '%s' "$days" && continue
      fi
      block congelacion "línea $n de $file: se espera AAAA-MM-DD o AAAA-MM-DD..AAAA-MM-DD"
      return
    done
    [ "$from" -le "$to" ] || { block congelacion "línea $n de $file: el rango va al revés"; return; }
    if [ "$today" -ge "$from" ] && [ "$today" -le "$to" ]; then hit=$n; fi
  done < "$file" || { block congelacion "no se puede leer $file"; return; }
  if [ -n "$hit" ]; then
    block congelacion "$today_ymd está congelado (línea $hit de $file)"
  else
    ok congelacion
  fi
}

[ $# -eq 0 ] || { echo "uso: sudo infra/prod/cd-preflight.sh (sin argumentos)" >&2; exit 2; }
now=$(date -u +%s 2> /dev/null) && num "$now" || now=""
today="" today_ymd=""
if [ -n "$now" ]; then
  today=$(((now + lima_offset) / 86400))
  civil_from "$today"
  today_ymd=$ymd
fi
check_backup
check_disk
check_memory
check_load
check_production
check_lock
check_halt
check_incident
check_window
check_holiday
check_freeze
if [ "$blocked" = 0 ]; then echo "PREFLIGHT OK"; else echo "PREFLIGHT BLOQUEADO: no despliegues"; fi
exit "$blocked"
