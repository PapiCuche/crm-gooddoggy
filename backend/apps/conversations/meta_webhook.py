"""El webhook público de Meta (F7-03a; ADR-029 §3): `GET` y `POST /webhooks/meta/`.

Una vista de Django, no de DRF: fuera de `api/` y del espacio de tenant, sin sesión, sin CSRF
(`csrf_exempt`) y sin permisos de la API; la firma de Meta es lo único que autentica. La declara
`WEBHOOKS` en la auditoría del URLconf (`tests/test_access_api.py`). Con `META_ENABLED` apagado la
ruta no existe (404 como cualquier ruta desconocida: ni siquiera revela que está).

`GET` (suscripción): `hub.mode=subscribe` y `hub.verify_token` iguales, en tiempo constante, al
`META_WEBHOOK_VERIFY_TOKEN`; devuelve solo `hub.challenge` como texto plano (letras y dígitos,
hasta 128), nunca otra cosa. Todo lo demás, `403` vacío y sin distinguir la causa.

`POST` (eventos), en este orden: (1) leer el cuerpo **crudo**, como mucho `BODY_MAX` bytes (más es
`413`, sin leer más de ese tope más uno); (2) la firma `X-Hub-Signature-256`
(`sha256=<64 hex>`, HMAC-SHA256 del cuerpo crudo) contra **cada** secreto de `META_APP_SECRETS`,
sin cortar al primero y con el mismo trabajo se envíe lo que se envíe: ausente, mal formada o
inválida es `401` vacío; (3) solo ahora, parsear y validar el esquema. Con firma válida la respuesta
es siempre `200` vacío, también si el cuerpo no se puede parsear o no cumple el esquema (Meta
reintenta durante días lo que no recibe con `200` y puede desactivar el webhook): se cuenta y no se
procesa. Nada de esto escribe todavía (F7-03b guarda cada ítem); ningún registro lleva el cuerpo, la
firma ni el token.
El tope y los reintentos de Meta están **a confirmar** con su documentación.
"""

import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from typing import Any

from django.conf import settings
from django.http import Http404, HttpRequest, HttpResponse, HttpResponseNotAllowed
from django.views.decorators.csrf import csrf_exempt

from apps.conversations.models import META_ID
from core.observability.logging import get_logger

logger = get_logger(__name__)
BODY_MAX = 1 << 20  # 1 MiB: a confirmar con el tamaño máximo documentado de un lote de Meta
SIGNATURE = re.compile(r"sha256=([0-9a-fA-F]{64})")
CHALLENGE = re.compile(r"[A-Za-z0-9]{1,128}")
ID = re.compile(META_ID)
# El id de un mensaje de Meta, sin `:` (el separador de la clave de deduplicación).
WAMID = re.compile(r"[A-Za-z0-9._=-]{1,200}")
STATE = re.compile(r"[a-z_]{1,32}")  # `sent`, `delivered`, `read`, `failed`…
FIELD = re.compile(r"[a-z_]{1,64}")
OBJECT = "whatsapp_business_account"
MAX_ENTRIES, MAX_CHANGES, MAX_ROWS, MAX_ITEMS, MAX_DEPTH = 20, 20, 100, 500, 16
ID_MAX, TEXT_MAX = 200, 8192


