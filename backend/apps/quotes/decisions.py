"""Aprobar, rechazar y retirar la solicitud de aprobación de una cotización (F5-09b3; 04 §L.3). Solo
`apps.quotes.api` los importará. Cada uno, en una transacción, bloquea la cotización y su solicitud
abierta (`FOR NO KEY UPDATE`), pasa la solicitud a su estado y la cotización a `APPROVED` (aprobar)
o a `DRAFT` (rechazar, retirar), con versión y auditoría del antes y el después
(`quote.approved`, `quote.rejected`, `quote.approval_withdrawn`; el texto libre, como `[texto]`).

**Orden.** El permiso (`quotes.approve`; para retirar, ese o `quotes.update`) va antes de leer.
Con la cotización bloqueada, su **alcance** (aprobar y rechazar exigen verla, `quotes.view`;
retirar, el de `quotes.update`): una que no existe, de otra organización o fuera del alcance es la
misma `DoesNotExist`, sin oráculo de existencia. Luego la versión (`StaleVersion`) y el estado
(`NotPending`); al retirar, quién puede con esa solicitud va antes de ambos.

**Aprobar** exige que quien aprueba no sea quien pidió (`SelfApproval`), que no supere **su propio**
límite (`BeyondLimit`, el cálculo de `submit_quote`) y que cada precio manual **vuelva a cubrir el
costo de hoy** (`PriceRefused`, sin cifras). La solicitud de la IA nunca se aprueba sola: decide una
persona. **Retirar** lo hace quien pidió (con el alcance de `quotes.update`) o quien puede aprobar;
la de la IA, solo quien puede aprobar. **Rechazar** lleva su motivo."""

from collections.abc import Callable
from typing import Any
from uuid import UUID

from django.db import transaction
from django.utils import timezone

from apps.access.selectors import can, execution_context, has_permission
from apps.audit.services import record
from apps.quotes.commands import (
    NotAllowed,
    StaleVersion,
    _entity,
    _integer,
    _reachable,
    _require,
    _stamp,
    _text,
    _uuid,
)
from apps.quotes.discounts import REASON_MAX, _authority, _floor, _lines
from apps.quotes.models import ApprovalStatus, Quote, QuoteApproval, QuoteStatus
from core.tenancy.context import TenantContext
from core.tenancy.scope import require_scope

COMMENT_MAX = 500


class NotPending(Exception):
    """La cotización no espera una decisión: ya se decidió, se retiró o nunca se pidió."""


class SelfApproval(Exception):
    """Quien aprueba es quien pidió: la solicitud la decide otra persona."""


class BeyondLimit(Exception):
    """El descuento de la cotización supera el límite de quien aprueba."""


def _can_decide(ectx: Any, quote: Quote) -> bool:
    return can(ectx, "quotes.approve", quote) and can(ectx, "quotes.view", quote)


def _pending(
    alias: str, quote_id: UUID, version: int, reach: Callable[[Quote], bool],
    only: Callable[[Quote, QuoteApproval], bool] | None = None,
) -> tuple[Quote, QuoteApproval]:  # fmt: skip
    """La cotización bloqueada y al alcance (`reach`, o `DoesNotExist`), su solicitud abierta
    (bloqueada) con `only` (`NotAllowed`) **antes** de la versión y el estado, y luego estos."""
    found = _reachable(alias, quote_id, reach)
    asked = QuoteApproval.objects.using(alias).select_for_update(no_key=True).filter(
        quote_id=found.pk, status=ApprovalStatus.PENDING
    ).first()  # fmt: skip
    if asked is not None and only is not None and not only(found, asked):
        raise NotAllowed("quotes.approve")  # ni de otra persona ni la de la IA (sin nadie)
    if found.version != version:
        raise StaleVersion(quote_id)
    if found.status != QuoteStatus.PENDING_APPROVAL or asked is None:
        raise NotPending
    return found, asked


