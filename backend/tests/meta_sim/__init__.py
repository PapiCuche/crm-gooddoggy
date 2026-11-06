"""Simulador de la API de Meta para las pruebas de la Fase 7 (F7-01, ADR-029 §11), sin red real ni
credenciales reales: `webhooks` construye y firma los cuerpos del webhook; `graph` es la Graph API
falsa (`FakeGraph`); `transport` la sirve por `core.http` (con su política real) o por un servidor
local. Sigue la versión de `GRAPH_VERSION`: cambiarla obliga a revisar los fixtures. Todos los
identificadores, teléfonos y secretos son ficticios y evidentes.
"""

GRAPH_VERSION = (
    "v23.0"  # la de las fixtures; la real la fija F7-02 en `META_GRAPH_VERSION` (D-F7-08)
)
