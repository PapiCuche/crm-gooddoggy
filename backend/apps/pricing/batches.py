"""Edición masiva de precios (F3-49; E03-08; ADR-006 §3, 04 §K.5): el lote y su previsualización.

Crear un lote no cambia ningún precio: elige las variantes vivas de la selección, lee el precio que
rige en la lista en `effective_at` (sin él, ahora) y guarda el lote (`PREVIEWED`) con una línea por
variante: el precio de antes, el de después con su redondeo y la variación. Todo en la petición,
con un tope de `LINES_MAX` líneas: uno mayor se rechaza antes de escribir. Las rutas son F3-49b y
F3-49c (cancelar y las líneas); aplicar (`set_price` por línea, `STALE` y step-up), lo que sigue.

No comprueba permisos: los declarará la API (`prices.bulk_update`), la única que lo importa
(import-linter). Sí la lista: la que el actor no ve, como la que no existe o está inactiva, es «no
existe» (400 en `price_list_id`), porque las líneas enseñan sus precios. Solo una persona crea un
lote (`ActorRefused`, ADR-034: la IA no cambia precios). No lee costos (ADR-006 §2).
"""

from collections.abc import Iterable, Mapping
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from itertools import islice
from typing import Any
from uuid import UUID

from django.db import transaction
from django.db.models import QuerySet
from django.utils import timezone

from apps.access.selectors import execution_context
from apps.audit.services import Entity, record
from apps.catalog.models import ProductVariant
from apps.catalog.selectors import branch, brands, categories, live_variants, products
from apps.pricing.models import ALERT_PERCENT as ALERT_PERCENT  # la regla vive en el modelo
from apps.pricing.models import (
    BatchItemStatus,
    BatchKind,
    BatchStatus,
    PriceChangeBatch,
    PriceChangeBatchItem,
    PriceList,
)
from apps.pricing.selectors import prices_at, visible_batches, visible_price_lists
from apps.pricing.text import change_reason
from core.money import CENT, Rounding, amount_text, parse_amount, round_amount
from core.tenancy.context import ActorType, TenantContext
from core.tenancy.scope import require_scope

LINES_MAX = 2000  # síncrono, como la importación de stock (F3-21)
AMOUNT_MAX = Decimal("999999999999.99")  # NUMERIC(14,2)
PENDING, SKIPPED, ERROR = BatchItemStatus.PENDING, BatchItemStatus.SKIPPED, BatchItemStatus.ERROR
NO_PRICE = "Sin precio en la lista en esa fecha."
UNCHANGED = "Queda igual con el redondeo: no cambia."
NOT_POSITIVE = "El precio resultante no es mayor que cero."
TOO_BIG = "El precio resultante pasa del máximo."
FILTERS = ("category_id", "brand_id", "product_id", "variant_ids")
OPERATIONS = ("PERCENT_INCREASE", "PERCENT_DECREASE", "AMOUNT_INCREASE", "AMOUNT_DECREASE")


