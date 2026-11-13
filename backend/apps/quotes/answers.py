"""La respuesta a una cotización emitida (F5-09c3; 04 §L.2). Solo la importan su API y su tarea.

Aceptar o rechazar lo registra una persona (no hay portal) con `quotes.send`, en este orden:
permiso, cotización bloqueada en su alcance (una sola `DoesNotExist`), versión, `SENT`/`VIEWED`
y validez (`valid_until <= ahora`, o la base con su reloj: `QuoteExpired`). Aceptar mueve la
oportunidad a `pago-pendiente` (bloqueos cotización → pipeline → oportunidad, como `deals`) con
`opportunities.view` y `.update` sobre la fila bloqueada; una regla del pipeline se audita como
`skipped:<código>` y otro error lo deshace todo. Vencer es solo del sistema, una vez."""

from typing import Any
from uuid import UUID

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.access.selectors import can
from apps.audit.services import record
from apps.deals.models import Opportunity, PipelineStage
from apps.deals.opportunities import BadReference as DealReference
from apps.deals.opportunities import OpportunityClosed, _locked, _pipeline
from apps.deals.opportunity_stages import ClosingStage, MissingFields, move_opportunity
from apps.quotes.commands import (
    REASON_MAX,
    NotAllowed,
    StaleVersion,
    _entity,
    _event,
    _integer,
    _reachable,
    _reference,
    _require,
    _stamp,
    _text,
    _uuid,
    _within,
)
from apps.quotes.models import Quote, QuoteStatus
from core.tenancy.context import ActorType, TenantContext
from core.tenancy.scope import require_scope

STAGE = "pago-pendiente"  # 04 §L.2: «Pago pendiente» (los códigos de etapa van con guion)
OPEN = (QuoteStatus.SENT, QuoteStatus.VIEWED)
_SKIPPED: dict[type[Exception], str] = {  # solo las reglas del pipeline; otro error deshace todo
    OpportunityClosed: "closed", ClosingStage: "closing_stage", MissingFields: "missing_fields",
    DealReference: "bad_reference", Opportunity.DoesNotExist: "missing",
}  # fmt: skip
_SKIPPABLE = tuple(_SKIPPED)


class NotAnswerable(Exception):
    """Solo se responde una cotización emitida (`SENT` o `VIEWED`)."""


class QuoteExpired(Exception):
    """La validez de la cotización ya pasó."""


def _name(error: IntegrityError) -> str | None:
    return getattr(getattr(error.__cause__, "diag", None), "constraint_name", None)


def _move(ctx: TenantContext, ectx: Any, alias: str, opportunity_id: UUID) -> str:
    """Lleva la oportunidad de la cotización a `pago-pendiente`; dice qué pasó (solo códigos)."""
    try:
        with transaction.atomic(using=alias):  # un punto de guardado: lo saltado no deja nada
            live = Opportunity.objects.using(alias).filter(deleted_at__isnull=True)
            pipeline = live.values_list("pipeline_id", flat=True).get(pk=opportunity_id)
            _pipeline(alias, ctx.organization_id, pipeline)  # el orden de `deals`
            opp = _locked(alias, opportunity_id)
            if not all(can(ectx, f"opportunities.{c}", opp) for c in ("view", "update")):
                return "skipped:forbidden"
            stages = PipelineStage.objects.using(alias).values_list("pk", flat=True)
            stage = stages.filter(pipeline_id=opp.pipeline_id, code=STAGE, is_active=True).first()
            if stage is None:
                return "skipped:no_stage"
            if stage == opp.stage_id:
                return "skipped:same_stage"
            move_opportunity(ctx, opportunity_id=opp.pk, version=opp.version, stage_id=stage)
            return "moved"
    except _SKIPPABLE as error:
        return f"skipped:{next(c for k, c in _SKIPPED.items() if isinstance(error, k))}"


