"""Comandos de cotizaciones (F5-09a2; E13-01): crear un borrador desde un contacto o una
oportunidad, cambiarlo y cancelarlo. Solo la API (F5-09e) los importa (contrato de import-linter).
Comprueban la autoridad de quien actúa (F5-09c), siempre en este orden: el permiso, la
cotización bloqueada, su **alcance** (`quotes.view` y el de la acción), la versión y el estado;
crear pide `quotes.create` y el alcance del contacto y de la oportunidad lo pone la API. Validan
los tipos exactos
(`ValueError`, nunca `TypeError` ni `IntegrityError`) y auditan con el antes y el después; el
texto libre se audita como `[texto]`.

Los precios salen del `PricingService` (ADR-006): una variante sin precio vigente, una lista que
quien actúa no ve o un precio sin IGV incluido es un `BadReference`; nada se estima. Cada línea
congela nombre, SKU, precio de lista, promoción y precio final; los importes son `Decimal` con dos
decimales y llevan IGV (D-PRC-1). Bloqueos: contacto y oportunidad (`FOR NO KEY UPDATE`) y la
cotización; la membresía de quien actúa no, porque la FK de la autoría la rechaza.
"""

import datetime as dt
from collections.abc import Callable
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from uuid import UUID

from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone

from apps.access.selectors import can, execution_context, has_permission
from apps.audit.services import Entity, record
from apps.catalog.selectors import live_variants
from apps.contacts.models import Contact
from apps.deals.models import Opportunity
from apps.pricing.services import ListNotVisible, PriceResolution, resolve_many
from apps.quotes.models import (
    QUANTITY_MAX,
    TERMINAL,
    ApprovalStatus,
    Quote,
    QuoteApproval,
    QuoteItem,
    QuoteStatus,
)
from core.outbox import emit
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


class QuoteRefused(Exception):
    """La base rechazó el cambio por una regla que los comandos no previeron."""


class NotAllowed(Exception):
    """Quien actúa no tiene el permiso que pide esta acción."""


