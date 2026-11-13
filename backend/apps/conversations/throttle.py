"""Tope de peticiones de cada persona a las rutas de cuentas de WhatsApp (F7-02e): conectar y
rotar preguntan a Meta cada vez, así que nadie puede hacerlo sin límite.

Es un middleware **fuera** del scope de tenant: una ruta de tenant que acaba en error deshace su
transacción (`TenantResolutionMiddleware`), y con ella el contador, justo el caso que se quiere
limitar (Meta rechaza el token). Va tras la autenticación y el CSRF y antes de resolver el
tenant, y cuenta por persona (`chn:usr:<id>`): contar por organización dejaría que cualquier
miembro, aunque no gestione canales, agotara el tope de la organización.

Ventana fija en la tabla de contadores de acceso (`login_throttles`, de plataforma, compartida
entre instancias), en una sola sentencia que bloquea la fila. Cuenta antes de mirar nada más;
rechaza (429 con `Retry-After`) al pasar el tope. Las tres rutas comparten el contador. La purga
del acceso borra estas filas cuando su ventana ya pasó (la ventana es menor que su calma).

La subida de un medio (F7-09b2) tiene su contador por persona (`med:usr:<id>`).
"""

import re
from collections.abc import Callable

from django.conf import settings
from django.db import connection
from django.http import HttpRequest, HttpResponse

from core.api.errors import error_response

PATH = re.compile(r"^/api/v1/o/[^/]+/channels/whatsapp/")
MEDIA = re.compile(r"^/api/v1/o/[^/]+/conversations/[^/]+/media/$")
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


def wait_for(user_id: object, prefix: str = "chn") -> int:
    """Cuenta la petición de esa persona y devuelve los segundos que tiene que esperar si pasó el
    tope (0 si no)."""
    rule = settings.CHANNEL_CHECK_THROTTLE if prefix == "chn" else settings.MEDIA_UPLOAD_THROTTLE
    limit, window = rule
    with connection.cursor() as cursor:
        cursor.execute(_COUNT, {"key": f"{prefix}:usr:{user_id}", "window": window})
        asked, wait = cursor.fetchone()
    return int(wait) if asked > limit else 0


class ChannelThrottleMiddleware:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        user, path, method = getattr(request, "user", None), request.path_info, request.method
        prefix = "chn" if PATH.match(path) and method in ("POST", "DELETE") else None
        prefix = "med" if MEDIA.match(path) and method == "POST" else prefix
        if (
            prefix is not None
            and user is not None
            and user.is_authenticated
            and (wait := wait_for(user.pk, prefix))
        ):
            limited = error_response("RATE_LIMITED", 429)
            limited.headers["Retry-After"] = str(wait)
            return limited
        return self.get_response(request)
