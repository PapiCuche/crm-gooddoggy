"""Los estados de entrega de WhatsApp (F7-06a; ADR-029 §8): del ítem `status` enrutado por el
procesador de ingesta (`ingress.route`, con el scope de la organización de la cuenta **registrada**,
nunca la del payload) al mensaje saliente, con `advance_delivery`.

El mensaje se busca solo entre los salientes de esa cuenta: por `external_message_id` (el
`wamid`) o, si el envío aún no confirmó el id, por `biz_opaque_callback_data` (el id de nuestro
mensaje) cuando sigue `SENDING`, que así se reconcilia con el primer estado que llega. Un mensaje
de otra organización u otra cuenta no existe; uno que aún no se conoce falla de forma transitoria
(`NotYet`) y la ingesta reintenta con espera. Un estado atrasado o repetido no cambia nada y
`failed` es terminal (lo decide `advance_delivery` y, por debajo, el disparador). Del error de
Meta solo se lee el `code`: se reduce a un código propio cerrado (`CODES`) y nunca se guarda ni se
registra su título ni su texto.
"""

import re
from typing import Any
from uuid import UUID

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.conversations import accounts, commands, inbound
from apps.conversations.models import ChannelAccount, Message
from apps.conversations.whatsapp import EXTERNAL_ID
from core.observability.logging import get_logger
from core.outbox import emit
from core.tenancy.context import TenantContext
from core.tenancy.scope import require_scope

logger = get_logger(__name__)
STATES = {"sent": "SENT", "delivered": "DELIVERED", "read": "READ", "failed": "FAILED"}
# Código de error de Meta -> código propio (ADR-029 §8); a reconfirmar con su documentación. Los
# límites de tasa son los mismos que el adaptador reintenta (`whatsapp.TOO_FAST`).
CODES = {
    190: "TOKEN_INVALID", 131047: "WINDOW_CLOSED", 131026: "RECIPIENT_UNREACHABLE",
    130429: "RATE_LIMITED", 131056: "RATE_LIMITED", 131048: "QUALITY_BLOCKED",
    4: "RATE_LIMITED", 17: "RATE_LIMITED", 613: "RATE_LIMITED", 80007: "RATE_LIMITED",
    1: "PROVIDER_ERROR", 2: "PROVIDER_ERROR", 131000: "PROVIDER_ERROR", 131016: "PROVIDER_ERROR",
}  # fmt: skip
TEMPLATES = range(132000, 133000)  # la familia de errores de plantilla de Meta


class NotYet(Exception):  # noqa: N818 — el estado llegó antes que la confirmación del envío
    """El mensaje de este estado aún no se conoce: se reintenta, no se pierde."""


def code_for(error_code: Any) -> str:
    """El código propio de un `failed` de Meta: lo desconocido es `PROVIDER_REJECTED`."""
    if not isinstance(error_code, int) or isinstance(error_code, bool):
        return "PROVIDER_REJECTED"
    return (
        "TEMPLATE_UNAVAILABLE"
        if error_code in TEMPLATES
        else CODES.get(error_code, "PROVIDER_REJECTED")
    )


def _uuid(raw: Any) -> UUID | None:
    try:
        return UUID(raw) if isinstance(raw, str) else None
    except ValueError:
        return None


def _find(alias: str, account: ChannelAccount, wamid: str, opaque: UUID | None) -> Message | None:
    """El saliente de esta cuenta, bloqueado: por su `wamid`, o por el id opaco si sigue `SENDING`
    (o lo cerró el plazo con `PROVIDER_UNKNOWN`) y aún no tiene `wamid`."""
    mine = Message.objects.using(alias).select_for_update(no_key=True)
    mine = mine.filter(channel_account_id=account.pk, direction="OUTBOUND")
    if (found := mine.filter(external_message_id=wamid).first()) is not None or opaque is None:
        return found
    unsure = Q(delivery_status="SENDING") | Q(
        delivery_status="FAILED",
        provider_error_code="PROVIDER_UNKNOWN",  # cerrado por el plazo
    )
    return mine.filter(unsure, pk=opaque, external_message_id__isnull=True).first()


def apply(ctx: TenantContext, account: ChannelAccount, payload: dict[str, Any]) -> str | None:
    """Aplica un estado a su mensaje. Devuelve el motivo si se ignora (`BAD_STATUS`: sin id válido
    o con un estado que no es de entrega) o `None`; `NotYet` si el mensaje aún no se conoce."""
    alias = require_scope(ctx)
    wamid, state = payload.get("id"), STATES.get(str(payload.get("status")))
    if not (isinstance(wamid, str) and re.fullmatch(EXTERNAL_ID, wamid)) or state is None:
        return "BAD_STATUS"
    errors = payload.get("errors")
    first = errors[0] if isinstance(errors, list) and errors and isinstance(errors[0], dict) else {}
    reason = code_for(first.get("code")) if state == "FAILED" else None
    now = timezone.now()
    stamp = inbound.instant(payload.get("timestamp"))
    at = min(stamp, now) if stamp else now  # el instante del proveedor, nunca del futuro
    with transaction.atomic(using=alias):
        message = _find(alias, account, wamid, _uuid(payload.get("biz_opaque_callback_data")))
        if message is None:
            raise NotYet
        before = message.delivery_status
        if message.external_message_id is None:  # reconciliación: el envío no llegó a confirmarlo
            Message.objects.using(alias).filter(pk=message.pk).update(external_message_id=wamid)
        moved = commands.advance_delivery(
            ctx, message_id=message.pk, status=state, at=at, reason=reason
        )
        if moved.delivery_status == before:
            return None  # atrasado, repetido o tras `FAILED`: nada cambia
        emit(  # una pista para la pantalla: solo ids
            ctx, "message.updated", aggregate_type="message", aggregate_id=message.pk,
            payload={"conversation_id": str(message.conversation_id)},
        )  # fmt: skip
        if reason == "TOKEN_INVALID":
            accounts.mark_token_invalid(ctx, channel_account_id=account.pk)
        elif reason == "QUALITY_BLOCKED":
            accounts.mark_degraded(ctx, channel_account_id=account.pk)
    return None
