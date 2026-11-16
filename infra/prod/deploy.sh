#!/usr/bin/env bash
# Despliegue de producción por digest (F1-15, ADR-033 §7). Procedimiento y vuelta atrás: README.md.
#   1. valida el release nuevo: las cinco imágenes, cada una fijada por @sha256 (nada más), y su
#      REVISION, que tiene que ser el commit del checkout de infra/prod (F1-15b);
#   2. `pull` de esos digests;
#   3. arranca postgres y redis de la versión en marcha (también si estaban parados) y hace el volcado
#      previo con `backup/backup.sh predeploy` (F1-16): cifrado con age y subido al destino de los
#      backups, db/predeploy/, 7 días (ADR-033 §7.2 y §8.2). Sin volcado no se migra;
#   4. job `migrate` (solo crm_migrator) contra postgres y redis de la versión en marcha; si
#      falla, para: el runtime anterior sigue en marcha;
#   5. `up -d --wait` del stack y puerta de salud: /health/ready desde dentro, smoke.sh y la página
#      de entrada por https a través de Caddy. `up` vuelve a lanzar `migrate` (el runtime depende de
#      él en compose.yaml): ya no queda nada que aplicar y termina en segundos;
#   6. si la puerta falla, vuelve sola a los digests de release.env (la versión en marcha) y sale
#      con error. Nunca deshace migraciones (expandir y contraer, ADR-033 §7).
# Solo cuando todo pasa, release.env pasa a release.env.previous y el nuevo a release.env.
# Ejecútalo dentro de tmux: una señal (Ctrl-C, kill) corta el despliegue y deja el lock puesto.
# Uso: sudo infra/prod/deploy.sh [--allow-revision-mismatch] <release.env nuevo> [opciones de curl para smoke.sh]
#      sudo infra/prod/deploy.sh [--allow-revision-mismatch] --rollback [opciones de curl]   (a release.env.previous)
#      sudo infra/prod/deploy.sh --migrate   (solo `migrate` de la versión en marcha: crm-migrate.timer)
set -euo pipefail
umask 077

here=$(cd "$(dirname "$0")" && pwd)
env_dir=${CRM_ENV_DIR:-/etc/crm}
state=${CRM_DEPLOY_DIR:-/var/lib/crm-deploy}
health_timeout=${CRM_HEALTH_TIMEOUT:-180}
current="$env_dir/release.env"
previous="$env_dir/release.env.previous"
next="$env_dir/release.env.next"
images="BACKEND_IMAGE FRONTEND_IMAGE PROXY_IMAGE POSTGRES_IMAGE REDIS_IMAGE"
# Las imágenes propias solo salen de images.yml (ADR-033 §7).
ghcr="ghcr.io/papicuche/crm-gooddoggy"
digest='sha256:[0-9a-f]{64}$'
locked="" finished="" allow_mismatch=""

# El registro solo lleva fechas, pasos, digests y rutas: nunca el contenido de los archivos de entorno.
log() {
  local line
  line="$(date -u +%Y-%m-%dT%H:%M:%SZ) $*"
  echo "$line" >&2 || true
  if [ -n "$locked" ]; then echo "$line" >> "$state/deploy.log"; fi
}
die() { finished=1; log "ERROR: $*"; exit 1; }
compose() {
  local release=$1
  shift
  docker compose -f "$here/compose.yaml" --env-file "$release" "$@"
}
git_root() { git -c core.fsmonitor= -c safe.directory="$root" -C "$root" "$@"; }

