"""Los estados de entrega de WhatsApp (F7-06a; ADR-029 §8): del ítem `status` enrutado por el
procesador de ingesta (`ingress.route`, con el scope de la organización de la cuenta **registrada**,
nunca la del payload) al mensaje saliente, con `advance_delivery`.

El mensaje se busca solo entre los salientes de esa cuenta: por `external_message_id` (el
`wamid`) o, si el envío aún no confirmó el id, por `biz_opaque_callback_data` (el id de nuestro
mensaje) cuando sigue `SENDING`, que así se reconcilia con el primer estado que llega. Un mensaje
de otra organización u otra cuenta no existe; uno que aún no se conoce falla de forma transitoria
(`NotYet`) y la ingesta reintenta con espera. Un estado atrasado o repetido no cambia nada y
`failed` es terminal (lo decide `advance_delivery` y, por debajo, el disparador). Del error de
Meta solo se lee el `code`: se reduce a un código propio cerrado (`CODES`), se guarda también el
número (`provider_error_detail`, F7b-13) y nunca se guarda ni se registra su título ni su texto.

Instagram y Messenger (F7b-05c; ADR-035 §5–§6) en `social`: un eco es nuestro si su `app_id` es
`META_APP_ID` y su `metadata` es la ficha (`echo_token`) de un saliente de **esa** cuenta y **ese**
cliente; en Instagram, si el eco no trae `app_id`, basta la ficha (SIN VERIFICAR 12). El `mid` del
eco se escribe solo si el mensaje no tenía o ya era ese (si no, `ECHO_MID_CONFLICT` y nada cambia);
el eco solo avanza a `SENT`. Un eco de otra app es `FOREIGN_ECHO`; uno nuestro aún sin mensaje
espera como un estado (`NotYet`). Messenger marca entregado y leído con una marca de agua sobre los
salientes de la identidad reclamados hasta ese instante; Instagram, leído por `mid`. Todo por
`advance_delivery`: nada retrocede.

La clave de la ingesta es por cuenta (F7b-04d; ADR-040, que enmienda ADR-035 §4): si una
cuenta conectada escribe a otra, su eco y el entrante de la otra traen el mismo `mid` y se procesan
los dos.
"""

import datetime as dt
import re
from typing import Any
from uuid import UUID

from django.conf import settings
from django.db import transaction
from django.db.models import Q, QuerySet
from django.utils import timezone

from apps.conversations import accounts, commands, inbound
from apps.conversations.models import ECHO_HEX, META_ID, ChannelAccount, Credential, Message
from apps.conversations.whatsapp import EXTERNAL_ID, error_detail
from core.observability.logging import get_logger
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
    """El mensaje de este estado aún no se conoce: se reintenta, no se pierde. `closing` es el
    motivo con el que se cierra si no aparece tras `UNKNOWN_AFTER` intentos."""

    def __init__(self, closing: str = "UNKNOWN_MESSAGE") -> None:
        super().__init__(closing)
        self.closing = closing


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


def _before_rotation(alias: str, account: ChannelAccount, message: Message) -> bool:
    """¿Se reclamó el mensaje antes de que existiera la credencial vigente de la cuenta? Entonces un
    `190` tardío es del token que ya se cambió, no del de ahora."""
    claimed = message.sending_at
    made = (
        Credential.objects.using(alias)
        .filter(pk=account.credential_id)
        .values_list("created_at", flat=True)
        .first()
    )
    return claimed is not None and made is not None and claimed < made


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
    detail = error_detail(first.get("code")) if state == "FAILED" else None  # solo el número
    now = timezone.now()
    stamp = inbound.instant(payload.get("timestamp"))
    at = min(stamp, now) if stamp else now  # el instante del proveedor, nunca del futuro
    with transaction.atomic(using=alias):
        message = _find(alias, account, wamid, _uuid(payload.get("biz_opaque_callback_data")))
        if message is None:
            raise NotYet
        if reason in ("TOKEN_INVALID", "QUALITY_BLOCKED"):
            # F7b-14: la cuenta que se marcará, antes que la conversación (`_reply_failed`): el
            # orden del reclamo (cuenta → conversación), sin ciclos
            rows = ChannelAccount.objects.using(alias).select_for_update(no_key=True)
            rows.filter(pk=account.pk).exists()
        before = message.delivery_status
        if message.external_message_id is None:  # reconciliación: el envío no llegó a confirmarlo
            Message.objects.using(alias).filter(pk=message.pk).update(external_message_id=wamid)
        moved = commands.advance_delivery(
            ctx, message_id=message.pk, status=state, at=at, reason=reason, detail=detail
        )
        if moved.delivery_status == before:
            return None  # atrasado, repetido o tras `FAILED`: nada cambia
        if reason == "TOKEN_INVALID":
            if _before_rotation(alias, account, message):
                return None  # un fallo del token anterior: el nuevo (ya verificado) no se toca
            accounts.mark_token_invalid(ctx, channel_account_id=account.pk)
        elif reason == "QUALITY_BLOCKED":
            accounts.mark_degraded(ctx, channel_account_id=account.pk)
    return None