def _answer(
    ctx: TenantContext, quote_id: Any, version: Any, status: str, why: str | None = None
) -> Quote:
    alias = require_scope(ctx)
    ectx = _require(ctx, "quotes.send")  # lo primero: sin permiso, ni se mira lo que se envía
    if status == QuoteStatus.REJECTED and not (why := _text(why, "motivo", REASON_MAX)):
        raise ValueError("motivo: el rechazo lleva un motivo")
    quote_id = _uuid(quote_id, "cotización")
    _integer(version, 1, 2**31 - 1, "versión")
    try:
        with transaction.atomic(using=alias):
            quote = _reachable(alias, quote_id, _within(ectx, "quotes.view", "quotes.send"))
            if quote.version != version:
                raise StaleVersion(quote_id)
            if quote.status not in OPEN:
                raise NotAnswerable(quote_id)
            now, before = timezone.now(), quote.status
            if quote.valid_until is None or quote.valid_until <= now:
                raise QuoteExpired(quote_id)
            stamp, action = ("accepted_at", "quote.accepted") if why is None else (
                "rejected_at", "quote.declined")  # fmt: skip
            said = {} if why is None else {"rejection_reason": why}
            _stamp(ctx, alias, quote, {"status": status, stamp: now, **said})
            shown = {"status": [before, status], stamp: [None, now],
                     **{k: [None, "[texto]"] for k in said}}  # fmt: skip
            about: dict[str, Any] = {}
            if why is None and (opp := quote.opportunity_id) is not None:
                about = {"opportunity_id": str(opp),
                         "opportunity_move": _move(ctx, ectx, alias, opp)}  # fmt: skip
            record(ctx, action, _entity(quote), shown, about)
            _event(ctx, quote, action)
    except IntegrityError as error:
        if (name := _name(error)) == "quotes_answer_expired":  # la base ya la ve vencida
            raise QuoteExpired(quote_id) from None
        if not (name or "").startswith("quotes_"):  # no es de la cotización (p. ej., de `deals`)
            raise
        raise _reference(error) from None
    return quote


def accept_quote(ctx: TenantContext, *, quote_id: UUID, version: int) -> Quote:
    """Acepta por el cliente y mueve la oportunidad; `quote.accepted` (de él saldrá la reserva de
    stock, D-INV-2). `NotAllowed`, `DoesNotExist`, `StaleVersion`, `NotAnswerable`,
    `QuoteExpired`."""
    return _answer(ctx, quote_id, version, QuoteStatus.ACCEPTED)


def reject_quote_by_customer(
    ctx: TenantContext, *, quote_id: UUID, version: int, reason: str
) -> Quote:
    """Rechaza por el cliente con su motivo: `quote.declined`. Errores de aceptar y `ValueError`."""
    return _answer(ctx, quote_id, version, QuoteStatus.REJECTED, reason)


def expire_quote(ctx: TenantContext, quote_id: UUID) -> bool:
    """Vence una `SENT` o `VIEWED` con `valid_until <= ahora` (también para la base), como sistema.
    Devuelve si la venció ahora; si no (respondida, vigente, de otra organización), no escribe."""
    alias = require_scope(ctx)
    if ctx.actor_type != ActorType.SYSTEM:  # solo la tarea vence: nadie lo pide en su nombre
        raise NotAllowed("quotes.expire")
    try:
        with transaction.atomic(using=alias):
            rows = Quote.objects.using(alias).select_for_update(no_key=True).filter(pk=quote_id)
            quote = rows.first()
            now = timezone.now()
            if quote is None or quote.status not in OPEN or not quote.valid_until <= now:
                return False
            before = quote.status
            _stamp(ctx, alias, quote, {"status": QuoteStatus.EXPIRED})
            record(ctx, "quote.expired", _entity(quote), {"status": [before, QuoteStatus.EXPIRED]})
            _event(ctx, quote, "quote.expired")
    except IntegrityError as error:
        if _name(error) == "quotes_not_expired":  # el reloj de la base aún no: no es todavía
            return False
        raise
    return True
