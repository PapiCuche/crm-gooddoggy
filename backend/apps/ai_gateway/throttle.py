"""Tope de escrituras de cada persona en las rutas de cuentas del proveedor de IA (F8-03d; ADR-034
§4), como el de las cuentas de WhatsApp (F7-02e): la prueba de conexión llama al proveedor cada vez,
así que nadie la repite en bucle. Cuentan las escrituras (hoy POST y DELETE: guardar, rotar, borrar
y probar), que comparten el contador; GET y HEAD no cuentan (decisión del propietario del
2026-11-17, F8-03e): leer no llama al proveedor y sigue pidiendo `ai.manage` y el segundo factor.
OPTIONS tampoco cuenta: las vistas lo deniegan. Falla cerrado: todo método que no sea de lectura
cuenta, así que un PUT o un PATCH futuro (cambiar el modelo, ADR-034 §4) cuenta sin tocar esto.

Es un middleware **fuera** del scope de tenant: una ruta de tenant que acaba en error deshace su
transacción (`TenantResolutionMiddleware`), y con ella el contador. Va tras la autenticación y el
CSRF (un anónimo o una petición forzada no gastan el tope de nadie) y antes de resolver el tenant.
Cuenta por persona (`aik:usr:<id>`), no por organización: un miembro sin `ai.manage` se limita
solo y no agota el tope de quien gestiona. Cuenta antes de mirar el permiso, la cuenta o el cuerpo:
una escritura que acaba en 403 (sin permiso o sin segundo factor) también cuenta.

Ventana fija en la tabla de contadores de acceso (`login_throttles`, de plataforma, compartida entre
instancias), en una sola sentencia que bloquea la fila. Pasado el tope, 429 `RATE_LIMITED` con
`Retry-After` y sin detalle. La purga del acceso borra estas filas cuando su ventana ya pasó (la
ventana es menor que su calma).
"""

import re
from collections.abc import Callable

from django.conf import settings
from django.db import connection
from django.http import HttpRequest, HttpResponse

from core.api.errors import error_response

PATH = re.compile(r"^/api/v1/o/[^/]+/ai/provider-accounts/")
READS = ("GET", "HEAD", "OPTIONS")  # las que no cuentan; las demás, sí (F8-03e)
_COUNT = """
INSERT INTO login_throttles AS t (key, failures, blocked_until, updated_at)
VALUES (%(key)s, 1, NULL, now())
ON CONFLICT (key) DO UPDATE SET
  failures = CASE WHEN t.updated_at < now() - %(window)s::int * interval '1 second'
                  THEN 1 ELSE t.failures + 1 END,
  updated_at = CASE WHEN t.updated_at < now() - %(window)s::int * interval '1 second'
                    THEN now() ELSE t.updated_at END
RETURNING failures,
  GREATEST(CEIL(EXTRACT(EPOCH FROM updated_at
    + %(window)s::int * interval '1 second' - clock_timestamp())), 1)::int
"""


def wait_for(user_id: object) -> int:
    """Cuenta la escritura de esa persona y devuelve los segundos que tiene que esperar si pasó el
    tope (0 si no)."""
    limit, window = settings.AI_ACCOUNT_THROTTLE
    with connection.cursor() as cursor:
        cursor.execute(_COUNT, {"key": f"aik:usr:{user_id}", "window": window})
        asked, wait = cursor.fetchone()
    return int(wait) if asked > limit else 0


class AiAccountThrottleMiddleware:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        user = getattr(request, "user", None)
        if (
            request.method not in READS
            and PATH.match(request.path_info)
            and user is not None
            and user.is_authenticated
            and (wait := wait_for(user.pk))
        ):
            limited = error_response("RATE_LIMITED", 429)
            limited.headers["Retry-After"] = str(wait)
            return limited
        return self.get_response(request)