class Invalid(Exception):  # noqa: N818 — un cuerpo firmado que no es lo que Meta envía
    """El cuerpo no cumple el esquema. Solo lleva un motivo corto y fijo, nunca el contenido."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class Item:
    """Un ítem lógico del evento: lo que F7-03b guardará en una fila. `key` deduplica."""

    kind: str  # `message`, `status` u `other`
    key: str
    waba_id: str
    phone_number_id: str | None
    payload: dict[str, Any]


def _text(value: Any, limit: int = ID_MAX) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= limit or not value.isprintable():
        raise Invalid("text")
    return value


def _wamid(value: Any) -> str:
    """El id de un mensaje, con el juego de caracteres de Meta: así la clave de deduplicación no
    tiene dos lecturas (`a:b` + `c` frente a `a` + `b:c`)."""
    if not isinstance(value, str) or not WAMID.fullmatch(value):
        raise Invalid("id")
    return value


def _rows(value: Any, name: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > MAX_ROWS:
        raise Invalid(name)
    if any(not isinstance(row, dict) for row in value):
        raise Invalid(name)
    return value


def _deep(value: Any) -> bool:
    """¿Supera `MAX_DEPTH` de anidamiento? Sin recursión: el cuerpo es de quien lo envía."""
    stack = [(value, 1)]
    while stack:
        found, depth = stack.pop()
        if isinstance(found, dict | list):
            if depth > MAX_DEPTH:
                return True
            stack.extend(
                (child, depth + 1)
                for child in (found.values() if isinstance(found, dict) else found)
            )
    return False


def _messages(waba: str, value: dict[str, Any]) -> list[Item]:
    metadata = value.get("metadata")
    number = metadata.get("phone_number_id") if isinstance(metadata, dict) else None
    if not isinstance(number, str) or not ID.fullmatch(number):
        raise Invalid("phone_number_id")
    items = []
    for message in _rows(value.get("messages", []), "messages"):
        kind = _text(message.get("type"), 32)
        if kind == "text":
            text = message.get("text")
            body = text.get("body") if isinstance(text, dict) else None
            # El texto del cliente es de quien lo escribe: saltos de línea, tabuladores, espacios
            # duros y uniones de emoji son normales. Aquí solo el tipo y el tamaño; limpiarlo es de
            # F7-04. Rechazarlo descartaría el lote entero (200 sin procesar): mensajes perdidos.
            if not isinstance(body, str) or not 1 <= len(body) <= TEXT_MAX:
                raise Invalid("text")
        items.append(Item("message", f"wa:msg:{_wamid(message.get('id'))}", waba, number, message))
    for status in _rows(value.get("statuses", []), "statuses"):
        state = status.get("status")
        if not isinstance(state, str) or not STATE.fullmatch(state):
            raise Invalid("status")
        key = f"wa:status:{_wamid(status.get('id'))}:{state}"  # ni el id ni el estado llevan `:`
        items.append(Item("status", key, waba, number, status))
    return items


def _no_constant(name: str) -> None:
    """`NaN` e `Infinity` no son JSON: Python los acepta, y la base (`jsonb`) no los guarda."""
    raise ValueError(name)


def parse(body: bytes) -> list[Item]:
    """Valida el cuerpo (ya firmado) y lo divide en ítems. `Invalid` si no es lo que Meta envía:
    ni JSON, ni el objeto, ni las listas y los tamaños esperados. No guarda nada."""
    try:
        event = json.loads(body, parse_constant=_no_constant)
    except ValueError, RecursionError:  # incluye UnicodeDecodeError; 100 000 corchetes
        raise Invalid("json") from None
    if not isinstance(event, dict) or event.get("object") != OBJECT or _deep(event):
        raise Invalid("object")
    entries = event.get("entry")
    if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_ENTRIES:
        raise Invalid("entry")
    items: list[Item] = []
    for entry in _rows(entries, "entry"):
        waba = entry.get("id")
        if not isinstance(waba, str) or not ID.fullmatch(waba):
            raise Invalid("waba")
        changes = _rows(entry.get("changes"), "changes")
        if not 1 <= len(changes) <= MAX_CHANGES:
            raise Invalid("changes")
        for change in changes:
            field, value = change.get("field"), change.get("value")
            if (
                not isinstance(field, str)
                or not FIELD.fullmatch(field)
                or not isinstance(value, dict)
            ):
                raise Invalid("change")
            if field == "messages":
                items += _messages(waba, value)
            else:  # cuenta, plantillas…: F7-10 y F7-11; llegan con el WABA y sin número
                digest = hashlib.sha256(json.dumps(change, sort_keys=True).encode()).hexdigest()
                items.append(Item("other", f"wa:{field}:{digest[:32]}", waba, None, change))
    if len(items) > MAX_ITEMS:
        raise Invalid("items")
    return items


def signed(header: str | None, body: bytes) -> bool:
    """¿La firma de Meta es la del cuerpo crudo con alguno de los secretos? El mismo trabajo se
    envíe lo que se envíe: una cabecera mal formada se compara contra ceros, y se recorren todos
    los secretos."""
    found = SIGNATURE.fullmatch(header or "")
    supplied = (found.group(1).lower() if found else "0" * 64).encode()
    valid = False
    for secret in settings.META_APP_SECRETS:
        expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest().encode()
        valid |= hmac.compare_digest(expected, supplied)
    return valid and found is not None


def _subscribe(request: HttpRequest) -> HttpResponse:
    token = request.GET.get("hub.verify_token", "").encode()
    matches = hmac.compare_digest(token, settings.META_WEBHOOK_VERIFY_TOKEN.encode())
    challenge = request.GET.get("hub.challenge", "")
    if matches and request.GET.get("hub.mode") == "subscribe" and CHALLENGE.fullmatch(challenge):
        return HttpResponse(challenge, content_type="text/plain")
    return HttpResponse(status=403)


def _receive(request: HttpRequest) -> HttpResponse:
    length = request.META.get("CONTENT_LENGTH", "")
    if length.isascii() and length.isdigit() and int(length) > BODY_MAX:
        return HttpResponse(status=413)  # sin leer nada
    body = request.read(BODY_MAX + 1)
    if len(body) > BODY_MAX:
        return HttpResponse(status=413)
    if not signed(request.headers.get("X-Hub-Signature-256"), body):
        return HttpResponse(status=401)
    try:
        items = parse(body)
    except Invalid as error:
        logger.warning("meta.webhook.invalid", reason=error.reason)
        return HttpResponse(status=200)  # firmado pero mal formado: 200, contado y sin procesar
    logger.info("meta.webhook.received", items=len(items))
    return HttpResponse(status=200)


@csrf_exempt
def webhook(request: HttpRequest) -> HttpResponse:
    if not settings.META_ENABLED:
        raise Http404  # igual que una ruta que no existe
    if request.method == "GET":
        return _subscribe(request)
    if request.method == "POST":
        return _receive(request)
    return HttpResponseNotAllowed(["GET", "POST"])
