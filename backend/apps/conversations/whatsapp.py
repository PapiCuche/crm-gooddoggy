"""El adaptador de WhatsApp (F7-05b; ADR-029 §6, §8): `POST /<versión>/<phone_number_id>/messages`
por `core.http` con la versión de la Graph API fijada en `META_GRAPH_VERSION`.

Meta no deduplica, así que lo que no se sabe no se reenvía: un fallo antes de enviar, «demasiado
rápido» o un fallo pasajero que Meta rechazó (`AGAIN`, F7b-18) es `TemporaryChannelError`; un tiempo
agotado, un corte o un 5xx, `AmbiguousChannelError`.
Un error de Meta se normaliza a un motivo cerrado, nunca su texto. El token se abre solo para esta
llamada y va en la cabecera; `biz_opaque_callback_data` lleva el id de nuestro mensaje.

Un medio (F7-09b) se sube antes (`POST /<phone_number_id>/media`) y el envío lleva su id. Subir
no llega al cliente, así que un fallo dudoso de la subida es `TemporaryChannelError` (se repite);
un blob que no es el que se guardó (otro `sha256`) no sale.
"""

import hashlib
import json
import re
from typing import Any

from django.conf import settings

from apps.conversations import credentials, limiter
from apps.conversations.channels import (
    AmbiguousChannelError,
    Claim,
    Media,
    SendResult,
    TemporaryChannelError,
)
from apps.conversations.models import META_ID
from apps.conversations.outbound_media import CAPS
from core import http, sealed, storage
from core.observability.logging import get_logger

logger = get_logger(__name__)
# Plazo total de la subida de un medio a Meta (F7-09b2): con el envío (`core.http.request`, 3
# intentos de 5 + 15 s) cabe de sobra en `INBOX_SENDING_DEADLINE_MINUTES`; una prueba lo comprueba.
UPLOAD_SECONDS = 120.0
PHONE = r"^\+[1-9][0-9]{6,14}$"
EXTERNAL_ID = r"^[A-Za-z0-9._=-]{1,200}$"  # el `wamid`: se guarda en `messages.external_message_id`
# Código de error de Meta -> código propio (ADR-029 §8), el mismo en la respuesta del envío y en el
# `failed` del webhook (F7b-15); a reconfirmar con su documentación.
CODES = {
    190: "TOKEN_INVALID", 131047: "WINDOW_CLOSED", 131026: "RECIPIENT_UNREACHABLE",
    130429: "RATE_LIMITED", 131056: "RATE_LIMITED", 131048: "QUALITY_BLOCKED",
    4: "RATE_LIMITED", 17: "RATE_LIMITED", 613: "RATE_LIMITED", 80007: "RATE_LIMITED",
    1: "PROVIDER_ERROR", 2: "PROVIDER_ERROR", 131000: "PROVIDER_ERROR", 131016: "PROVIDER_ERROR",
}  # fmt: skip
TEMPLATES = range(132000, 133000)  # la familia de errores de plantilla de Meta
# Límites de tasa de Meta (los `RATE_LIMITED` de la tabla): en la respuesta del envío no lo aceptó y
# se reintenta antes de la tabla, salvo con un 5xx (F7b-19: Meta no documenta que un 5xx con su
# código no lo aceptó, así que es ambiguo como cualquier otro 5xx).
TOO_FAST = tuple(code for code, own in CODES.items() if own == "RATE_LIMITED")
# F7b-18: fallos pasajeros de Meta («Temporary due to downtime», «Message failed to send… Try
# again», «A service is temporarily unavailable»). Con una respuesta de error que no es 5xx, Meta no
# lo aceptó: se reintenta. El `1` («Invalid request or possible server error») sigue definitivo; un
# 5xx sigue ambiguo y en el webhook (ya lo había aceptado) siguen siendo `PROVIDER_ERROR`.
AGAIN = (2, 131000, 131016)


def field_at(raw: bytes, *path: str | int) -> Any:
    """Un campo de la respuesta de Meta, o `None` si no es lo que se espera."""
    try:
        found: Any = json.loads(raw)
        for key in path:
            found = found[key]
    except ValueError, KeyError, IndexError, TypeError, RecursionError:
        return None
    return found


def _number(value: Any) -> int | None:
    fine = isinstance(value, int) and not isinstance(value, bool) and 0 <= value < 10**10
    return value if fine else None


def error_detail(code: Any, subcode: Any = None) -> str | None:
    """El error de Meta en números (F7b-13): `code` o `code/subcode`, o `None` sin un `code`
    legible. Nunca su texto, su título ni sus detalles (pueden llevar datos del cliente)."""
    code, subcode = _number(code), _number(subcode)
    if code is None:
        return None
    return str(code) if subcode is None else f"{code}/{subcode}"


def code_for(error_code: Any) -> str:
    """El código propio de un error de Meta: lo desconocido es `PROVIDER_REJECTED`."""
    if not isinstance(error_code, int) or isinstance(error_code, bool):
        return "PROVIDER_REJECTED"
    return (
        "TEMPLATE_UNAVAILABLE"
        if error_code in TEMPLATES
        else CODES.get(error_code, "PROVIDER_REJECTED")
    )


