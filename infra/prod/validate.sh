#!/bin/sh
# Valida infra/prod sin servidor ni secretos (F1-13): `docker compose config` con valores ficticios
# sacados de las secciones de .env.production.example, y comprueba sobre el resultado que solo el
# proxy publica puertos (80 y 443), que no hay build, Garage ni Mailpit, que cada imagen va por
# digest y cada contenedor tiene límite de memoria y rotación de logs, y que cada archivo de
# entorno llega solo a su destinatario. Con --caddy además construye la imagen de infra/prod/caddy
# y pasa `caddy validate` al Caddyfile. F1-15b: cada Dockerfile del repositorio fija sus bases (FROM)
# y su frontend (`# syntax=`) por digest; `--dockerfiles [raíz]` hace solo eso (test_workflow_security.py).
# Uso: sh infra/prod/validate.sh [--caddy | --dockerfiles [raíz]]
set -eu
here=$(cd "$(dirname "$0")" && pwd)
root=$(cd "$here/../.." && pwd)

pinned() { # <raíz>: falla con cada FROM o `# syntax=` sin @sha256:<64 hex>, o si no hay ningún Dockerfile
  found=$(find "$1" \( -name node_modules -o -name .git \) -prune -o -type f -name 'Dockerfile*' -print)
  [ -n "$found" ] || { echo "$1: ningún Dockerfile" >&2; return 1; }
  bad=$(echo "$found" | while IFS= read -r f; do
    grep -inE '^[[:space:]]*(FROM[[:space:]]|#[[:space:]]*syntax[[:space:]]*=)' "$f" |
      grep -vE '@sha256:[0-9a-f]{64}([[:space:]]|$)' | sed "s|^|$f:|"
  done)
  [ -z "$bad" ] || { printf '%s\n' "$bad" "FROM o # syntax= sin @sha256:<digest>" >&2; return 1; }
}
if [ "${1:-}" = --dockerfiles ]; then
  pinned "${2:-$root}"
  exit
fi
pinned "$root"
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
    # deploy.sh migra antes con `run`; `up` lo repite sin nada que aplicar (F1-15b). Sin esto, un `up`
    # a mano arrancaría el runtime sin migrar.
    dep = services[name]["depends_on"].get("migrate", {}).get("condition")
    assert dep == "service_completed_successfully", f"{name}: no espera a migrate"
assert set(services["migrate"]["environment"]) == {"DATABASE_MIGRATOR_URL", "DJANGO_SETTINGS_MODULE"}
for name in ("proxy", "frontend"):
    assert "data" not in services[name]["networks"], f"{name}: en la red de datos"
print("infra/prod: compose válido; solo el proxy publica 80 y 443")
PY

# Variante de servidor compartido (ADR-046): compose.yaml + compose.shared.yaml. Ningún servicio
# publica puertos, el proxy entra en la red externa `ingress` con su IP fija y monta Caddyfile.shared,
# y el resto del stack es el mismo. Las rutas y los topes de los dos Caddyfile tienen que coincidir.
docker compose -f "$here/compose.yaml" -f "$here/compose.shared.yaml" --env-file "$work/release.env" \
  config --format json > "$work/shared.json"
python3 - "$work/config.json" "$work/shared.json" "$here/Caddyfile" "$here/Caddyfile.shared" <<'PY'
import json, re, sys

base = json.load(open(sys.argv[1]))
shared = json.load(open(sys.argv[2]))
assert set(shared["services"]) == set(base["services"]), "la variante compartida cambia los servicios"
for name, svc in shared["services"].items():
    assert not svc.get("ports"), f"compartido: {name} publica puertos"
    if name == "proxy":
        continue
    same = {k: v for k, v in svc.items() if k != "networks"}
    assert same == {k: v for k, v in base["services"][name].items() if k != "networks"}, f"compartido: {name} cambia"
    assert set(svc.get("networks", {})) == set(base["services"][name].get("networks", {})), f"compartido: {name} cambia de redes"
proxy = shared["services"]["proxy"]
assert set(proxy["networks"]) == {"edge", "ingress"}, f"compartido: el proxy en {set(proxy['networks'])}"
assert proxy["networks"]["ingress"]["ipv4_address"], "compartido: el proxy sin IP fija en ingress"
assert shared["networks"]["ingress"].get("external"), "compartido: ingress tiene que ser externa"
mounts = {v["target"]: v.get("source", "") for v in proxy["volumes"]}
assert mounts["/etc/caddy/Caddyfile"].endswith("/Caddyfile.shared"), "compartido: el proxy no monta Caddyfile.shared"
assert proxy["environment"].get("CRM_EDGE_PROXY_IP"), "compartido: falta CRM_EDGE_PROXY_IP"
for net in ("data", "egress"):
    assert shared["networks"][net]["ipam"]["config"][0]["subnet"], f"compartido: {net} sin subred fija"


def table(path):  # rutas, topes y tasa, en orden
    keep = re.compile(r"^\s*(handle\b.*\{|max_size\b.*|respond\b.*|events\b.*|window\b.*)")
    rows = []
    for line in open(path):
        m = keep.match(line)
        if m:
            rows.append(re.sub(r"\s+", " ", m.group(1).split("#")[0]).strip())
    return rows


assert table(sys.argv[3]) == table(sys.argv[4]), "Caddyfile y Caddyfile.shared no tienen las mismas rutas y topes"
print("infra/prod: variante compartida válida; ningún servicio publica puertos")
PY

if [ "${1:-}" = "--caddy" ]; then
  docker build -q -t crm-proxy:validate "$here/caddy" > /dev/null
  docker run --rm -e CRM_DOMAIN=example.invalid -v "$here/Caddyfile:/etc/caddy/Caddyfile:ro" \
    crm-proxy:validate caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
  docker run --rm -e CRM_DOMAIN=example.invalid -e CRM_EDGE_PROXY_IP=172.31.250.2 \
    -v "$here/Caddyfile.shared:/etc/caddy/Caddyfile:ro" \
    crm-proxy:validate caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
fi
