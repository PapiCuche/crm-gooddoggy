#!/bin/sh
# Valida infra/prod sin servidor ni secretos (F1-13): `docker compose config` con valores ficticios
# sacados de las secciones de .env.production.example, y comprueba sobre el resultado que solo el
# proxy publica puertos (80 y 443), que no hay build, Garage ni Mailpit, que cada imagen va por
# digest y cada contenedor tiene límite de memoria y rotación de logs, y que cada archivo de
# entorno llega solo a su destinatario. Con --caddy además construye la imagen de infra/prod/caddy
# y pasa `caddy validate` al Caddyfile. Uso: sh infra/prod/validate.sh [--caddy]
set -eu
here=$(cd "$(dirname "$0")" && pwd)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

# Cada sección `# ==== <archivo> ====` de la plantilla → un archivo con valores ficticios.
awk -v dir="$work" '
  /^# =+ [a-z]+\.env / { file = dir "/" $3; printf "" > file; next }
  /^[A-Z][A-Z0-9_]*=$/ { print $0 "ci-config-only" >> file }
' "$here/.env.production.example"
digest="@sha256:$(printf '0%.0s' $(seq 64))"
sed -i.bak -e "s|^\([A-Z]*_IMAGE\)=.*|\1=example.invalid/crm${digest}|" -e "s|^CRM_ENV_DIR=.*|CRM_ENV_DIR=$work|" \
  "$work/release.env"

docker compose -f "$here/compose.yaml" --env-file "$work/release.env" config -q
docker compose -f "$here/compose.yaml" --env-file "$work/release.env" config --format json > "$work/config.json"
python3 - "$work/config.json" <<'PY'
import json, sys

services = json.load(open(sys.argv[1]))["services"]
runtime = {"backend", "ws", "worker", "worker-media", "beat"}
expected = runtime | {"postgres", "redis", "migrate", "frontend", "proxy"}
assert set(services) == expected, f"servicios inesperados: {set(services) ^ expected}"
for name, svc in services.items():
    assert "build" not in svc, f"{name}: build en producción"
    assert "@sha256:" in svc["image"], f"{name}: imagen sin digest"
    assert svc.get("mem_limit"), f"{name}: sin límite de memoria"
    log = svc.get("logging", {})
    assert log.get("driver") == "json-file" and log.get("options", {}).get("max-size"), f"{name}: logs sin rotación"
    ports = svc.get("ports", [])
    if name != "proxy":
        assert not ports and not svc.get("expose"), f"{name}: publica puertos"
proxy = sorted((p["target"], p["published"], p.get("protocol", "tcp")) for p in services["proxy"]["ports"])
assert proxy == [(80, "80", "tcp"), (443, "443", "tcp")], f"proxy publica {proxy}"
forbidden = {"DATABASE_MIGRATOR_URL", "CRM_MIGRATOR_PASSWORD", "POSTGRES_PASSWORD", "REDIS_PASSWORD"}
for name in runtime:
    env = services[name]["environment"]
    assert not forbidden & set(env), f"{name}: recibe {forbidden & set(env)}"
    assert env["DJANGO_SETTINGS_MODULE"] == "config.settings.production"
    assert env["DATABASE_URL"] and "data" in services[name]["networks"]
assert set(services["migrate"]["environment"]) == {"DATABASE_MIGRATOR_URL", "DJANGO_SETTINGS_MODULE"}
for name in ("proxy", "frontend"):
    assert "data" not in services[name]["networks"], f"{name}: en la red de datos"
print("infra/prod: compose válido; solo el proxy publica 80 y 443")
PY

# F1-15: las bases de la imagen del proxy, también por digest (revisión de #845).
if grep -E '^FROM ' "$here/caddy/Dockerfile" | grep -v '@sha256:[0-9a-f]\{64\}'; then
  echo "infra/prod/caddy/Dockerfile: FROM sin @sha256" >&2
  exit 1
fi

if [ "${1:-}" = "--caddy" ]; then
  docker build -q -t crm-proxy:validate "$here/caddy" > /dev/null
  docker run --rm -e CRM_DOMAIN=example.invalid -v "$here/Caddyfile:/etc/caddy/Caddyfile:ro" \
    crm-proxy:validate caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
fi
