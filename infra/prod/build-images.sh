#!/usr/bin/env bash
# Construye y prueba las imágenes del backend, del frontend y del proxy (F1-15b, ADR-033 §7). Lo mismo
# en cada PR (images-pr.yml, sin credenciales ni publicación) y antes de publicar (images.yml):
#   1. `--load` con el builder fijado (crm-images) y las etiquetas de origen y revisión;
#   2. gitleaks sobre su configuración (ENV, ARG, etiquetas) y su historial de capas;
#   3. las arranca: usuario no root, /health/live y el frontend; el proxy, con rate_limit y el Caddyfile.
# Deja en <salida>/<nombre>.config el digest de la configuración de cada imagen probada (sus capas,
# ENV, etiquetas e historial): images.yml solo publica si la imagen subida tiene esa misma.
# Uso (en CI, con GITHUB_SERVER_URL, GITHUB_REPOSITORY y GITHUB_SHA): build-images.sh <salida> <gitleaks>
set -euo pipefail
out=$1 gitleaks=$2
mkdir -p "$out/meta"
for name in backend frontend proxy; do
  context=$name
  [ "$name" = proxy ] && context=infra/prod/caddy
  docker buildx build --builder crm-images --platform linux/amd64 --pull --load \
    --label "org.opencontainers.image.source=$GITHUB_SERVER_URL/$GITHUB_REPOSITORY" \
    --label "org.opencontainers.image.revision=$GITHUB_SHA" -t "crm-$name:ci" "$context"
  { docker image inspect --format '{{json .Config}}' "crm-$name:ci"
    docker history --no-trunc --format '{{.CreatedBy}}' "crm-$name:ci"; } > "$out/meta/$name.txt"
  # La configuración que Docker guarda (la misma con su almacén clásico o el de containerd).
  docker save "crm-$name:ci" | tar -xO manifest.json |
    python3 -c 'import json, re, sys; print("sha256:" + re.search("[0-9a-f]{64}", json.load(sys.stdin)[0]["Config"])[0])' \
      > "$out/$name.config"
done
"$gitleaks" dir --config .gitleaks.toml --redact --no-banner --exit-code 1 "$out/meta"

test "$(docker run --rm --entrypoint id crm-backend:ci -u)" = 10001
DJANGO_SECRET_KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(64))')" \
SECRETS_KEKS="1:$(python3 -c 'import base64, secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())')" \
  sh infra/docker/smoke-image.sh crm-backend:ci 8000 /health/live \
  -e DJANGO_SECRET_KEY -e SECRETS_KEKS -e DJANGO_ALLOWED_HOSTS=app.example.com \
  -e FORWARDED_ALLOW_IPS=127.0.0.1 -e DATABASE_URL=postgres://ci:ci@127.0.0.1:5432/ci
test "$(docker run --rm --entrypoint id crm-frontend:ci -u)" != 0
sh infra/docker/smoke-image.sh crm-frontend:ci 3000 /o/ci
docker run --rm crm-proxy:ci caddy list-modules | grep -qx http.handlers.rate_limit
docker run --rm -e CRM_DOMAIN=example.invalid -v "$PWD/infra/prod/Caddyfile:/etc/caddy/Caddyfile:ro" \
  crm-proxy:ci caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
