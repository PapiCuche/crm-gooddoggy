"""Descuentos, precio manual y solicitud de aprobación de una cotización (F5-09b2; 04 §L.3). Solo
`apps.quotes.api` los importará; comprueban la autoridad de quien actúa (`quotes.discount`,
`quotes.override_price` y el alcance de `quotes.update` sobre la cotización, ya bloqueada).

**Descuento** es lo quitado a mano del precio que paga el cliente (lista o promoción), hacia arriba
y por línea; se compara con la política más alta de los roles de quien actúa (sin política, 0 %; el
Owner sin límite) y su tope en importe. Pasar del límite, o un precio manual (que cubre el costo,
`apps.pricing.floor`), deja `PENDING_APPROVAL` con su solicitud y las líneas congeladas.
`submit_quote` es la única vía de `DRAFT` a `APPROVED` y mide con quien envía; aprobar, rechazar y
retirar la solicitud son de `apps.quotes.decisions` (F5-09b3)"""

from decimal import ROUND_HALF_UP, ROUND_UP, Decimal
from typing import Any
from uuid import UUID

from django.db import transaction
from django.db.models import Q

from apps.access.models import MembershipRole, Role
from apps.access.selectors import can, execution_context, has_permission
from apps.audit.services import record
from apps.pricing.floor import covers_cost
from apps.quotes.commands import (
    CENT,
    QuoteNotDraft,
    StaleVersion,
    _entity,
    _integer,
    _locked,
    _stamp,
    _text,
    _totals,
    _uuid,
)
from apps.quotes.models import (
    ApprovalReason,
    DiscountPolicy,
    DiscountType,
    Quote,
    QuoteApproval,
    QuoteItem,
    QuoteStatus,
)
from core.money import parse_amount
from core.tenancy.context import TenantContext
from core.tenancy.scope import require_scope

HUNDRED = Decimal(100)
REASON_MAX = 200


class NotAllowed(Exception):
    """Quien actúa no tiene el permiso que pide esta acción."""


class PriceRefused(Exception):
    """El precio no se admite, sin decir por qué (el costo no se revela)."""


Verdict = tuple[Decimal, Decimal, Decimal, str | None]  # % e importe quitados, límite, motivo


def _require(ctx: TenantContext, code: str) -> Any:
    ectx = execution_context(ctx)
    if not has_permission(ectx, code):
        raise NotAllowed(code)
    return ectx


def _reach(ectx: Any, quote: Quote) -> None:
    """El alcance de `quotes.update` de quien actúa sobre la cotización ya bloqueada."""
    if not can(ectx, "quotes.update", quote):
        raise NotAllowed("quotes.update")


def _given(line: QuoteItem) -> Decimal:
    return line.quantity * (line.unit_price - line.final_unit_price)


def _percent(amount: Decimal, base: Decimal) -> Decimal:
    return (amount / base * HUNDRED).quantize(CENT, ROUND_UP) if base else Decimal("0.00")


def _authority(
    alias: str, ectx: Any, quote: Quote, lines: list[QuoteItem]
) -> tuple[Decimal, Decimal, Decimal, bool]:
    """Lo quitado a mano (% por línea e importe), el límite de quien actúa y si lo cubre."""
    amount = sum((_given(line) for line in lines), Decimal(0))
    # por línea, sobre lo que paga el cliente (con la promoción); el total es una media de ellas
    percent = max(
        (_percent(_given(x), x.quantity * x.unit_price) for x in lines), default=Decimal(0)
    )
    roles = MembershipRole.objects.using(alias).filter(membership_id=ectx.membership_id)
    role_ids = list(roles.values_list("role_id", flat=True))
    unlimited = Role.objects.using(alias).filter(pk__in=role_ids, is_owner_role=True).exists()
    policies = list(
        DiscountPolicy.objects.using(alias)
        .filter(role_id__in=role_ids)
        .filter(Q(applies_to_price_list_id=quote.price_list_id) | Q(applies_to_price_list_id=None))
    )
    limit = HUNDRED if unlimited else max((p.max_discount_percent for p in policies), default=0)
    within = unlimited or not amount or any(
        p.max_discount_percent >= percent
        and (p.max_discount_amount is None
             or (p.currency == quote.currency and amount <= p.max_discount_amount))
        for p in policies
    )  # fmt: skip
    return percent, amount, Decimal(limit), within


