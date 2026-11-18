"""La Graph API de Meta, lo mínimo que la conexión de una cuenta necesita (F7-02b, ADR-029 §2):
comprobar que un token abre un número de un WABA y leer su salud; y, para Instagram y Messenger
(F7b-04a, ADR-035 §3), que gestiona una página (y su cuenta de Instagram vinculada) y suscribirla.
Todo pasa por `core.http` (https, puerto 443, allowlist, IP pública). El token va en la cabecera
`Authorization`, nunca en la URL, y ni él ni el cuerpo de la respuesta llegan a un error ni a un
registro. Los identificadores se validan antes: solo dígitos, así que no cambian la ruta de la URL.
"""

import json
import re
from typing import Any

from django.conf import settings

from apps.conversations.models import LIMIT_TIERS, META_ID, QUALITY_RATINGS
from core import http

FIELDS = "id,display_phone_number,quality_rating,messaging_limit_tier"
BODY_MAX = 1 << 20  # una respuesta mayor no es de Meta
# Lo que la página manda al webhook (ADR-035 §4 [S17]); en Instagram la lista es SIN VERIFICAR 7.
SUBSCRIBED_FIELDS = (
    "messages,message_echoes,message_deliveries,message_reads,messaging_handovers,standby"
)
# Códigos de límite de tasa de Meta: llegan con 400, pero no son un token rechazado (los mismos que
# el adaptador de salida reintenta, `whatsapp.TOO_FAST`, y `32`, el límite de una página: F7b-06g).
THROTTLED = (4, 17, 32, 613, 80007, 130429, 131056)


class ChannelNotEnabled(Exception):
    """El canal de Meta no está habilitado en este despliegue (`META_ENABLED`)."""


class GraphRejected(Exception):
    """Meta no aceptó el token para ese WABA o ese número (o no existen)."""


class TokenRejected(GraphRejected):
    """Meta rechazó el token mismo (`190`), no el objeto pedido."""


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


def _ask(method: str, path: str, token: str, *, retry: bool = True) -> dict[str, Any]:
    """Una llamada a la Graph API con los mismos criterios que `phone_number`: `GraphRejected` si
    Meta rechaza el token o el objeto (`TokenRejected` si es el token), `GraphUnavailable` si no
    se pudo saber. `retry=False` deja los reintentos a quien llama (`core.http`)."""
    if not settings.META_ENABLED:
        raise ChannelNotEnabled
    url = f"https://{settings.META_GRAPH_HOST}/{settings.META_GRAPH_VERSION}/{path}"
    try:
        auth = {"Authorization": f"Bearer {token}"}
        reply = http.request(method, url, headers=auth, retry=retry)
        status, raw = reply.status, reply.data
    except http.BlockedDestination, http.HTTPError, OSError:
        raise GraphUnavailable from None
    if status == 429 or _code(raw) in THROTTLED:
        raise GraphUnavailable
    if status in (400, 401, 403, 404):
        raise TokenRejected if _code(raw) == 190 else GraphRejected
    if status != 200 or len(raw) > BODY_MAX:
        raise GraphUnavailable
    try:
        found = json.loads(raw)
    except ValueError, RecursionError:
        raise GraphUnavailable from None
    if not isinstance(found, dict):
        raise GraphUnavailable
    return found


def page(token: str, page_id: str, instagram_id: str | None = None) -> dict[str, str]:
    """La página `page_id` vista con `token`: su `id` y su nombre. Con `instagram_id`, la cuenta de
    Instagram vinculada a la página tiene que ser **esa** (`GraphRejected` si no hay o es otra)."""
    if not re.fullmatch(META_ID, page_id) or not re.fullmatch(META_ID, instagram_id or "00000"):
        raise ValueError("identificador de Meta no válido")
    found = _ask("GET", f"{page_id}?fields=id,name,instagram_business_account", token)
    linked = found.get("instagram_business_account")
    linked_id = linked.get("id") if isinstance(linked, dict) else None
    if found.get("id") != page_id or (instagram_id is not None and linked_id != instagram_id):
        raise GraphRejected
    name = found.get("name")
    return {
        "id": page_id,
        "name": name[:100] if isinstance(name, str) and name.isprintable() else "",
    }


def subscribe(token: str, page_id: str) -> None:
    """Suscribe la app a la página (`POST /<PAGE_ID>/subscribed_apps`) con `SUBSCRIBED_FIELDS`."""
    if not re.fullmatch(META_ID, page_id):
        raise ValueError("identificador de Meta no válido")
    found = _ask("POST", f"{page_id}/subscribed_apps?subscribed_fields={SUBSCRIBED_FIELDS}", token)
    if found.get("success") is not True:
        raise GraphUnavailable


def subscribed_apps(token: str, page_id: str) -> tuple[list[str], bool]:
    """Los ids de las apps suscritas a la página (`GET /<PAGE_ID>/subscribed_apps`, F7b-06e) y si
    Meta dice que hay más páginas de la lista (`paging.next`: solo se lee la primera). Meta solo la
    contesta a un token con un rol en la página: si no, `GraphRejected`. Una respuesta sin la lista
    de `data` no es de Meta (`GraphUnavailable`)."""
    if not re.fullmatch(META_ID, page_id):
        raise ValueError("identificador de Meta no válido")
    found = _ask("GET", f"{page_id}/subscribed_apps", token)
    rows, paging = found.get("data"), found.get("paging")
    if not isinstance(rows, list):
        raise GraphUnavailable
    more = isinstance(paging, dict) and bool(paging.get("next"))
    ids = [str(r["id"]) for r in rows if isinstance(r, dict) and isinstance(r.get("id"), str)]
    return ids, more


def subscription(token: str, page_id: str) -> bool | None:
    """¿Tiene la página suscrita la app del despliegue? (F7b-06g). `None` si no se sabe: sin un
    `META_APP_ID` válido, o si no está en la primera página de la lista y Meta dice que hay más."""
    ids, more = subscribed_apps(token, page_id)
    found = ours(ids)
    return None if found is False and more else found


def ours(apps: list[str]) -> bool | None:
    """¿Está la app del despliegue (`META_APP_ID`) entre las suscritas `apps`? `None` si no se
    sabe: sin un id de app válido nunca se supone ni lo uno ni lo otro (F7b-06g)."""
    app = settings.META_APP_ID
    if not isinstance(app, str) or not re.fullmatch(META_ID, app):
        return None
    return app in apps


def profile(token: str, customer: str) -> dict[str, Any]:
    """`name` y `username` del cliente de Instagram `customer` (su IGSID) con el token de la página
    (User Profile API, ADR-035 [S9]; D-F7b-06). Son datos que escribe el cliente: se devuelven tal
    cual llegan y quien los guarde los sanea. Sin reintentos de `core.http`: los acota la tarea."""
    if not re.fullmatch(META_ID, customer):
        raise ValueError("identificador de Meta no válido")
    found = _ask("GET", f"{customer}?fields=name,username", token, retry=False)
    return {"name": found.get("name"), "username": found.get("username")}
