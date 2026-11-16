# Caddy del borde de producción con el módulo caddy-ratelimit (ADR-033 §4.3, decisión 10): límite
# de tasa por IP en /webhooks/*. Código de terceros: versión fijada y revisada al actualizar. CI la
# publica y el servidor la despliega por digest (F1-15); infra/prod/validate.sh la construye.
# Las dos imágenes base van por digest (revisión de #845): validate.sh rechaza un FROM sin él.
FROM caddy:2.11.4-builder-alpine@sha256:0aa610043dab5da82ad0a0268e46bb852785e6f5160f12f1c6fe3f42903d7e1b AS build
RUN xcaddy build v2.11.4 --with github.com/mholt/caddy-ratelimit@v0.1.0

FROM caddy:2.11.4-alpine@sha256:6aeddd44c3078b0f9a35206472a11420648a79c184603ef95957d0a20044cb2b
COPY --from=build /usr/bin/caddy /usr/bin/caddy
