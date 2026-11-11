"""La Graph API de Meta, lo mínimo que la conexión de una cuenta necesita (F7-02b, ADR-029 §2):
comprobar que un token abre un número de un WABA y leer su salud. Todo pasa por `core.http` (https,
puerto 443, allowlist, IP pública). El token va en la cabecera `Authorization`, nunca en la URL, y
ni él ni el cuerpo de la respuesta llegan a un error ni a un registro. Los identificadores se
validan antes: solo dígitos, así que no cambian la ruta de la URL.
"""

import json
import re
from typing import Any

from django.conf import settings

from apps.conversations.models import LIMIT_TIERS, META_ID, QUALITY_RATINGS
from core import http

FIELDS = "id,display_phone_number,quality_rating,messaging_limit_tier"
BODY_MAX = 1 << 20  # una respuesta mayor no es de Meta
# Códigos de límite de tasa de Meta: llegan con 400, pero no son un token rechazado (los mismos que
# el adaptador de salida reintenta, `whatsapp.TOO_FAST`).
THROTTLED = (4, 17, 613, 80007, 130429, 131056)


class ChannelNotEnabled(Exception):
    """El canal de Meta no está habilitado en este despliegue (`META_ENABLED`)."""


class GraphRejected(Exception):
    """Meta no aceptó el token para ese WABA o ese número (o no existen)."""


class GraphUnavailable(Exception):
    """No se pudo preguntar a Meta ahora (red, tiempo agotado o error suyo): vale reintentar."""


def _code(raw: bytes) -> int | None:
    """El `error.code` de una respuesta de Meta, o `None` si no es lo que se espera."""
    try:
        code = json.loads(raw)["error"]["code"]
    except ValueError, KeyError, TypeError, RecursionError:
        return None
    return code if isinstance(code, int) and not isinstance(code, bool) else None


def phone_number(token: str, waba_id: str, phone_number_id: str) -> dict[str, Any]:
    """El número `phone_number_id` del WABA `waba_id` según Meta, visto con `token`: `id`, el
    número visible, la calidad y el nivel de mensajería (normalizados a los conjuntos cerrados de
    la base). `GraphRejected` si el token no sirve o el número no es de ese WABA."""
    if not settings.META_ENABLED:
        raise ChannelNotEnabled
    if not (re.fullmatch(META_ID, waba_id) and re.fullmatch(META_ID, phone_number_id)):
        raise ValueError("identificador de Meta no válido")
    url = (
        f"https://{settings.META_GRAPH_HOST}/{settings.META_GRAPH_VERSION}/{waba_id}/"
        f"phone_numbers?fields={FIELDS}&limit=100"
    )
    try:
        reply = http.request("GET", url, headers={"Authorization": f"Bearer {token}"})
        status, raw = reply.status, reply.data
    except http.BlockedDestination, http.HTTPError, OSError:
        raise GraphUnavailable from None  # sin el mensaje: puede llevar la URL
    if status == 429 or _code(raw) in THROTTLED:  # un límite de tasa no dice nada del token
        raise GraphUnavailable
    if status in (400, 401, 403, 404):
        raise GraphRejected
    if status != 200 or len(raw) > BODY_MAX:
        raise GraphUnavailable
    try:
        rows = json.loads(raw)["data"]
        if not isinstance(rows, list):
            raise TypeError
    except ValueError, KeyError, TypeError, RecursionError:
        raise GraphUnavailable from None  # una respuesta que no es de Meta no dice nada del token
    # Solo la primera página (`limit=100`): un número que no está en ella se rechaza (cerrado).
    same = (r for r in rows if isinstance(r, dict) and r.get("id") == phone_number_id)
    found = next(same, None)
    if found is None:
        raise GraphRejected
    shown = found.get("display_phone_number")
    quality, tier = found.get("quality_rating"), found.get("messaging_limit_tier")
    return {
        "id": phone_number_id,
        "display": shown if isinstance(shown, str) and shown.isprintable() else "",
        "quality_rating": quality if quality in QUALITY_RATINGS else "UNKNOWN",
        "messaging_limit_tier": tier if tier in LIMIT_TIERS else "TIER_NOT_SET",
    }