def _close(ctx: TenantContext, alias: str, quote: Quote, asked: QuoteApproval, *, action: str,
           approval: str, status: str, comment: str) -> None:  # fmt: skip
    """La solicitud decidida primero (así lo exige la base al salir de `PENDING_APPROVAL`)."""
    before = quote.status
    asked.status, asked.decided_by_user_id, asked.decided_at = approval, ctx.user_id, timezone.now()
    asked.comment, asked.version = comment, asked.version + 1
    asked.save(using=alias, update_fields=[
        "status", "decided_by_user_id", "decided_at", "comment", "version", "updated_at",
    ])  # fmt: skip
    _stamp(ctx, alias, quote, {"status": status})
    changes: dict[str, Any] = {
        "status": [before, status],
        "approval_id": [str(asked.pk)] * 2,
        "approval_status": [ApprovalStatus.PENDING, approval],
    }
    if comment:
        changes["comment"] = [None, "[texto]"]
    record(ctx, action, _entity(quote), changes)


def approve_quote(
    ctx: TenantContext, *, quote_id: UUID, version: int, comment: str | None = None
) -> Quote:
    """Aprueba la solicitud abierta (`quotes.approve`): la cotización queda `APPROVED`. Quien
    aprueba no es quien pidió, no supera su límite y los precios manuales vuelven a cubrir el costo.
    `NotAllowed`, `DoesNotExist`, `StaleVersion`, `NotPending`, `SelfApproval`, `BeyondLimit`,
    `PriceRefused` (nada se escribe)."""
    alias = require_scope(ctx)
    quote_id = _uuid(quote_id, "cotización")
    _integer(version, 1, 2**31 - 1, "versión")
    note = "" if comment is None else _text(comment, "comentario", COMMENT_MAX)
    with transaction.atomic(using=alias):
        ectx = _require(ctx, "quotes.approve")
        quote, asked = _pending(alias, quote_id, version, lambda q: _can_decide(ectx, q))
        if ctx.user_id in (asked.requested_by_user_id, asked.submitted_by_user_id):
            raise SelfApproval  # ni quien pidió ni quien envió (la de la IA no tiene a quien pidió)
        lines = _lines(alias, quote)
        _floor(ctx, quote, lines)
        if not _authority(alias, ectx, quote, lines)[3]:
            raise BeyondLimit
        _close(ctx, alias, quote, asked, action="quote.approved", approval=ApprovalStatus.APPROVED,
               status=QuoteStatus.APPROVED, comment=note)  # fmt: skip
    return quote


def reject_quote(ctx: TenantContext, *, quote_id: UUID, version: int, reason: str) -> Quote:
    """Rechaza la solicitud abierta (`quotes.approve`), con su motivo obligatorio: la cotización
    vuelve a `DRAFT`. Quien pidió también puede (equivale a retirarla), si puede aprobar."""
    alias = require_scope(ctx)
    quote_id = _uuid(quote_id, "cotización")
    _integer(version, 1, 2**31 - 1, "versión")
    why = _text(reason, "motivo", REASON_MAX)
    if not why:
        raise ValueError("motivo: obligatorio")
    with transaction.atomic(using=alias):
        ectx = _require(ctx, "quotes.approve")
        quote, asked = _pending(alias, quote_id, version, lambda q: _can_decide(ectx, q))
        _close(ctx, alias, quote, asked, action="quote.rejected", approval=ApprovalStatus.REJECTED,
               status=QuoteStatus.DRAFT, comment=why)  # fmt: skip
    return quote


def withdraw_approval_request(ctx: TenantContext, *, quote_id: UUID, version: int) -> Quote:
    """Retira la solicitud abierta y devuelve la cotización a `DRAFT`: quien la pidió (con el
    alcance de `quotes.update`) o quien puede aprobar; la solicitud de la IA solo quien puede
    aprobar. La solicitud queda `CANCELLED`."""
    alias = require_scope(ctx)
    quote_id = _uuid(quote_id, "cotización")
    _integer(version, 1, 2**31 - 1, "versión")
    with transaction.atomic(using=alias):
        ectx = execution_context(ctx)
        if not (has_permission(ectx, "quotes.approve") or has_permission(ectx, "quotes.update")):
            raise NotAllowed("quotes.update")
        quote, asked = _pending(
            alias, quote_id, version,
            lambda q: _can_decide(ectx, q) or can(ectx, "quotes.update", q),
            lambda q, a: _can_decide(ectx, q) or a.requested_by_user_id == ctx.user_id,
        )  # fmt: skip
        _close(ctx, alias, quote, asked, action="quote.approval_withdrawn",
               approval=ApprovalStatus.CANCELLED, status=QuoteStatus.DRAFT, comment="")  # fmt: skip
    return quote