# El lock solo se quita en una salida prevista (éxito, error antes de tocar el runtime, tras la
# vuelta atrás). Una señal o un error inesperado lo dejan, con release.env.next, para revisar.
cleanup() {
  [ -n "$locked" ] || return 0
  if [ -n "$finished" ]; then
    rm -f "$next"
    rmdir "$state/lock"
  else
    log "INTERRUMPIDO: quedan $state/lock y $next. Revisa docker compose ps antes de repetir"
  fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap '' HUP

# Copia KEY=valor del release a la salida estándar, solo si es una de las cinco imágenes y va por
# digest. No se ejecuta el archivo (`source`): un valor con comillas, espacios o `$` no pasa.
release_lines() {
  local file=$1 line key value name seen=" "
  [ -f "$file" ] || die "no existe el release $file"
  while IFS= read -r line || [ -n "$line" ]; do
    case $line in '' | '#'*) continue ;; esac
    key=${line%%=*} value=${line#*=}
    [ "$key" != "$line" ] || die "$file: línea sin KEY=valor"
    if [ "$key" = CRM_ENV_DIR ]; then
      [ "$value" = "$env_dir" ] || die "$file: CRM_ENV_DIR distinto de $env_dir"
      continue
    fi
    if [ "$key" = REVISION ]; then
      [[ $value =~ ^[0-9a-f]{40}$ ]] || die "$file: REVISION no es el SHA de un commit (40 hex)"
      [[ $seen != *" REVISION "* ]] || die "$file: REVISION repetida"
      seen="$seen$key "
      echo "$key=$value"
      continue
    fi
    case " $images " in *" $key "*) ;; *) die "$file: variable no permitida: $key" ;; esac
    case $seen in *" $key "*) die "$file: $key repetida" ;; esac
    if [ "$key" = POSTGRES_IMAGE ] || [ "$key" = REDIS_IMAGE ]; then
      [[ $value =~ ^[a-z0-9][a-z0-9._/:-]*@$digest ]] || die "$file: $key no va fijada por @sha256:<digest>"
    else
      name=$(echo "${key%_IMAGE}" | tr '[:upper:]' '[:lower:]')
      [ "${value%%@*}" = "$ghcr/$name" ] || die "$file: $key no es $ghcr/$name@sha256:<digest>"
      [[ ${value#*@} =~ ^$digest ]] || die "$file: $key no es $ghcr/$name@sha256:<digest>"
    fi
    seen="$seen$key "
    echo "$key=$value"
  done < "$file"
  for key in $images REVISION; do
    case $seen in *" $key "*) ;; *) die "$file: falta $key" ;; esac
  done
  echo "CRM_ENV_DIR=$env_dir"
}

# Versión mayor de PostgreSQL (postgres:18.6@sha256:… → 18): cambiarla exige pg_upgrade, no un deploy.
pg_major() {
  sed -n 's|^POSTGRES_IMAGE=.*postgres:\([0-9][0-9]*\)[^/@]*@sha256:.*|\1|p' "$1"
}

# Volcado previo de la versión en marcha, con el backup de F1-16: pg_dump cifrado al vuelo para las
# claves de backup y de despliegue y subido a db/predeploy/ del destino. Nunca queda en claro en el disco.
# postgres ya está en marcha (main lo arranca con $current antes): nunca se migra sin volcado.
predeploy_dump() {
  CRM_RELEASE_ENV="$current" "$here/backup/backup.sh" predeploy ||
    die "falló el volcado previo (backup.sh predeploy): no se ha migrado ni arrancado el runtime"
  log "volcado previo en el destino de los backups (db/predeploy/)"
}

# /health/ready desde dentro de la red del stack (desde Internet es 404), smoke.sh y la página de
# entrada por https a través de Caddy (ADR-033 §7, paso 6): smoke.sh no prueba la ruta al frontend.
gate() {
  local release=$1 waited=0
  shift
  until compose "$release" exec -T backend python -c 'import sys, urllib.request as u
try:
    sys.exit(0 if u.urlopen("http://127.0.0.1:8000/health/ready", timeout=5).status == 200 else 1)
except Exception:
    sys.exit(1)' 2> /dev/null; do
    waited=$((waited + 5))
    [ "$waited" -lt "$health_timeout" ] || { log "/health/ready no responde 200 en ${health_timeout}s"; return 1; }
    sleep 5
  done
  log "/health/ready 200"
  CRM_RELEASE_ENV="$release" sh "$here/smoke.sh" "$origin" "$@" || { log "smoke.sh falló"; return 1; }
  curl -fsS --connect-timeout 5 --max-time 30 -o /dev/null "$@" "$origin/login" || { log "la entrada $origin/login no responde 200"; return 1; }
  log "entrada $origin/login 200"
}

start() {
  compose "$1" up -d --wait --wait-timeout "$health_timeout" --remove-orphans
}

# `finished` justo antes de cada salida: una señal durante la vuelta atrás deja el lock.
rollback() {
  if [ ! -f "$current" ]; then
    log "ROLLBACK IMPOSIBLE: no hay versión anterior (primer despliegue). El stack queda como está"
    finished=1
    exit 2
  fi
  log "ROLLBACK: vuelvo a los digests de $current"
  if start "$current" && gate "$current" "$@"; then
    log "DESPLIEGUE FALLIDO. ROLLBACK OK: sigue en marcha la versión de $current"
    finished=1
    exit 1
  fi
  log "ROLLBACK FALLIDO: el stack no pasa la puerta con $current. Sigue el runbook (README.md)"
  finished=1
  exit 2
}

