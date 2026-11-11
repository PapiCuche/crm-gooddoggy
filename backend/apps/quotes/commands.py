"""Comandos de cotizaciones (F5-09a; E13-01, modelo de datos §E.6): crear un borrador desde un
contacto o una oportunidad, cambiarlo y cancelarlo. No comprueban permisos: los declarará la API
(F5-09d y F5-09e), la única que los importa (contrato de import-linter). Validan los tipos exactos
(`ValueError`, nunca `TypeError` ni `IntegrityError`) y auditan con el antes y el después; las
notas libres se auditan como `[texto]`.

Los precios salen del `PricingService` (ADR-006): una variante sin precio vigente, o una lista que
quien actúa no ve, es un `BadReference`; nada se estima. Cada línea congela el nombre, el SKU, el
precio de lista, la promoción y el precio final; los importes son `Decimal` con dos decimales y los
precios llevan IGV (D-PRC-1): `total` es lo que paga el cliente y `tax_total` el IGV que lleva.
Bloqueos: contacto y oportunidad (`FOR NO KEY UPDATE`), la cotización, la membresía de quien actúa.
"""

import datetime as dt
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from uuid import UUID

from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone

from apps.access.selectors import execution_context
from apps.audit.services import Entity, record
from apps.catalog.selectors import live_variants
from apps.contacts.models import Contact
from apps.deals.models import Opportunity
from apps.pricing.services import ListNotVisible, PriceResolution, resolve_many
from apps.quotes.models import QUANTITY_MAX, TERMINAL, Quote, QuoteItem, QuoteStatus
from core.sequences import allocate
from core.tenancy.context import TenantContext
from core.tenancy.scope import require_scope
from core.text import line

ITEMS_MAX, NOTE_MAX, REASON_MAX = 50, 2000, 500
TAX_RATE = Decimal("0.1800")  # IGV (D-PRC-1: los precios lo llevan incluido)
CENT = Decimal("0.01")
MONEY_MAX = Decimal(10) ** 12  # `numeric(14,2)`: un importe de la tabla es menor que esto
_ITEM = frozenset({"variant_id", "quantity"})
_FIELDS = frozenset({"valid_until", "notes_to_customer", "internal_notes"})
_REFERENCES = {  # FK compuesta violada → el campo que la causó
    "quotes_contact_fk": "contact_id",
    "quotes_opportunity_fk": "opportunity_id",
    "quotes_price_list_fk": "price_list_id",
    "quote_items_variant_fk": "variant_id",
    "quotes_created_by_fk": "created_by_user_id",  # quien actúa no es miembro
    "quotes_updated_by_fk": "updated_by_user_id",
}
_EARLIEST, _LATEST = dt.datetime(2000, 1, 1, tzinfo=dt.UTC), dt.datetime(2100, 1, 1, tzinfo=dt.UTC)


class StaleVersion(Exception):
    """Otra escritura cambió la cotización después de la versión que se editaba."""


class BadReference(ValueError):
    """Una referencia que no sirve (contacto, oportunidad, lista de precios o variante sin precio
    vigente). `field` dice cuál."""

    def __init__(self, field: str) -> None:
        super().__init__(field)
        self.field = field


class QuoteNotDraft(Exception):
    """Solo un borrador se edita: lo demás se revisa o se cancela."""


class QuoteTerminal(Exception):
    """La cotización ya terminó (aceptada, rechazada, vencida o cancelada)."""


def _uuid(value: Any, what: str) -> UUID:
    if isinstance(value, UUID):
        return value
    try:
        return UUID(value)
    except ValueError, TypeError, AttributeError:
        raise ValueError(f"{what}: un identificador") from None


def _integer(value: Any, low: int, high: int, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f"{what}: un entero de {low} a {high}")
    return int(value)


def _moment(value: Any) -> dt.datetime:
    if not (isinstance(value, dt.datetime) and value.utcoffset() is not None
            and _EARLIEST <= value <= _LATEST):  # fmt: skip
        raise ValueError("validez: fecha y hora con zona horaria, entre los años 2000 y 2100")
    return value.astimezone(dt.UTC)


