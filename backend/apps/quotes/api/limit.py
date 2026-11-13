"""Tope del oráculo del costo (F5-09e2; #655 §5, ADR-006): con `quotes.override_price` y sin ver
costos, una búsqueda binaria sobre el sí o no del suelo daría el costo en unas 20 respuestas. Cada
`POST` a una ruta de `FLOOR` cuenta **antes de buscar nada**, por persona y cotización y por persona
en todas; pasado el tope, 429 `RATE_LIMITED` con `Retry-After` (al minuto; con el tope por persona,
el mismo para toda cotización y ruta), exista o no la cotización, sea o no de su alcance u
organización. Ventana fija en `login_throttles`: alrededor de un cambio de ventana caben dos topes.
Va antes de `TenantResolutionMiddleware`, que deshace toda respuesta de error: el contador queda,
y la negativa del suelo que marcó la vista (`refusal`) se audita luego en su propia transacción
(`quote.price_refused`, `DENIED`, solo la ruta y los ids); si esa auditoría falla, responde igual.
Aprobar y emitir (F5-09e3, F5-09e4) añadirán su ruta a `FLOOR`: la prueba estructural lo exige."""

import logging
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any
from uuid import UUID

from django.conf import settings
from django.db import connection
from django.http import HttpRequest, HttpResponseBase

from apps.audit.services import Entity, Result, record
from apps.quotes.discounts import PriceRefused
from core.api.errors import error_response
from core.tenancy.scope import tenant_scope

logger = logging.getLogger(__name__)
ID = "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
FLOOR = (rf"items/{ID}/price", "request-approval")  # las rutas que miden el suelo de costo
PATH = re.compile(rf"^/api/v1/o/[^/]+/quotes/(?P<quote>{ID})/(?:{'|'.join(FLOOR)})/$", re.I)
_COUNT = """
INSERT INTO login_throttles AS t (key, failures, blocked_until, updated_at)
VALUES (%(key)s, 1, NULL, now())
ON CONFLICT (key) DO UPDATE SET
  failures = CASE WHEN t.updated_at < now() - %(window)s::int * interval '1 second'
                  THEN 1 ELSE t.failures + 1 END,
  updated_at = CASE WHEN t.updated_at < now() - %(window)s::int * interval '1 second'
                    THEN now() ELSE t.updated_at END
RETURNING failures,
  CEIL(EXTRACT(EPOCH FROM updated_at + %(window)s::int * interval '1 second'
    - clock_timestamp()) / 60)::int * 60
"""


def wait_for(user_id: object, quote: str) -> int:
    """Cuenta en las dos claves, en este orden; la espera si pasó un tope (antes, la de persona)."""
    rules = settings.QUOTE_FLOOR_THROTTLE
    keys = (("user", f"qfl:usr:{user_id}"), ("quote", f"qfl:usr:{user_id}:q:{quote.lower()}"))
    waits = []
    with connection.cursor() as cursor:
        for name, key in keys:
            limit, window = rules[name]
            cursor.execute(_COUNT, {"key": key, "window": window})
            asked, wait = cursor.fetchone()
            waits.append(max(int(wait), 60) if asked > limit else 0)
    return waits[0] or waits[1]


@contextmanager
def refusal(request: Any, route: str, quote_id: UUID, line_id: UUID | None) -> Iterator[None]:
    """Marca la petición si el comando negó el precio (ya con la fila bloqueada y en su alcance)."""
    try:
        yield
    except PriceRefused:
        request._request.quote_refusal = (quote_id, line_id, route)
        raise


class QuoteFloorLimitMiddleware:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponseBase]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponseBase:
        user, found = getattr(request, "user", None), PATH.match(request.path_info)
        if (
            found
            and request.method == "POST"
            and user is not None
            and user.is_authenticated
            and (wait := wait_for(user.pk, found["quote"]))
        ):
            limited = error_response("RATE_LIMITED", 429)
            limited.headers["Retry-After"] = str(wait)
            return limited
        response = self.get_response(request)
        mark, tenant = getattr(request, "quote_refusal", None), getattr(request, "tenant", None)
        if mark is not None and tenant is not None and response.status_code == 422:
            quote_id, line_id, route = mark
            try:
                shown = {"route": route, "line_id": line_id and str(line_id)}  # ni cifras ni motivo
                with tenant_scope(tenant) as ctx:
                    record(ctx, "quote.price_refused", Entity("quote", quote_id), metadata=shown,
                           result=Result.DENIED)  # fmt: skip
            except Exception as error:  # la negativa se responde igual; el tope ya contó
                logger.error("quotes: refusal not audited (%s)", type(error).__name__)  # noqa: TRY400
        return response