def _refused(status: int, raw: bytes, server: type[Exception]) -> SendResult:
    """Un rechazo de Meta: un 5xx es `server` con cualquier código (F7b-19: antes que el límite),
    «demasiado rápido» se reintenta, un fallo pasajero (`AGAIN`) se reintenta y el resto, el motivo
    cerrado de `code_for` (el del webhook)."""
    code = field_at(raw, "error", "code")
    code = code if isinstance(code, int) and not isinstance(code, bool) else None
    detail = error_detail(code, field_at(raw, "error", "error_subcode"))
    if status >= 500:  # pudo haberlo aceptado: un `130429` o un `4` no lo aclaran
        if server is TemporaryChannelError:  # la subida (nunca llega al cliente), con su código
            raise TemporaryChannelError(detail)
        raise server
    if code in TOO_FAST or status == 429:
        raise TemporaryChannelError(detail)  # F7b-17: el código, por si se agotan los reintentos
    if code in AGAIN:
        raise TemporaryChannelError(detail)
    reason = "TOKEN_INVALID" if status == 401 else code_for(code)
    return SendResult(False, reason=reason, detail=detail)


def _upload(base: str, token: str, media: Media) -> dict[str, Any] | SendResult:
    """Sube el blob a Meta y devuelve la parte del envío (`{"id", "caption", "filename"}`), o el
    rechazo. El almacén o la red que fallan: se repite (nada llegó al cliente)."""
    try:
        with storage.storage().open(media.key) as blob:
            data = blob.read(CAPS[media.kind] + 1)
    except Exception:
        raise TemporaryChannelError from None
    if hashlib.sha256(data).hexdigest() != media.sha256:
        return SendResult(False, reason="ADAPTER_REJECTED")
    fields = {"messaging_product": "whatsapp", "type": media.mime,
              "file": (media.name, data, media.mime)}  # fmt: skip
    body, kind = http.form(fields)
    auth = {"Authorization": f"Bearer {token}", "Content-Type": kind}
    try:
        with http.deadline(UPLOAD_SECONDS):  # un servidor que gotea no la alarga sin fin
            reply = http.request("POST", f"{base}/media", headers=auth, body=body)
    except Exception:
        raise TemporaryChannelError from None
    found = field_at(reply.data, "id")
    if reply.status != 200:
        return _refused(reply.status, reply.data, TemporaryChannelError)
    if not isinstance(found, str) or not re.fullmatch(META_ID, found):
        raise TemporaryChannelError
    part = {"id": found} | ({"caption": media.caption} if media.caption else {})
    return part | ({"filename": media.name} if media.kind == "DOCUMENT" else {})


def admit(claim: Claim) -> str | SendResult:
    """El cupo de la cuenta y su token, abierto solo para esta llamada (lo comparten los adaptadores
    de Meta): sin cupo o sin Redis se reintenta; una credencial que no se abre es `TOKEN_INVALID`,
    salvo que falte la clave en este despliegue (no es del token: se reintenta)."""
    assert claim.credential is not None  # noqa: S101 — quien llama ya lo comprobó
    try:
        room = limiter.allow(claim.account_id)
    except Exception:  # Redis caído: falla cerrado hacia el reintento, sin registrar el error
        logger.warning("channel.limiter_unavailable")
        raise TemporaryChannelError from None
    if not room:
        raise TemporaryChannelError  # sin cupo: no sale, se reintenta
    try:
        return credentials.open_token(claim.credential)
    except sealed.SealError:
        if not sealed.has_key(claim.credential.key_version):
            raise TemporaryChannelError from None  # falta la clave en este despliegue
        return SendResult(False, reason="TOKEN_INVALID")


class WhatsAppAdapter:
    def send(self, claim: Claim) -> SendResult:
        if not settings.META_ENABLED or claim.credential is None:
            return SendResult(False, reason="CHANNEL_UNAVAILABLE")
        if not re.fullmatch(PHONE, claim.recipient):
            return SendResult(False, reason="RECIPIENT_UNREACHABLE")
        if not re.fullmatch(META_ID, claim.phone_number_id):
            raise ValueError("identificador de Meta no válido")  # nunca cambia la ruta de la URL
        base = (
            f"https://{settings.META_GRAPH_HOST}/{settings.META_GRAPH_VERSION}/"
            f"{claim.phone_number_id}"
        )
        body: dict[str, Any] = {
            "messaging_product": "whatsapp", "to": claim.recipient[1:], "type": "text",
            "text": {"body": claim.body}, "biz_opaque_callback_data": str(claim.message_id),
        }  # fmt: skip
        token = admit(claim)  # solo durante esta llamada
        if isinstance(token, SendResult):
            return token
        try:
            if claim.media is not None:  # fuera del `except` de abajo: la subida no es ambigua
                part = _upload(base, token, claim.media)
                if isinstance(part, SendResult):
                    return part
                kind = claim.media.kind.lower()
                body = {**body, "type": kind, kind: part}
                del body["text"]
            try:
                reply = http.request(
                    "POST",
                    f"{base}/messages",
                    headers={"Authorization": f"Bearer {token}"},
                    json=body,
                )
            except Exception as error:  # se clasifica; el texto del error puede llevar la URL
                if http.never_connected(error):
                    raise TemporaryChannelError from None
                logger.warning("channel.whatsapp_send_ambiguous", message_id=str(claim.message_id))
                raise AmbiguousChannelError from None
        finally:
            del token
        status, raw = reply.status, reply.data
        if status == 200:
            wamid = field_at(raw, "messages", 0, "id")
            if isinstance(wamid, str) and re.fullmatch(EXTERNAL_ID, wamid):
                return SendResult(True, wamid)
            raise AmbiguousChannelError  # Meta lo aceptó (200) y no dijo con qué id
        return _refused(status, raw, AmbiguousChannelError)
