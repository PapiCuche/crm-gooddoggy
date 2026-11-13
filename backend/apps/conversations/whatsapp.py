"""El adaptador de WhatsApp (F7-05b; ADR-029 §6, §8): `POST /<versión>/<phone_number_id>/messages`
por `core.http` con la versión de la Graph API fijada en `META_GRAPH_VERSION`.

Meta no deduplica, así que lo que no se sabe no se reenvía: un fallo antes de enviar o «demasiado
rápido» es `TemporaryChannelError`; un tiempo agotado, un corte o un 5xx, `AmbiguousChannelError`.
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
PHONE = r"^\+[1-9][0-9]{6,14}$"
EXTERNAL_ID = r"^[A-Za-z0-9._=-]{1,200}$"  # el `wamid`: se guarda en `messages.external_message_id`
REASONS = {190: "TOKEN_INVALID", 131047: "WINDOW_CLOSED", 131026: "RECIPIENT_UNREACHABLE"}
# Límites de tasa de Meta: no lo aceptó, se reintenta.
TOO_FAST = (4, 17, 613, 80007, 130429, 131056)


def _field(raw: bytes, *path: str | int) -> Any:
    """Un campo de la respuesta de Meta, o `None` si no es lo que se espera."""
    try:
        found: Any = json.loads(raw)
        for key in path:
            found = found[key]
    except ValueError, KeyError, IndexError, TypeError, RecursionError:
        return None
    return found


def _refused(status: int, raw: bytes, server: type[Exception]) -> SendResult:
    """Un rechazo de Meta: «demasiado rápido» se reintenta, un 5xx es `server` y el resto, un motivo
    cerrado."""
    code = _field(raw, "error", "code")
    code = code if isinstance(code, int) and not isinstance(code, bool) else None
    if code in TOO_FAST or status == 429:
        raise TemporaryChannelError
    if status >= 500:
        raise server
    reason = "TOKEN_INVALID" if status == 401 else REASONS.get(code or 0, "PROVIDER_REJECTED")
    return SendResult(False, reason=reason)


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
        reply = http.request("POST", f"{base}/media", headers=auth, body=body)
    except Exception:
        raise TemporaryChannelError from None
    found = _field(reply.data, "id")
    if reply.status != 200:
        return _refused(reply.status, reply.data, TemporaryChannelError)
    if not isinstance(found, str) or not re.fullmatch(META_ID, found):
        raise TemporaryChannelError
    part = {"id": found} | ({"caption": media.caption} if media.caption else {})
    return part | ({"filename": media.name} if media.kind == "DOCUMENT" else {})


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
        try:
            room = limiter.allow(claim.account_id)
        except Exception:  # Redis caído: falla cerrado hacia el reintento, sin registrar el error
            logger.warning("channel.limiter_unavailable")
            raise TemporaryChannelError from None
        if not room:
            raise TemporaryChannelError  # sin cupo: no sale, se reintenta
        try:
            token = credentials.open_token(claim.credential)  # solo durante esta llamada
        except sealed.SealError:
            if not sealed.has_key(claim.credential.key_version):
                raise TemporaryChannelError from None  # falta la clave en este despliegue: no es
            return SendResult(False, reason="TOKEN_INVALID")  # del token; se reintenta
        try:
            if claim.media is not None:  # fuera del `except` de abajo: la subida no es ambigua
                part = _upload(base, token, claim.media)
                if isinstance(part, SendResult):
                    return part
                kind = claim.media.kind.lower()
                body = {**body, "type": kind, kind: part}
                del body["text"]
            try:
                auth = {"Authorization": f"Bearer {token}"}
                reply = http.request("POST", f"{base}/messages", headers=auth, json=body)
            except Exception as error:  # se clasifica; el texto del error puede llevar la URL
                if http.never_connected(error):
                    raise TemporaryChannelError from None
                logger.warning("channel.whatsapp_send_ambiguous", message_id=str(claim.message_id))
                raise AmbiguousChannelError from None
        finally:
            del token
        status, raw = reply.status, reply.data
        if status == 200:
            wamid = _field(raw, "messages", 0, "id")
            if isinstance(wamid, str) and re.fullmatch(EXTERNAL_ID, wamid):
                return SendResult(True, wamid)
            raise AmbiguousChannelError  # Meta lo aceptó (200) y no dijo con qué id
        return _refused(status, raw, AmbiguousChannelError)