def _text(value: Any, what: str, limit: int) -> str:
    """Texto libre (varias líneas) sin caracteres de control, hasta `limit`."""
    if not isinstance(value, str) or len(value) > limit * 4:
        raise ValueError(f"{what}: un texto de hasta {limit} caracteres")
    text = "\n".join(line(part, limit) for part in value.splitlines()).strip()
    if len(text) > limit:
        raise ValueError(f"{what}: hasta {limit} caracteres")
    return text


def _values(fields: dict[str, Any]) -> dict[str, Any]:
    values = dict(fields)
    if values.get("valid_until") is not None:
        values["valid_until"] = _moment(values["valid_until"])
    for field, what in (
        ("notes_to_customer", "notas al cliente"),
        ("internal_notes", "notas internas"),
    ):
        if field in values:
            values[field] = _text(values[field], what, NOTE_MAX)
    return values


def _items(raw: Any) -> list[tuple[UUID, int]]:
    """Las líneas pedidas, en orden: hasta 50, una por variante."""
    if not isinstance(raw, list | tuple) or not 1 <= len(raw) <= ITEMS_MAX:
        raise ValueError(f"las líneas, una lista de 1 a {ITEMS_MAX} objetos")
    wanted: dict[UUID, int] = {}
    for spec in raw:
        if not isinstance(spec, dict) or set(spec) != _ITEM:
            raise ValueError(f"cada línea es un objeto con {sorted(_ITEM)}")
        variant = _uuid(spec["variant_id"], "variante")
        if variant in wanted:
            raise ValueError("variante repetida")
        wanted[variant] = _integer(spec["quantity"], 1, QUANTITY_MAX, "cantidad")
    return list(wanted.items())


def _round(amount: Decimal) -> Decimal:
    return amount.quantize(CENT, ROUND_HALF_UP)


def _lines(
    alias: str, ctx: TenantContext, wanted: list[tuple[UUID, int]], price_list_id: UUID | None
) -> tuple[list[dict[str, Any]], PriceResolution]:
    """Las líneas con su precio y su snapshot, en una sola resolución del `PricingService` (la
    lista, el instante y las promociones son los mismos para todas). `BadReference` si la lista no
    se ve o una variante no tiene precio vigente."""
    ids = [variant for variant, _ in wanted]
    try:
        found = resolve_many(execution_context(ctx), ids, price_list_id)
    except ListNotVisible:
        raise BadReference("price_list_id") from None
    variants = {
        v.pk: v for v in live_variants().using(alias).select_related("product").filter(pk__in=ids)
    }
    rows: list[dict[str, Any]] = []
    for order, (variant, quantity) in enumerate(wanted, 1):
        price = found[variant]
        if not price.found or variant not in variants:
            raise BadReference("variant_id")
        assert price.base_amount is not None and price.final_amount is not None  # noqa: S101
        rows.append({
            "sort_order": order, "variant_id": variant, "quantity": quantity,
            "product_name_snapshot": variants[variant].product.name,
            "variant_name_snapshot": variants[variant].name, "sku_snapshot": variants[variant].sku,
            "list_unit_price": price.base_amount, "price_id_snapshot": price.price_id,
            "promotion_id_snapshot": price.promotion_id,
            "promotion_name_snapshot": price.promotion.name[:100] if price.promotion else None,
            "unit_price": price.final_amount, "final_unit_price": price.final_amount,
            "line_total": quantity * price.final_amount, "currency": price.currency,
        })  # fmt: skip
    return rows, found[ids[0]]


def _totals(rows: list[dict[str, Any]], rate: Decimal) -> dict[str, Decimal]:
    """Con IGV incluido: el total es la suma de las líneas, el IGV lo que lleva, y el neto el resto;
    el descuento es lo que se ahorra frente a la lista."""
    total = sum((r["line_total"] for r in rows), Decimal(0))
    saved = sum(
        (r["quantity"] * (r["list_unit_price"] - r["final_unit_price"]) for r in rows), Decimal(0)
    )
    if max(total, saved) >= MONEY_MAX:  # no cabe en la columna: un error claro, no uno de la base
        raise ValueError("importe: la cotización pasa del máximo")
    tax = _round(total - total / (1 + rate))
    return {"subtotal": total - tax, "discount_total": saved, "tax_total": tax, "total": total}


