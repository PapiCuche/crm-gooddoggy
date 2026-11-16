# Caddy del borde de producción con el módulo caddy-ratelimit (ADR-033 §4.3, decisión 10): límite
# de tasa por IP en /webhooks/*. Código de terceros: versión fijada y revisada al actualizar. CI la
# publica y el servidor la despliega por digest (F1-15); infra/prod/validate.sh la construye.
FROM caddy:2.11.4-builder-alpine AS build
RUN xcaddy build v2.11.4 --with github.com/mholt/caddy-ratelimit@v0.1.0

FROM caddy:2.11.4-alpine
COPY --from=build /usr/bin/caddy /usr/bin/caddy
