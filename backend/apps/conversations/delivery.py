"""Los estados de entrega de WhatsApp (F7-06a; ADR-029 §8): del ítem `status` enrutado por el
procesador de ingesta (`ingress.route`, con el scope de la organización de la cuenta **registrada**,
nunca la del payload) al mensaje saliente, con `advance_delivery`.

El mensaje se busca solo entre los salientes de esa cuenta: por `external_message_id` (el
`wamid`) o, si el envío aún no confirmó el id, por `biz_opaque_callback_data` (el id de nuestro
mensaje) cuando sigue `SENDING`, que así se reconcilia con el primer estado que llega. Un mensaje
de otra organización u otra cuenta no existe; uno que aún no se conoce falla de forma transitoria
(`NotYet`) y la ingesta reintenta con espera. Un estado atrasado o repetido no cambia nada y
`failed` es terminal (lo decide `advance_delivery` y, por debajo, el disparador). Del error de
Meta solo se lee el `code`: se reduce a un código propio cerrado (`code_for`, la tabla que comparte
con la respuesta del envío, F7b-15), se guarda también el número (`provider_error_detail`, F7b-13) y
nunca se guarda ni se registra su título ni su texto. Un `failed` sobre un `FAILED` que el plazo
cerró con `PROVIDER_UNKNOWN` lo corrige una vez (F7b-15). Un `190` o un `131048` cambia también la
cuenta, solo si la credencial con la que salió el mensaje sigue siendo la suya (F7b-20,
`_sent_with`): nunca una cuenta rotada después del envío ni una reconectada (otra fila).

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
from apps.conversations.whatsapp import EXTERNAL_ID, code_for, error_detail
from core.observability.logging import get_logger
from core.tenancy.context import TenantContext
from core.tenancy.scope import require_scope

logger = get_logger(__name__)
STATES = {"sent": "SENT", "delivered": "DELIVERED", "read": "READ", "failed": "FAILED"}
ON_ACCOUNT = ("TOKEN_INVALID", "QUALITY_BLOCKED")  # los fallos que también cambian la cuenta


class NotYet(Exception):  # noqa: N818 — el estado llegó antes que la confirmación del envío
    """El mensaje de este estado aún no se conoce: se reintenta, no se pierde. `closing` es el
    motivo con el que se cierra si no aparece tras `UNKNOWN_AFTER` intentos."""

    def __init__(self, closing: str = "UNKNOWN_MESSAGE") -> None:
        super().__init__(closing)
        self.closing = closing


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


def _sent_with(alias: str, account_id: UUID, message: Message) -> UUID | None:
    """La credencial con la que salió el mensaje (F7b-20): la de su cuenta viva cuando se reclamó
    (`sending_at`). El reclamo fija esa hora con la cuenta en `FOR SHARE` (`claim_outbound`) y
    `rotate_token` y `disconnect` fijan `revoked_at` y `created_at` con ella en `FOR NO KEY UPDATE`:
    se excluyen, así que con un mismo reloj el orden de las horas es el de los bloqueos. `None` si
    no se sabe: sin `sending_at`, o no hay exactamente una credencial que lo cubra."""
    at = message.sending_at
    if at is None:
        return None
    alive = Q(revoked_at__isnull=True) | Q(revoked_at__gt=at)
    found = Credential.objects.using(alias).filter(alive, owner_id=account_id, created_at__lte=at)
    only = list(found.values_list("pk", flat=True)[:2])
    return only[0] if len(only) == 1 else None


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
        if reason in ON_ACCOUNT:
            # F7b-14: la cuenta que se marcará, antes que la conversación (`_reply_failed`): el
            # orden del reclamo (cuenta → conversación), sin ciclos
            rows = ChannelAccount.objects.using(alias).select_for_update(no_key=True)
            rows.filter(pk=account.pk).exists()
        before = (message.delivery_status, message.provider_error_code)
        if message.external_message_id is None:  # reconciliación: el envío no llegó a confirmarlo
            Message.objects.using(alias).filter(pk=message.pk).update(external_message_id=wamid)
        moved = commands.advance_delivery(
            ctx, message_id=message.pk, status=state, at=at, reason=reason, detail=detail
        )
        if (moved.delivery_status, moved.provider_error_code) == before:
            return None  # atrasado, repetido o tras un `FAILED` definitivo: nada cambia
        # F7b-20: el efecto en la cuenta, solo si la credencial del envío sigue siendo la suya
        # (`_demote` lo compara con la cuenta bloqueada, como en `tasks.deliver`): un fallo tardío
        # tras una rotación es del token anterior y la rotación ya leyó la calidad. Sin saber con
        # cuál salió, ninguno: el próximo envío (con la credencial del reclamo) o la comprobación
        # de salud lo dirán sin arriesgar bloquear una cuenta sana.
        if reason not in ON_ACCOUNT or (used := _sent_with(alias, account.pk, message)) is None:
            return None
        mark = accounts.mark_token_invalid if reason == "TOKEN_INVALID" else accounts.mark_degraded
        mark(ctx, channel_account_id=account.pk, credential_id=used)
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