def _verdict(alias: str, ectx: Any, quote: Quote, lines: list[QuoteItem]) -> Verdict:
    percent, amount, limit, within = _authority(alias, ectx, quote, lines)
    reason: str | None = None
    if any(line.manual_price_override for line in lines):
        reason = ApprovalReason.MANUAL_PRICE
    elif not within:
        reason = ApprovalReason.DISCOUNT_OVER_LIMIT
    return percent, amount, limit, reason


def _lines(alias: str, quote: Quote) -> list[QuoteItem]:
    return list(QuoteItem.objects.using(alias).filter(quote_id=quote.pk).order_by("sort_order"))


def _open(alias: str, quote_id: UUID, version: int, ectx: Any) -> Quote:
    """La cotización bloqueada, al alcance de quien actúa (antes que su versión o estado, que no
    se le cuentan a quien no la ve), con su versión y en borrador."""
    found = _locked(alias, quote_id)
    _reach(ectx, found)
    if found.version != version:
        raise StaleVersion(quote_id)
    if found.status != QuoteStatus.DRAFT:
        raise QuoteNotDraft(quote_id)
    return found


def _money(value: Any, what: str) -> Decimal:
    """Un importe escrito por una persona: `Decimal` o texto, nunca un `float` (`ValueError`)."""
    try:
        return parse_amount(value)
    except TypeError:
        raise ValueError(f"{what}: un importe como texto o Decimal") from None


def _ask(ctx: TenantContext, alias: str, quote: Quote, verdict: Verdict) -> QuoteApproval | None:
    percent, _, limit, reason = verdict
    if reason is None:
        return None
    asked: QuoteApproval = QuoteApproval.objects.using(alias).create(
        organization_id=ctx.organization_id, quote_id=quote.pk, reason=reason,
        requested_by_user_id=ctx.user_id, requested_discount_percent=percent,
        limit_percent_at_request=limit,
    )  # fmt: skip
    record(ctx, "quote.approval_requested", _entity(quote), {
        "approval_id": [None, str(asked.pk)], "reason": [None, reason],
        "requested_percent": [None, str(percent)], "limit_percent": [None, str(limit)],
    })  # fmt: skip
    return asked


def _settle(ctx: TenantContext, alias: str, ectx: Any, quote: Quote, action: str,
            shown: dict[str, Any]) -> None:  # fmt: skip
    lines = _lines(alias, quote)
    rows = [
        {
            "quantity": x.quantity,
            "list_unit_price": x.list_unit_price,
            "final_unit_price": x.final_unit_price,
            "line_total": x.line_total,
        }
        for x in lines
    ]
    changes: dict[str, Any] = _totals(rows, quote.tax_rate)
    before = quote.total
    if _ask(ctx, alias, quote, _verdict(alias, ectx, quote, lines)):
        changes["status"] = QuoteStatus.PENDING_APPROVAL
    _stamp(ctx, alias, quote, changes)
    record(ctx, action, _entity(quote), {**shown, "total": [str(before), str(quote.total)]})


def _line(alias: str, quote: Quote, line_id: Any) -> QuoteItem:
    found: QuoteItem = QuoteItem.objects.using(alias).get(pk=_uuid(line_id, "línea"), quote=quote)
    return found


def _save(alias: str, line: QuoteItem, **fields: Any) -> dict[str, Any]:
    """Cambia la línea y devuelve el antes y el después de lo que la audita."""
    names = ("discount_type", "discount_value", "final_unit_price", "manual_price_override")
    before = {n: str(getattr(line, n)) for n in names}
    for name, value in fields.items():
        setattr(line, name, value)
    line.line_total = line.quantity * line.final_unit_price
    line.save(using=alias)
    after = {n: str(getattr(line, n)) for n in names}
    return {"line": [str(line.pk)] * 2} | {
        n: [before[n], after[n]] for n in names if before[n] != after[n]
    }