main() {
  if [ "${1:-}" = --allow-revision-mismatch ]; then
    allow_mismatch=1
    shift
  fi
  [ $# -ge 1 ] || die "uso: deploy.sh [--allow-revision-mismatch] <release.env nuevo> | --rollback | --migrate [opciones de curl]"
  local candidate=$1 domain rev stores revision
  shift
  [ "$(id -u)" = 0 ] || die "ejecútalo con sudo: los archivos de entorno son de root (ADR-033 §6)"
  command -v docker > /dev/null || die "falta docker"
  domain=$(sed -n 's/^CRM_DOMAIN=//p' "$env_dir/proxy.env" 2> /dev/null | tail -n 1)
  [[ $domain =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$ ]] ||
    die "CRM_DOMAIN de $env_dir/proxy.env falta o no es un nombre de dominio (sin comillas ni esquema)"
  origin="https://crm.$domain"

  # Solo se despliega lo revisado: un checkout con cambios locales en infra/prod no despliega.
  root=$(cd "$here/../.." && pwd)
  if rev=$(git_root rev-parse HEAD 2> /dev/null); then
    [ -z "$(git_root status --porcelain -- infra/prod)" ] ||
      die "infra/prod tiene cambios sin commit: despliega solo lo que está en main"
  else
    rev="(no es un checkout de git)"
  fi

  mkdir -p "$state"
  chmod 700 "$state"
  mkdir "$state/lock" 2> /dev/null ||
    die "hay otro despliegue o el backup nocturno en curso, o un despliegue anterior se cortó: revisa $state/deploy.log (y journalctl -u crm-backup) antes de borrar $state/lock"
  locked=1

  # ADR-033 §7: las particiones de auditoría se crean al migrar, 12 meses por delante. El
  # temporizador mensual (crm-migrate.timer) repite `migrate` de la versión en marcha sin desplegar.
  if [ "$candidate" = --migrate ]; then
    [ -f "$current" ] || die "no hay $current: todavía no se ha desplegado nada"
    release_lines "$current" > /dev/null
    compose "$current" run --rm --no-deps migrate || die "falló migrate de la versión en marcha ($current)"
    finished=1
    log "migrate mensual OK: $current"
    return
  fi

  if [ "$candidate" = --rollback ]; then
    [ -f "$previous" ] || die "no hay $previous al que volver"
    candidate=$previous
    log "vuelta atrás manual a $previous"
  fi
  # La versión en marcha también se valida: la vuelta atrás automática la usa.
  if [ -f "$current" ]; then release_lines "$current" > /dev/null; fi
  release_lines "$candidate" > "$next"
  [ -n "$(pg_major "$next")" ] || die "POSTGRES_IMAGE sin versión (postgres:<mayor>.<menor>@sha256:…)"
  if [ -f "$current" ] && [ "$(pg_major "$next")" != "$(pg_major "$current")" ]; then
    die "POSTGRES_IMAGE cambia de versión mayor ($(pg_major "$current") → $(pg_major "$next")): exige pg_upgrade en una ventana"
  fi
  # El checkout de infra/prod (compose.yaml, smoke.sh, backup/) tiene que ser el de las imágenes.
  revision=$(sed -n 's/^REVISION=//p' "$next")
  if [ "$revision" != "$rev" ]; then
    [ -n "$allow_mismatch" ] ||
      die "el release es de $revision e infra/prod está en $rev: git checkout $revision (o --allow-revision-mismatch)"
    log "AVISO: release de $revision con infra/prod en $rev (--allow-revision-mismatch)"
  fi
  log "inicio: infra/prod en $rev; release $(grep -e '_IMAGE=' -e '^REVISION=' "$next" | tr '\n' ' ')"
  compose "$next" config -q
  compose "$next" pull --quiet || die "falló el pull de los digests: no se ha tocado nada"
  log "pull OK"

  # postgres y redis siguen en los digests en marcha hasta `start`, que cubren la puerta y la vuelta
  # atrás. Solo el primer despliegue los arranca con el release nuevo, y no tiene nada que volcar.
  stores=$next
  if [ -f "$current" ]; then stores=$current; fi
  compose "$stores" up -d --wait postgres redis || die "postgres o redis no arrancan: el runtime no se ha tocado"
  if [ -f "$current" ]; then predeploy_dump; fi
  compose "$next" run --rm --no-deps migrate ||
    die "fallaron las migraciones: el runtime anterior sigue en marcha y release.env no cambia"
  log "migrate OK"

  if start "$next" && gate "$next" "$@"; then
    if ! cmp -s "$next" "$current" 2> /dev/null; then
      if [ -f "$current" ]; then mv "$current" "$previous"; fi
    fi
    mv "$next" "$current"
    finished=1
    log "DESPLIEGUE OK: $current"
    return
  fi
  log "la puerta de salud falló con el release nuevo"
  rollback "$@"
}

main "$@"