class Invalid(ValueError):
    """Un dato que no sirve; `field` es el campo del cuerpo (400)."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field


class TooManyLines(Exception):
    """La selección pasa de `LINES_MAX` variantes (400: hay que acotar los filtros)."""

    def __init__(self, count: int) -> None:
        super().__init__(count)
        self.count = count


class BatchStateConflict(Exception):
    """El estado del lote no admite el cambio (409); `status` es el que tiene."""

    def __init__(self, status: str) -> None:
        super().__init__(status)
        self.status = status


class ActorRefused(Exception):
    """Solo una persona (con `user_id`, que es su `actor_id`) edita precios en lote: un agente de
    IA, una integración o el sistema, no (ADR-034). No es un `ValueError`: su respuesta es el
    403, nunca un 400 sobre un campo."""

    def __init__(self, actor_type: str) -> None:
        super().__init__(actor_type)
        self.actor_type = actor_type


def require_person(ctx: TenantContext) -> None:
    if ctx.actor_type != ActorType.USER or ctx.user_id is None or ctx.actor_id != ctx.user_id:
        raise ActorRefused(ctx.actor_type)


def _value(operation: str, value: str | Decimal) -> Decimal:
    """Un importe mayor que cero o un porcentaje en (0, 100], con como mucho 2 decimales."""
    try:
        number = parse_amount(value)
    except ValueError, TypeError:
        raise Invalid("value", "hasta 12 cifras enteras y 2 decimales") from None
    if number <= 0 or (operation.startswith("PERCENT") and number > 100):
        raise Invalid("value", "mayor que cero y, en un porcentaje, hasta 100")
    return number


def _found(field: str, rows: QuerySet[Any], pk: UUID) -> Any:
    found = rows.filter(pk=pk).first()
    if found is None:
        raise Invalid(field, "no existe")
    return found


def _selection(
    alias: str, filters: Mapping[str, Any]
) -> tuple[QuerySet[ProductVariant], dict[str, Any]]:
    """Las variantes vivas de la selección (los filtros se combinan con «y») y cómo se guarda."""
    if not isinstance(filters, Mapping) or not set(filters) <= set(FILTERS):
        raise Invalid("filters", "solo `category_id`, `brand_id`, `product_id` y `variant_ids`")
    chosen = {key: value for key, value in filters.items() if value is not None}
    if not chosen:
        raise Invalid("filters", "al menos uno: categoría, marca, producto o variantes")
    rows = live_variants().using(alias)
    kept: dict[str, Any] = {k: str(v) for k, v in chosen.items()}
    if "category_id" in chosen:
        node = _found("category_id", categories().using(alias), chosen["category_id"])
        rows = rows.filter(product__category_id__in=branch(node).using(alias).values("id"))
    if "brand_id" in chosen:
        rows = rows.filter(
            product__brand=_found("brand_id", brands().using(alias), chosen["brand_id"])
        )
    if "product_id" in chosen:
        rows = rows.filter(
            product=_found("product_id", products().using(alias), chosen["product_id"])
        )
    if "variant_ids" in chosen:
        given = chosen["variant_ids"]
        if isinstance(given, str | bytes | Mapping) or not isinstance(given, Iterable):
            raise Invalid("variant_ids", "una lista")
        try:
            ids = [UUID(str(pk)) for pk in islice(given, LINES_MAX + 1)]
        except ValueError:
            raise Invalid("variant_ids", "identificadores de variante") from None
        if not ids or len(ids) > LINES_MAX or len(set(ids)) != len(ids):
            raise Invalid("variant_ids", f"de 1 a {LINES_MAX} variantes distintas")
        if live_variants().using(alias).filter(pk__in=ids).count() != len(ids):
            raise Invalid("variant_ids", "alguna variante no existe o está borrada")
        rows, kept["variant_ids"] = rows.filter(pk__in=ids), [str(pk) for pk in ids]
    return rows, kept


def _compute(operation: str, value: Decimal, old: Decimal, rounding: Rounding) -> Decimal:
    change = old * value / 100 if operation.startswith("PERCENT") else value
    raw = old + change if operation.endswith("INCREASE") else old - change
    return round_amount(raw, rounding)


def _line(
    operation: str, value: Decimal, rounding: Rounding, price: Any
) -> tuple[str, str, dict[str, Any]]:
    """El estado, el motivo y los importes de una línea con su precio de antes (o sin él)."""
    if price is None:
        return SKIPPED, NO_PRICE, {}
    old = price.amount
    new = _compute(operation, value, old, rounding)
    amounts: dict[str, Any] = {"current_price_id": price.pk, "old_amount": old}
    if new > AMOUNT_MAX:
        return ERROR, TOO_BIG, amounts
    delta = ((new - old) * 100 / old).quantize(CENT, rounding=ROUND_HALF_UP)
    amounts |= {"new_amount": new, "delta_percent": delta}
    if new <= 0:
        return ERROR, NOT_POSITIVE, amounts
    if new == old:
        return SKIPPED, UNCHANGED, amounts
    return PENDING, "", amounts


def alert(item: PriceChangeBatchItem) -> bool:
    """Una variación de más del `ALERT_PERCENT` % (04 §K.5), sobre la `delta_percent` guardada: la
    regla de `models.ALERTING`, el filtro `?alert=` de las líneas, así cuentan las mismas."""
    return item.alert


def create_batch(
    ctx: TenantContext,
    *,
    price_list_id: UUID,
    operation: str,
    value: str | Decimal,
    filters: Mapping[str, Any],
    reason: str,
    rounding: str = Rounding.CENTS,
    effective_at: datetime | None = None,
) -> PriceChangeBatch:
    """Crea el lote (`BULK_ADJUST`, `PREVIEWED`) con sus líneas y lo audita
    (`price.bulk_previewed`, con los recuentos y sin las líneas). No cambia ningún precio.
    `ActorRefused` antes de la primera consulta; `Invalid` (400, con su campo) o `TooManyLines`,
    siempre sin escribir nada."""
    require_person(ctx)
    alias = require_scope(ctx)
    if operation not in OPERATIONS:
        raise Invalid("operation", ", ".join(OPERATIONS))
    if rounding not in Rounding.__members__:
        raise Invalid("rounding", ", ".join(Rounding))
    number, rule = _value(operation, value), Rounding(rounding)
    try:
        why = change_reason(reason)
    except ValueError as error:
        raise Invalid("reason", str(error)) from None
    now = timezone.now()
    if effective_at is not None and (effective_at.tzinfo is None or effective_at <= now):
        raise Invalid("effective_at", "con zona horaria y en el futuro; sin ella, al aplicar")
    lists = visible_price_lists(execution_context(ctx)).using(alias)
    price_list: PriceList | None = lists.filter(pk=price_list_id, is_active=True).first()
    if price_list is None:
        raise Invalid("price_list_id", "no existe")
    rows, kept = _selection(alias, filters)
    found = list(rows.order_by("sku", "id")[: LINES_MAX + 1])
    if len(found) > LINES_MAX:
        raise TooManyLines(rows.count())
    if not found:
        raise Invalid("filters", "ninguna variante viva coincide")
    at = effective_at or now
    ruling = {
        p.variant_id: p for p in prices_at([v.pk for v in found], price_list.pk, at).using(alias)
    }
    params = {"operation": operation, "value": amount_text(number), "rounding": rule.value}
    batch = PriceChangeBatch(
        organization_id=ctx.organization_id,
        kind=BatchKind.BULK_ADJUST,
        status=BatchStatus.PREVIEWED,
        price_list=price_list,
        params={**params, "filters": kept},
        reason=why,
        effective_at=effective_at,
        created_by_user_id=ctx.user_id,
    )
    items = []
    for variant in found:
        status, note, amounts = _line(operation, number, rule, ruling.get(variant.pk))
        items.append(
            PriceChangeBatchItem(
                organization_id=ctx.organization_id,
                batch=batch,
                variant_id=variant.pk,
                price_list_id=price_list.pk,
                status=status,
                error=note,
                **amounts,
            )
        )
    states = [item.status for item in items]
    batch.summary = {
        "lines": len(items),
        "changes": states.count(PENDING),
        "no_price": sum(item.error == NO_PRICE for item in items),
        "unchanged": sum(item.error == UNCHANGED for item in items),
        "errors": states.count(ERROR),
        "alerts": sum(map(alert, items)),
        "currency": price_list.currency,
    }
    with transaction.atomic(using=alias):  # savepoint: el lote con todas sus líneas o nada
        batch.save(using=alias)
        PriceChangeBatchItem.objects.using(alias).bulk_create(items)
        shown = {"effective_at": effective_at.isoformat() if effective_at else None}
        metadata = {"price_list_id": str(price_list.pk), **params, **shown, **batch.summary}
        entity = Entity("price_change_batch", batch.pk, price_list.code)
        record(ctx, "price.bulk_previewed", entity, metadata=metadata)
    return batch


def cancel_batch(ctx: TenantContext, batch_id: UUID) -> PriceChangeBatch:
    """`DRAFT` o `PREVIEWED` → `CANCELLED`, `version + 1` y `price.bulk_cancelled`; no cambia
    ningún precio. Uno ya cancelado se devuelve tal cual, sin auditar. `ActorRefused` antes de la
    primera consulta; `PriceChangeBatch.DoesNotExist` si el actor no ve su lista (activa o no);
    `BatchStateConflict` desde otro estado. La fila, bloqueada hasta el final de la transacción."""
    require_person(ctx)
    alias = require_scope(ctx)
    shown = visible_batches(execution_context(ctx)).using(alias).select_related("price_list")
    with transaction.atomic(using=alias):
        batch: PriceChangeBatch = shown.select_for_update(no_key=True, of=("self",)).get(
            pk=batch_id
        )
        if batch.status == BatchStatus.CANCELLED:
            return batch
        if batch.status not in (BatchStatus.DRAFT, BatchStatus.PREVIEWED):
            raise BatchStateConflict(batch.status)
        before, batch.status, batch.version = batch.status, BatchStatus.CANCELLED, batch.version + 1
        batch.save(using=alias, update_fields=["status", "version", "updated_at"])
        entity = Entity("price_change_batch", batch.pk, batch.price_list.code)
        record(ctx, "price.bulk_cancelled", entity, {"status": [before, batch.status]})
    return batch