def set_line_discount(
    ctx: TenantContext, *, quote_id: UUID, version: int, line_id: UUID,
    discount_type: str, discount_value: Any = None,
) -> Quote:  # fmt: skip
    """Descuento `PERCENT`, `AMOUNT` (por unidad) o `NONE` (lo quita, también un precio manual) de
    una línea de un borrador, con `quotes.discount`; nunca deja el precio en cero o menos."""
    alias = require_scope(ctx)
    kind, quote_id = DiscountType(discount_type), _uuid(quote_id, "cotización")
    if kind is DiscountType.NONE and discount_value is not None:
        raise ValueError("descuento: NONE no lleva valor")
    value = Decimal("0.00") if kind is DiscountType.NONE else _money(discount_value, "descuento")
    _integer(version, 1, 2**31 - 1, "versión")
    with transaction.atomic(using=alias):
        ectx = _require(ctx, "quotes.discount")
        quote = _open(alias, quote_id, version, ectx)
        line = _line(alias, quote, line_id)
        given = {
            DiscountType.NONE: Decimal(0),
            DiscountType.AMOUNT: value,
            DiscountType.PERCENT: (line.unit_price * value / HUNDRED).quantize(CENT, ROUND_HALF_UP),
        }[kind]
        if given >= line.unit_price:  # también más de 100 %: nunca deja el precio en cero o menos
            raise ValueError("descuento: deja el precio en cero o menos")
        shown = _save(alias, line, discount_type=kind, discount_value=value,
                      manual_price_override=False, override_reason=None,
                      discount_amount=given, final_unit_price=line.unit_price - given)  # fmt: skip
        _settle(ctx, alias, ectx, quote, "quote.line_discounted", shown)
    return quote


def set_manual_price(
    ctx: TenantContext, *, quote_id: UUID, version: int, line_id: UUID, price: Any, reason: str
) -> Quote:
    """Precio final escrito a mano (con IGV, menor que el vigente y con su motivo), con
    `quotes.override_price`; solo si cubre el costo (si no, `PriceRefused`). Pide aprobación."""
    alias = require_scope(ctx)
    amount, quote_id = _money(price, "precio"), _uuid(quote_id, "cotización")
    why = _text(reason, "motivo", REASON_MAX)
    if not why:
        raise ValueError("motivo: obligatorio")
    _integer(version, 1, 2**31 - 1, "versión")
    with transaction.atomic(using=alias):
        ectx = _require(ctx, "quotes.override_price")
        quote = _open(alias, quote_id, version, ectx)
        line = _line(alias, quote, line_id)
        if not 0 < amount < line.unit_price:
            raise ValueError("precio: mayor que cero y menor que el precio vigente")
        if not covers_cost(ctx, {line.variant_id: amount}, quote.currency, quote.tax_rate):
            raise PriceRefused  # el costo no se revela: ni cifras ni motivo
        shown = _save(alias, line, discount_type=DiscountType.NONE, discount_value=Decimal(0),
                      manual_price_override=True, override_reason=why,
                      discount_amount=line.unit_price - amount,
                      final_unit_price=amount)  # fmt: skip
        shown["override_reason"] = [None, "[texto]"]
        _settle(ctx, alias, ectx, quote, "quote.price_overridden", shown)
    return quote


def submit_quote(ctx: TenantContext, *, quote_id: UUID, version: int) -> Quote:
    """La única vía de `DRAFT` a `APPROVED`: mide con el límite de quien envía y, si no cabe o hay
    precio manual (que vuelve a pasar por el suelo de costo), deja `PENDING_APPROVAL`."""
    alias = require_scope(ctx)
    quote_id = _uuid(quote_id, "cotización")
    _integer(version, 1, 2**31 - 1, "versión")
    with transaction.atomic(using=alias):
        ectx = _require(ctx, "quotes.update")
        quote = _open(alias, quote_id, version, ectx)
        lines = _lines(alias, quote)
        manual = {x.variant_id: x.final_unit_price for x in lines if x.manual_price_override}
        if manual and not covers_cost(ctx, manual, quote.currency, quote.tax_rate):
            raise PriceRefused  # el costo pudo subir desde que se escribió el precio
        verdict = _verdict(alias, ectx, quote, lines)
        asked = _ask(ctx, alias, quote, verdict)
        status = QuoteStatus.PENDING_APPROVAL if asked else QuoteStatus.APPROVED
        _stamp(ctx, alias, quote, {"status": status})
        record(ctx, "quote.submitted", _entity(quote), {
            "status": [QuoteStatus.DRAFT, status], "requested_percent": [None, str(verdict[0])],
            "limit_percent": [None, str(verdict[2])],
            "approval_id": [None, None if asked is None else str(asked.pk)],
        })  # fmt: skip
    return quote
