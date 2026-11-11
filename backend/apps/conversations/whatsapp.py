"""El adaptador de WhatsApp (F7-05b; ADR-029 §6, §8): `POST /<versión>/<phone_number_id>/messages`
por `core.http` con la versión de la Graph API fijada en `META_GRAPH_VERSION`.

Meta no deduplica, así que lo que no se sabe no se reenvía: un fallo antes de enviar o un «demasiado
rápido» es `TemporaryChannelError`; un tiempo agotado, un corte o un 5xx, `AmbiguousChannelError`
(el mensaje sigue `SENDING`); un error de Meta se normaliza a un motivo cerrado y nunca se guarda ni
se registra su texto. El token se abre solo para esta llamada y va en la cabecera, no en la URL;
`biz_opaque_callback_data` lleva el id de nuestro mensaje, para reconciliar el estado del webhook.
"""

import json
import re
from typing import Any

from django.conf import settings

from apps.conversations import credentials
from apps.conversations.channels import (
    AmbiguousChannelError,
    Claim,
    SendResult,
    TemporaryChannelError,
)
from apps.conversations.models import META_ID
from core import http, sealed
from core.observability.logging import get_logger

logger = get_logger(__name__)
PHONE = r"^\+[1-9][0-9]{6,14}$"
EXTERNAL_ID = r"^[A-Za-z0-9._=-]{1,200}$"  # el `wamid`: se guarda en `messages.external_message_id`
# Código de error de Meta -> motivo cerrado propio (ADR-029 §8); a reconfirmar con su documentación.
REASONS = {190: "TOKEN_INVALID", 131047: "WINDOW_CLOSED", 131026: "RECIPIENT_UNREACHABLE"}
TOO_FAST = (130429, 131056)  # rendimiento o par: Meta no lo aceptó, vale esperar y reintentar


def _field(raw: bytes, *path: str | int) -> Any:
    """Un campo de la respuesta de Meta, o `None` si no es lo que se espera."""
    try:
        found: Any = json.loads(raw)
        for key in path:
            found = found[key]
    except ValueError, KeyError, IndexError, TypeError, RecursionError:
        return None
    return found


class WhatsAppAdapter:
    def send(self, claim: Claim) -> SendResult:
        if not settings.META_ENABLED or claim.credential is None:
            return SendResult(False, reason="CHANNEL_UNAVAILABLE")
        if not re.fullmatch(PHONE, claim.recipient):
            return SendResult(False, reason="RECIPIENT_UNREACHABLE")
        if not re.fullmatch(META_ID, claim.phone_number_id):
            raise ValueError("identificador de Meta no válido")  # nunca cambia la ruta de la URL
        url = (
            f"https://{settings.META_GRAPH_HOST}/{settings.META_GRAPH_VERSION}/"
            f"{claim.phone_number_id}/messages"
        )
        body: dict[str, Any] = {
            "messaging_product": "whatsapp", "to": claim.recipient[1:], "type": "text",
            "text": {"body": claim.body}, "biz_opaque_callback_data": str(claim.message_id),
        }  # fmt: skip
        try:
            token = credentials.open_token(claim.credential)  # solo durante esta llamada
        except sealed.SealError:
            return SendResult(False, reason="TOKEN_INVALID")
        try:
            reply = http.request(
                "POST", url, headers={"Authorization": f"Bearer {token}"}, json=body
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
            wamid = _field(raw, "messages", 0, "id")
            if isinstance(wamid, str) and re.fullmatch(EXTERNAL_ID, wamid):
                return SendResult(True, wamid)
            raise AmbiguousChannelError  # Meta lo aceptó (200) y no dijo con qué id
        code = _field(raw, "error", "code")
        code = code if isinstance(code, int) and not isinstance(code, bool) else None
        if code in TOO_FAST or status == 429:
            raise TemporaryChannelError
        if status >= 500:
            raise AmbiguousChannelError
        reason = "TOKEN_INVALID" if status == 401 else REASONS.get(code or 0, "PROVIDER_REJECTED")
        return SendResult(False, reason=reason)