NOT_FOUND = "Quote matching query does not exist."


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
    se ve o no lleva el IGV incluido, o una variante no tiene precio vigente."""
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
        if price.tax_included is not True:  # D-PRC-1: solo se cotiza con IGV incluido
            raise BadReference("price_list_id")
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


def _event(ctx: TenantContext, quote: Quote, name: str, **ids: UUID) -> None:
    """El evento del outbox en la misma transacción: solo identificadores y el estado."""
    emit(ctx, name, aggregate_type="quote", aggregate_id=quote.pk, payload={
        "quote_id": str(quote.pk), "contact_id": str(quote.contact_id),
        "opportunity_id": None if quote.opportunity_id is None else str(quote.opportunity_id),
        "status": quote.status, "revision": quote.revision,
        **{k: str(v) for k, v in ids.items()},
    })  # fmt: skip


def _reference(error: IntegrityError) -> Exception:
    """La restricción que la base violó, por su nombre (no por el texto): una referencia que no
    sirve es `BadReference`; cualquier otra, `QuoteRefused`, sin detalle (nunca un error crudo)."""
    name = getattr(getattr(error.__cause__, "diag", None), "constraint_name", None)
    return BadReference(_REFERENCES[name]) if name in _REFERENCES else QuoteRefused()


def _locked(alias: str, quote_id: UUID) -> Quote:
    found: Quote = Quote.objects.using(alias).select_for_update(no_key=True).get(pk=quote_id)
    return found


def _require(ctx: TenantContext, code: str) -> Any:
    ectx = execution_context(ctx)
    if not has_permission(ectx, code):
        raise NotAllowed(code)
    return ectx


def _reachable(alias: str, quote_id: UUID, allowed: Callable[[Quote], bool]) -> Quote:
    """La cotización bloqueada si `allowed` (el alcance de quien actúa sobre ella). Una que no
    existe, de otra organización o fuera del alcance es **la misma** `DoesNotExist`, con el mismo
    mensaje: no se le cuenta a nadie cuál de las tres es (la API responde 404). Se lee **sin
    bloquear** y solo si es suya se bloquea (y se vuelve a comprobar con la fila bloqueada): quien
    no la alcanza no espera a quien la tiene, así que el tiempo no cuenta si existe."""
    try:
        if not allowed(Quote.objects.using(alias).get(pk=quote_id)):
            raise Quote.DoesNotExist
        found = _locked(alias, quote_id)
    except Quote.DoesNotExist:
        raise Quote.DoesNotExist(NOT_FOUND) from None
    if not allowed(found):
        raise Quote.DoesNotExist(NOT_FOUND)
    return found


def _within(ectx: Any, *codes: str) -> Callable[[Quote], bool]:
    """El alcance sobre la cotización: todos los permisos, cada uno con el suyo (`quotes.view` y el
    de la acción: tener uno no da el otro)."""
    return lambda quote: all(can(ectx, code, quote) for code in codes)


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
    campo; `ValueError`; `NotAllowed` sin `quotes.create`. Audita `quote.created`."""
    alias = require_scope(ctx)
    _require(ctx, "quotes.create")  # lo primero: sin permiso, ni se mira lo que se envía
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
    desde `version`, y audita `quote.updated`; sin cambios no escribe. `NotAllowed` sin
    `quotes.update`; `DoesNotExist` (también fuera del alcance); `StaleVersion`; `QuoteNotDraft`;
    `BadReference`; `ValueError`."""
    alias = require_scope(ctx)
    ectx = _require(ctx, "quotes.update")  # lo primero: sin permiso, ni se mira lo que se envía
    if unknown := set(fields) - _FIELDS - {"items"}:
        raise ValueError(f"campos no admitidos: {sorted(unknown)}")
    values = _values({k: v for k, v in fields.items() if k != "items"})
    wanted = _items(fields["items"]) if "items" in fields else None
    quote_id = _uuid(quote_id, "cotización")
    _integer(version, 1, 2**31 - 1, "versión")
    try:
        with transaction.atomic(using=alias):
            quote = _reachable(alias, quote_id, _within(ectx, "quotes.view", "quotes.update"))
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
    """Cancela una cotización que no ha terminado (`quotes.cancel` en su alcance), con un motivo
    opcional (texto libre, que se audita como `[texto]`) y la fecha, y audita `quote.cancelled`.
    Si espera aprobación, su solicitud abierta se cancela **antes**, en la misma transacción (la
    base no deja salir de `PENDING_APPROVAL` con una abierta), y solo lo hace quien la pidió (con
    `quotes.update` en su alcance) o quien puede aprobar. `NotAllowed`, `DoesNotExist`,
    `StaleVersion`, `QuoteTerminal`."""
    alias = require_scope(ctx)
    ectx = _require(ctx, "quotes.cancel")  # lo primero: sin permiso, ni se mira lo que se envía
    why = None if reason is None else _text(reason, "motivo", REASON_MAX) or None
    quote_id = _uuid(quote_id, "cotización")
    _integer(version, 1, 2**31 - 1, "versión")
    try:
        with transaction.atomic(using=alias):
            quote = _reachable(alias, quote_id, _within(ectx, "quotes.view", "quotes.cancel"))
            if quote.version != version:
                raise StaleVersion(quote_id)
            if quote.status in TERMINAL:
                raise QuoteTerminal(quote_id)
            before = quote.status
            shown: dict[str, Any] = {
                "status": [before, QuoteStatus.CANCELLED],
                "cancel_reason": [None, "[texto]" if why else None],
            }
            if before == QuoteStatus.PENDING_APPROVAL:
                open_ = QuoteApproval.objects.using(alias).select_for_update(no_key=True)
                asked = open_.filter(quote_id=quote.pk, status=ApprovalStatus.PENDING).first()
                mine = asked is not None and asked.requested_by_user_id == ctx.user_id
                if asked is None or not (  # sin solicitud abierta, tampoco: la base no lo permite
                    (can(ectx, "quotes.approve") and can(ectx, "quotes.view", quote))
                    or (mine and can(ectx, "quotes.update", quote))
                ):  # como al retirar: quien pidió o quien puede aprobar (la de la IA, solo este)
                    raise NotAllowed("quotes.cancel")
                asked.status, asked.decided_at = ApprovalStatus.CANCELLED, timezone.now()
                asked.decided_by_user_id, asked.version = ctx.user_id, asked.version + 1
                asked.save(using=alias, update_fields=[
                    "status", "decided_at", "decided_by_user_id", "version", "updated_at",
                ])  # fmt: skip
                shown["approval_id"] = [str(asked.pk), str(asked.pk)]
                shown["approval_status"] = [ApprovalStatus.PENDING, ApprovalStatus.CANCELLED]
            gone = {"status": QuoteStatus.CANCELLED, "cancelled_at": timezone.now(),
                    "cancel_reason": why}  # fmt: skip
            _stamp(ctx, alias, quote, gone)
            record(ctx, "quote.cancelled", _entity(quote), shown)
            _event(ctx, quote, "quote.cancelled")
    except IntegrityError as error:
        raise _reference(error) from None
    return quote
