# shellcheck shell=sh
# Modo del servidor (ADR-046): qué archivos de Compose forman el stack en ESTE servidor. Lo leen
# deploy.sh, smoke.sh y backup/lib.sh (backup, check, restore), así que despliegue, migraciones,
# volcados, restauración, prueba de humo y vuelta atrás usan siempre la misma configuración.
#
# Se declara UNA vez por servidor, en $CRM_ENV_DIR/host-mode (root:0600), con una sola palabra:
#   dedicated  (o sin archivo)  ADR-033 tal cual: compose.yaml, el proxy publica 80 y 443;
#   shared                      compose.yaml + compose.shared.yaml: el proxy no publica puertos y
#                               recibe el tráfico del borde público del servidor (ADR-046).
# Un archivo y no una variable de entorno: `sudo` no pasa las variables, y un despliegue que se
# olvidara de ella intentaría publicar 80/443 en un servidor donde ya son de otra aplicación.
# CRM_COMPOSE_OVERRIDE, si viene, manda: el simulacro de backup (drill.sh) monta su propio override.

# crm_compose_override <directorio de infra/prod>: imprime la ruta del override o nada. Sale con 1
# (y lo dice) si el archivo no se puede leer o trae otra cosa: mejor parar que desplegar a ciegas.
crm_compose_override() {
  if [ -n "${CRM_COMPOSE_OVERRIDE:-}" ]; then
    printf '%s\n' "$CRM_COMPOSE_OVERRIDE"
    return 0
  fi
  crm_mode_file="${CRM_ENV_DIR:-/etc/crm}/host-mode"
  [ -e "$crm_mode_file" ] || return 0
  crm_mode=$(tr -d ' \t\r\n' < "$crm_mode_file") || {
    echo "no se puede leer $crm_mode_file" >&2
    return 1
  }
  case $crm_mode in
    "" | dedicated) ;;
    shared) printf '%s\n' "$1/compose.shared.yaml" ;;
    *)
      echo "$crm_mode_file: modo «$crm_mode» desconocido (dedicated o shared)" >&2
      return 1
      ;;
  esac
}
