"""Los errores de los comandos de cotizaciones con la forma del contrato (ADR-014 §1), en una sola
tabla para todas sus rutas (F5-09e). Ningún cuerpo lleva cifras, ids ni el estado de otra persona.
`DoesNotExist` (inexistente, de otra organización o fuera del alcance) es el mismo 404;
`NotAllowed`, 403 sin motivo; una carrera o un estado, 409 con su código; `PriceRefused`, siempre
el mismo 422. El contacto y la oportunidad dan **un único** 400 que no dice cuál falló ni por qué.
El `ValueError` de un comando es un 400 general, nunca su texto (los 400 de campo del serializador
no pasan por aquí). `DataError` es un 500 sin el mensaje (puede llevar el valor): en el servidor,
la clase y el SQLSTATE, sin traza."""

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from django.core.exceptions import ObjectDoesNotExist
from django.db import DataError
from rest_framework import exceptions, serializers

from apps.quotes import answers, commands, decisions, discounts, lifecycle
from core.api.errors import INTERNAL_ERROR, ApiError

logger = logging.getLogger(__name__)
GENERIC = "Dato no admitido."
LINKS = "El contacto o la oportunidad no existe o no lo puedes ver."
BAD_REFERENCE = {  # sin repetir el id recibido
    "price_list_id": "No es una lista de precios que puedas usar.",
    "variant_id": "Una variante no existe o no tiene precio vigente en la lista.",
}
CONFLICTS: tuple[tuple[type[Exception], str, str], ...] = (
    (commands.StaleVersion, "STALE_VERSION",
     "Otra persona cambió la cotización: vuelve a cargarla y repite el cambio."),
    (commands.QuoteNotDraft, "QUOTE_NOT_DRAFT", "Solo se cambia un borrador."),
    (commands.QuoteTerminal, "QUOTE_TERMINAL", "La cotización ya terminó."),
    (commands.QuoteRefused, "QUOTE_REFUSED", "La cotización no admite ese cambio."),
    (decisions.NotPending, "NOT_PENDING", "La cotización no espera una decisión."),
    (decisions.SelfApproval, "SELF_APPROVAL_FORBIDDEN", "La solicitud la decide otra persona."),
    (decisions.BeyondLimit, "BEYOND_LIMIT", "El descuento supera tu límite."),
    (lifecycle.NotIssuable, "NOT_ISSUABLE", "Solo se emite una cotización aprobada o un borrador."),
    (lifecycle.NeedsApproval, "NEEDS_APPROVAL", "Antes necesita una aprobación."),
    (lifecycle.NotRevisable, "NOT_REVISABLE",
     "Solo se revisa una cotización aprobada o emitida que sigue abierta."),
    (answers.NotAnswerable, "NOT_ANSWERABLE", "Solo se responde una cotización emitida."),
    (answers.QuoteExpired, "QUOTE_EXPIRED", "La validez de la cotización ya pasó."),
)  # fmt: skip
PRICE_REFUSED = ("PRICE_NOT_ALLOWED", 422, "Ese precio no se admite.")


@contextmanager
def domain() -> Iterator[None]:
    """Envuelve la llamada al comando (no el serializador ni la respuesta)."""
    try:
        yield
    except ObjectDoesNotExist:
        raise exceptions.NotFound from None
    except commands.NotAllowed:
        raise exceptions.PermissionDenied from None
    except discounts.PriceRefused:
        raise ApiError(*PRICE_REFUSED) from None
    except commands.BadReference as bad:
        if bad.field in BAD_REFERENCE:
            raise serializers.ValidationError({bad.field: [BAD_REFERENCE[bad.field]]}) from None
        raise serializers.ValidationError(LINKS) from None  # contacto, oportunidad o autoría
    except ValueError:  # un comando rechazó algo que el serializador dejó pasar
        raise serializers.ValidationError(GENERIC) from None
    except DataError as error:
        state = getattr(error.__cause__, "sqlstate", None)
        logger.error("quotes: DataError %s", state)  # noqa: TRY400 — sin traza: lleva el valor
        raise ApiError(INTERNAL_ERROR, 500) from None
    except Exception as error:
        for kind, code, message in CONFLICTS:
            if isinstance(error, kind):
                raise ApiError(code, 409, message) from None
        raise