def _at(raw: Any) -> dt.datetime:
    """El instante del evento (en milisegundos o segundos), nunca del futuro; si no, ahora."""
    now, stamp = timezone.now(), inbound.instant_ms(raw)
    return min(stamp, now) if stamp else now


def _outbound(alias: str, account: ChannelAccount, customer: Any) -> QuerySet[Message]:
    """Los salientes de esta cuenta a este cliente (su identidad en la cuenta)."""
    return Message.objects.using(alias).filter(
        channel_account_id=account.pk, direction="OUTBOUND",
        conversation__contact_identity__external_id=customer,
    )  # fmt: skip


def _echo(ctx: TenantContext, account: ChannelAccount, event: dict[str, Any]) -> str | None:
    alias, message = require_scope(ctx), event["message"]
    app, token, mid = message.get("app_id"), message.get("metadata"), message["mid"]
    ours = bool(re.fullmatch(META_ID, settings.META_APP_ID)) and str(app) == settings.META_APP_ID
    if not ours and (app is not None or account.platform != "INSTAGRAM"):
        return "FOREIGN_ECHO"  # otra app o la bandeja nativa (D-F7b-07)
    if not (isinstance(token, str) and re.fullmatch(ECHO_HEX, token)):
        return "UNKNOWN_MESSAGE" if ours else "FOREIGN_ECHO"  # sin ficha nunca correlaciona
    unsure = Q(delivery_status__in=("SENDING", "SENT", "DELIVERED", "READ")) | Q(
        delivery_status="FAILED", provider_error_code="PROVIDER_UNKNOWN"
    )
    with transaction.atomic(using=alias):
        found = _outbound(alias, account, event["recipient"]["id"]).filter(unsure, echo_token=token)
        mine = found.select_for_update(no_key=True, of=("self",)).first()
        if mine is None:
            raise NotYet("UNKNOWN_MESSAGE" if ours else "FOREIGN_ECHO")
        if mine.external_message_id not in (None, mid):  # nunca sobrescribe un `mid` real
            logger.warning("meta.webhook.echo_mid_conflict", message_id=str(mine.pk))
            return "ECHO_MID_CONFLICT"
        if mine.external_message_id is None:
            Message.objects.using(alias).filter(pk=mine.pk).update(external_message_id=mid)
        commands.advance_delivery(
            ctx, message_id=mine.pk, status="SENT", at=_at(event.get("timestamp"))
        )
    return None


def social(ctx: TenantContext, account: ChannelAccount, event: dict[str, Any]) -> str | None:
    """Un eco, un leído o una entrega de Instagram o Messenger ya enrutado. Devuelve el motivo si
    se ignora o `None`; `NotYet` si su mensaje aún no se conoce o sigue `SENDING`."""
    alias = require_scope(ctx)
    if isinstance(event.get("message"), dict):
        return _echo(ctx, account, event)
    mine = _outbound(alias, account, event["sender"]["id"])
    if account.platform == "INSTAGRAM":  # leído por `mid`
        found = mine.filter(external_message_id=event["read"]["mid"]).first()
        if found is None:
            raise NotYet
        commands.advance_delivery(
            ctx, message_id=found.pk, status="READ", at=_at(event.get("timestamp"))
        )
        return None
    delivered = isinstance(event.get("delivery"), dict)  # como lo leyó el analizador
    until = inbound.instant_ms((event.get("delivery") or event["read"])["watermark"])
    if until is None:
        return "BAD_STATUS"
    state, at = ("DELIVERED" if delivered else "READ"), min(until, timezone.now())
    # Todo lo enviado hasta la marca: lo reclamado antes de ella (`sending_at`, antes del POST y
    # de que Meta le ponga su hora). Lo que sigue `SENDING` aún no tiene `mid`: se espera.
    # Aproximado aun con relojes perfectos: si B se reclama mientras el POST de A está en vuelo,
    # `sending_at` de B ≤ la hora que Meta da a A (la marca), y el entregado o leído de A marca
    # también B. Solo es cosmético (el mismo cliente y la misma cuenta) y nunca retrocede. El
    # arreglo: comparar con la hora de Meta del mensaje (la del eco) o usar `delivery.mids`.
    covered = mine.filter(sending_at__lte=until)
    later = ("SENT",) if delivered else ("SENT", "DELIVERED")
    for pk in covered.filter(delivery_status__in=later).values_list("pk", flat=True):
        commands.advance_delivery(ctx, message_id=pk, status=state, at=at)
    if covered.filter(delivery_status="SENDING").exists():
        raise NotYet
    return None
