# shellcheck shell=bash
# Lock común del servidor compartido (F1-20f, ADR-048 §7 y decisión 9): un despliegue del CRM y su
# backup nocturno no corren a la vez que otro proceso que tome el mismo lock (cuando el propietario lo
# autorice, el despliegue de la otra aplicación; contrato en README.md, «Lock común del servidor»).
# Lo cargan deploy.sh y backup/lib.sh. Sin efectos al cargarlo.
#
# Es flock(2) exclusivo sobre /var/lib/host-deploy/lock (directorio root 0755, archivo root 0644). El
# núcleo lo suelta cuando muere quien lo tiene: no puede quedar uno cortado y nadie lo borra nunca.
# Orden en todas partes: primero el lock del CRM ($CRM_DEPLOY_DIR/lock), después este; así no hay
# abrazo mortal, y un despliegue cortado (que deja el del CRM) no hace que el backup tome este.
host_lock_file=${CRM_HOST_LOCK_DIR:-/var/lib/host-deploy}/lock

# host_lock <segundos de espera; 0: no espera>: lo toma en un descriptor que se hereda y sigue abierto
# hasta salir (o host_unlock). Falla cerrado: sin flock, o con el directorio o el archivo de otro, con
# escritura de grupo u otros o como enlace, no lo toma y sale con 1 diciendo por qué.
host_lock() {
  local dir=${host_lock_file%/*} path info uid mode
  command -v flock > /dev/null || { echo "falta flock (util-linux): no se puede tomar el lock común $host_lock_file" >&2; return 1; }
  [ -e "$dir" ] || [ -L "$dir" ] || mkdir -m 755 "$dir" || return 1
  for path in "$dir" "$host_lock_file"; do
    if [ "$path" = "$host_lock_file" ] && [ ! -e "$path" ] && [ ! -L "$path" ]; then
      (umask 022 && : >> "$path") || return 1
    fi
    info=$(stat -c '%u %a' "$path" 2> /dev/null || stat -f '%u %Lp' "$path" 2> /dev/null) || info="x 777"
    uid=${info% *} mode=${info#* }
    [[ $mode =~ ^[0-7]{1,4}$ ]] || mode=777
    if [ -L "$path" ] || { [ "$uid" != 0 ] && [ "$uid" != "$EUID" ]; } || (((8#$mode & 8#022) != 0)); then
      echo "$path no es de root, tiene escritura de grupo u otros o es un enlace: no se toma el lock común" >&2
      return 1
    fi
  done
  [ -f "$host_lock_file" ] || { echo "$host_lock_file no es un archivo: no se toma el lock común" >&2; return 1; }
  exec {host_lock_fd}< "$host_lock_file" || return 1
  if [ "$1" -gt 0 ]; then
    flock -w "$1" "$host_lock_fd" && return 0
  elif flock -n "$host_lock_fd"; then
    return 0
  fi
  host_unlock
  echo "el lock común $host_lock_file está tomado (¿un despliegue de la otra aplicación?)" >&2
  return 1
}

host_unlock() {
  [ -z "${host_lock_fd:-}" ] || exec {host_lock_fd}<&-
  host_lock_fd=''
}