def _shown(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Las líneas para la auditoría: lo que se vendió y a cuánto, sin nombres."""
    return [{"variant_id": str(r["variant_id"]), "quantity": r["quantity"],
             "unit_price": str(r["final_unit_price"])} for r in rows]  # fmt: skip


def _entity(quote: Quote) -> Entity:
    return Entity("quote", quote.pk, quote.number)


def _reference(error: IntegrityError) -> Exception:
    for constraint, field in _REFERENCES.items():
        if constraint in str(error):
            return BadReference(field)
    raise error


def _locked(alias: str, quote_id: UUID) -> Quote:
    found: Quote = Quote.objects.using(alias).select_for_update(no_key=True).get(pk=quote_id)
    return found


def _links(alias: str, contact_id: UUID | None, opportunity_id: UUID | None) -> UUID:
    """Bloquea el contacto y la oportunidad vivos y devuelve el contacto de la cotización: el dado
    o, sin él, el de la oportunidad, que ha de ser de ese contacto."""
    if opportunity_id is not None:
        live = Opportunity.objects.using(alias).filter(pk=opportunity_id, deleted_at__isnull=True)
        found = live.select_for_update(no_key=True).values_list("contact_id", flat=True)[:1]
        if not found or contact_id not in (None, found[0]):
            raise BadReference("opportunity_id")
        contact_id = found[0]
    if contact_id is None:
        raise ValueError("un contacto o una oportunidad")
    live_contact = Contact.objects.using(alias).filter(pk=contact_id, deleted_at__isnull=True)
    if not live_contact.select_for_update(no_key=True).values_list("pk", flat=True)[:1]:
        raise BadReference("contact_id")
    return contact_id


def create_quote(
    ctx: TenantContext,
    *,
    items: Any,
    contact_id: UUID | None = None,
    opportunity_id: UUID | None = None,
    price_list_id: UUID | None = None,
    **fields: Any,
) -> Quote:
    """Crea un borrador (`DRAFT`, revisión 1, con su número `COT-…`) de un contacto vivo o de una
    oportunidad viva (que ha de ser de ese contacto), con las líneas `{variant_id, quantity}`
    valoradas por el `PricingService` en la lista pedida o la predeterminada. `BadReference` con el
    campo; `ValueError`. Audita `quote.created`."""
    alias = require_scope(ctx)
    if unknown := set(fields) - _FIELDS:
        raise ValueError(f"campos no admitidos: {sorted(unknown)}")
    values, wanted = _values(fields), _items(items)
    contact = None if contact_id is None else _uuid(contact_id, "contacto")
    opportunity = None if opportunity_id is None else _uuid(opportunity_id, "oportunidad")
    listed = None if price_list_id is None else _uuid(price_list_id, "lista de precios")
    try:
        with transaction.atomic(using=alias):
            contact = _links(alias, contact, opportunity)
            rows, price = _lines(alias, ctx, wanted, listed)
            quote: Quote = Quote.objects.using(alias).create(
                number=allocate(ctx, "COT", using=alias), contact_id=contact,
                opportunity_id=opportunity, price_list_id=price.price_list_id,
                currency=price.currency, tax_rate=TAX_RATE, created_by_user_id=ctx.user_id,
                updated_by_user_id=ctx.user_id, **_totals(rows, TAX_RATE), **values,
            )  # fmt: skip
            QuoteItem.objects.using(alias).bulk_create(
                QuoteItem(organization_id=ctx.organization_id, quote=quote, **row) for row in rows
            )
            shown = {"contact_id": contact, "opportunity_id": opportunity,
                     "price_list_id": price.price_list_id, "status": QuoteStatus.DRAFT,
                     "currency": price.currency, "total": quote.total,
                     "valid_until": values.get("valid_until")}  # fmt: skip
            changes: dict[str, Any] = {k: [None, v] for k, v in shown.items()}
            changes["items"] = [None, _shown(rows)]
            for field in ("notes_to_customer", "internal_notes"):
                if values.get(field):
                    changes[field] = [None, "[texto]"]
            record(ctx, "quote.created", _entity(quote), changes)
    except IntegrityError as error:
        raise _reference(error) from None
    return quote


def _stamp(ctx: TenantContext, alias: str, quote: Quote, changes: dict[str, Any]) -> None:
    for field, value in changes.items():
        setattr(quote, field, value)
    quote.updated_by_user_id = ctx.user_id
    quote.version = F("version") + 1
    quote.save(using=alias, update_fields=[*changes, "updated_by_user_id", "version", "updated_at"])
    quote.refresh_from_db(using=alias, fields=["version"])


def update_quote(ctx: TenantContext, *, quote_id: UUID, version: int, **fields: Any) -> Quote:
    """Cambia lo que se envía de un borrador (`valid_until`, las notas y `items`, que reemplazan
    todas las líneas y se vuelven a valorar, en su lista y a la hora de ahora) si nadie lo cambió
    desde `version`, y audita `quote.updated`; sin cambios no escribe. `DoesNotExist`;
    `StaleVersion`; `QuoteNotDraft`; `BadReference`; `ValueError`."""
    alias = require_scope(ctx)
    if unknown := set(fields) - _FIELDS - {"items"}:
        raise ValueError(f"campos no admitidos: {sorted(unknown)}")
    values = _values({k: v for k, v in fields.items() if k != "items"})
    wanted = _items(fields["items"]) if "items" in fields else None
    quote_id = _uuid(quote_id, "cotización")
    _integer(version, 1, 2**31 - 1, "versión")
    try:
        with transaction.atomic(using=alias):
            quote = _locked(alias, quote_id)
            if quote.version != version:
                raise StaleVersion(quote_id)
            if quote.status != QuoteStatus.DRAFT:
                raise QuoteNotDraft(quote_id)
            changes: dict[str, Any] = {f: v for f, v in values.items() if getattr(quote, f) != v}
            audited: dict[str, Any] = {f: [getattr(quote, f), v] for f, v in changes.items()}
            for field in ("notes_to_customer", "internal_notes"):
                if field in audited:
                    audited[field] = ["[texto]", "[texto]"]
            if wanted is not None:
                rows, _ = _lines(alias, ctx, wanted, quote.price_list_id)
                totals = _totals(rows, quote.tax_rate)  # antes de tocar nada: puede no caber
                kept = (
                    QuoteItem.objects.using(alias).filter(quote_id=quote.pk).order_by("sort_order")
                )
                old = [
                    (i.variant_id, i.quantity, i.list_unit_price, i.final_unit_price) for i in kept
                ]
                new = [
                    (r["variant_id"], r["quantity"], r["list_unit_price"], r["final_unit_price"])
                    for r in rows
                ]
                if old != new:
                    kept.delete()
                    QuoteItem.objects.using(alias).bulk_create(
                        QuoteItem(organization_id=ctx.organization_id, quote=quote, **row)
                        for row in rows
                    )
                    changes |= totals
                    audited["items"] = [None, _shown(rows)]
                    audited["total"] = [quote.total, changes["total"]]
            if changes:
                _stamp(ctx, alias, quote, changes)
                record(ctx, "quote.updated", _entity(quote), audited)
    except IntegrityError as error:
        raise _reference(error) from None
    return quote


def cancel_quote(
    ctx: TenantContext, *, quote_id: UUID, version: int, reason: str | None = None
) -> Quote:
    """Cancela una cotización que no ha terminado, con un motivo opcional (texto libre, que se
    audita como `[texto]`) y la fecha, y audita `quote.cancelled`. `QuoteTerminal` si ya terminó."""
    alias = require_scope(ctx)
    why = None if reason is None else _text(reason, "motivo", REASON_MAX) or None
    quote_id = _uuid(quote_id, "cotización")
    _integer(version, 1, 2**31 - 1, "versión")
    with transaction.atomic(using=alias):
        quote = _locked(alias, quote_id)
        if quote.version != version:
            raise StaleVersion(quote_id)
        if quote.status in TERMINAL:
            raise QuoteTerminal(quote_id)
        before = quote.status
        gone = {
            "status": QuoteStatus.CANCELLED,
            "cancelled_at": timezone.now(),
            "cancel_reason": why,
        }
        _stamp(ctx, alias, quote, gone)
        shown = "[texto]" if why else None
        record(
            ctx,
            "quote.cancelled",
            _entity(quote),
            {"status": [before, QuoteStatus.CANCELLED], "cancel_reason": [None, shown]},
        )
    return quote
