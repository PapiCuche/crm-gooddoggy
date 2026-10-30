"""Comandos de promociones (F3-48c; E03-05; ADR-006 §2): crear en borrador, publicar y cancelar.

No comprueban permisos: los declara la API (`prices.manage`), la única que los importa (contrato
de import-linter), y solo este módulo escribe las tablas de promociones. Validan todo lo que
guardan; lo que el actor no ve (otra organización, borrado) es "no encontrado" (400 en su campo).

Ciclo: `DRAFT` → `PUBLISHED` → `CANCELLED`. Publicar solo sale de `DRAFT`; cancelar sale de `DRAFT`
o `PUBLISHED`; lo demás es `PromotionStateConflict` (409), también el segundo de dos a la vez: la
fila se bloquea (`FOR NO KEY UPDATE`) y quien espera ya ve el estado nuevo. Una publicada no cambia
sus términos, objetivos ni listas: lo impide la base (migración 0004), no solo este módulo. Quien
aplica la promoción es `PricingService`: rige entre `starts_at` y `ends_at` sin ningún job.
"""

import re
from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from django.db import transaction
from django.utils import timezone

from apps.audit.services import Entity, record
from apps.catalog.selectors import brands, categories, live_variants, products
from apps.pricing.models import (
    DiscountType,
    PriceList,
    Promotion,
    PromotionItem,
    PromotionPriceList,
    PromotionStatus,
)
from apps.pricing.services import check_channel
from core.money import amount_text, parse_amount
from core.money import currency as check_currency
from core.tenancy.context import TenantContext
from core.tenancy.scope import require_scope
from core.text import name as clean_name

TAG = re.compile(r"<[A-Za-z/!?]")  # el comienzo de una etiqueta o un comentario HTML
NAME_MAX, TEXT_MAX, PRIORITY_MAX = 200, 2000, 10_000
ITEMS_MAX, LISTS_MAX, CHANNELS_MAX = 100, 20, 10
TARGETS = {"variant_id": live_variants, "product_id": products, "category_id": categories}
TARGETS |= {"brand_id": brands}
DRAFT, PUBLISHED, CANCELLED = (
    PromotionStatus.DRAFT,
    PromotionStatus.PUBLISHED,
    PromotionStatus.CANCELLED,
)


class Invalid(ValueError):
    """Un dato que no sirve; `field` es el campo del cuerpo (400)."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field


class PromotionStateConflict(Exception):
    """El estado actual no admite el cambio (409); `status` es el que tiene."""

    def __init__(self, status: str) -> None:
        super().__init__(status)
        self.status = status


class PromotionEnded(Exception):
    """Ya pasó su `ends_at`: publicarla no serviría de nada (409 `PROMOTION_ENDED`)."""


def _text(field: str, value: str, limit: int) -> str:
    text = value.strip()
    if len(text) > limit or "\x00" in text or TAG.search(text):
        raise Invalid(field, f"hasta {limit} caracteres, sin HTML")
    return text


def _amount(field: str, kind: str, value: str | Decimal) -> Decimal:
    """Un valor de descuento: un importe mayor que cero y, si es porcentaje, hasta 100."""
    try:
        number = parse_amount(value)
    except ValueError, TypeError:
        raise Invalid(field, "hasta 12 cifras enteras y 2 decimales") from None
    if number <= 0 or (kind == DiscountType.PERCENT_OFF and number > 100):
        raise Invalid(field, "mayor que cero y, en un porcentaje, hasta 100")
    return number


def _channels(value: Iterable[str] | None) -> list[str] | None:
    if value is None:
        return None
    wanted = [item.strip() if isinstance(item, str) else item for item in value]
    try:
        checked = [check_channel(item) for item in wanted]
    except ValueError as error:
        raise Invalid("channels", str(error)) from None
    if not checked or len(checked) > CHANNELS_MAX or len(set(checked)) != len(checked):
        raise Invalid("channels", f"de 1 a {CHANNELS_MAX} canales distintos, o `null`: todos")
    return checked


def _lists(alias: str, ids: list[UUID], money: str) -> list[UUID]:
    if not ids or len(ids) > LISTS_MAX or len(set(ids)) != len(ids):
        raise Invalid("price_list_ids", f"de 1 a {LISTS_MAX} listas distintas")
    found = PriceList.objects.using(alias).filter(deleted_at__isnull=True, pk__in=ids)
    rows = {row.pk: row for row in found}
    if len(rows) != len(ids):
        raise Invalid("price_list_ids", "alguna lista no existe")
    if any(row.currency != money for row in rows.values()):
        raise Invalid("price_list_ids", "la moneda de cada lista debe ser la de la promoción")
    return ids


def _items(alias: str, kind: str, items: list[dict[str, Any]]) -> list[tuple[str, UUID, Any]]:
    if not items or len(items) > ITEMS_MAX:
        raise Invalid("items", f"de 1 a {ITEMS_MAX} objetivos")
    wanted: list[tuple[str, UUID, Any]] = []
    for item in items:
        targets = [key for key in TARGETS if item.get(key) is not None]
        if len(targets) != 1 or not set(item) <= {*TARGETS, "override_value"}:
            raise Invalid(
                "items",
                "cada objetivo lleva exactamente uno de variante, producto, "
                "categoría o marca, y a lo sumo `override_value`",
            )
        override = item.get("override_value")
        wanted.append(
            (
                targets[0],
                item[targets[0]],
                None if override is None else _amount("items", kind, override),
            )
        )
    if len({(key, pk) for key, pk, _ in wanted}) != len(wanted):
        raise Invalid("items", "objetivos repetidos")
    for key, source in TARGETS.items():
        ids = {pk for k, pk, _ in wanted if k == key}
        if ids and source().using(alias).filter(pk__in=ids).count() != len(ids):
            raise Invalid("items", "algún objetivo no existe")
    return wanted


def create_promotion(
    ctx: TenantContext,
    *,
    name: str,
    discount_type: str,
    value: str | Decimal,
    starts_at: datetime,
    ends_at: datetime,
    price_list_ids: list[UUID],
    items: list[dict[str, Any]],
    currency: str = "PEN",
    description: str = "",
    terms: str = "",
    priority: int = 0,
    ai_exposable: bool = False,
    channels: Iterable[str] | None = None,
) -> Promotion:
    """Crea la promoción en `DRAFT` con sus objetivos y listas, y la audita (`promotion.created`).
    `Invalid` (400, con su campo) por cualquier dato, sin escribir nada. Lo de otra organización
    o borrado no se distingue de lo que no existe."""
    alias = require_scope(ctx)
    if discount_type not in DiscountType.values:
        raise Invalid("discount_type", "FIXED_PRICE, PERCENT_OFF o AMOUNT_OFF")
    try:
        label, money = clean_name(name, NAME_MAX), check_currency(currency)
    except ValueError as error:
        raise Invalid("name" if "nombre" in str(error) else "currency", str(error)) from None
    if starts_at.tzinfo is None or ends_at.tzinfo is None or ends_at <= starts_at:
        raise Invalid("ends_at", "con zona horaria y después de `starts_at`")
    if abs(priority) > PRIORITY_MAX:
        raise Invalid("priority", f"entre -{PRIORITY_MAX} y {PRIORITY_MAX}")
    parsed = _amount("value", discount_type, value)
    fields = {
        "name": label,
        "description": _text("description", description, TEXT_MAX),
        "terms": _text("terms", terms, TEXT_MAX),
        "discount_type": discount_type,
        "value": parsed,
        "currency": money,
        "starts_at": starts_at,
        "ends_at": ends_at,
        "priority": priority,
        "ai_exposable": ai_exposable,
        "channels": _channels(channels),
    }
    with transaction.atomic(using=alias):  # savepoint: todo o nada
        listed, targets = _lists(alias, price_list_ids, money), _items(alias, discount_type, items)
        promotion: Promotion = Promotion.objects.using(alias).create(
            **fields, status=DRAFT, created_by_user_id=ctx.user_id, updated_by_user_id=ctx.user_id
        )
        org = ctx.organization_id
        PromotionItem.objects.using(alias).bulk_create(
            PromotionItem(
                organization_id=org, promotion=promotion, override_value=override, **{key: pk}
            )
            for key, pk, override in targets
        )
        PromotionPriceList.objects.using(alias).bulk_create(
            PromotionPriceList(organization_id=org, promotion=promotion, price_list_id=pk)
            for pk in listed
        )
        shown = {k: v.isoformat() if isinstance(v, datetime) else v for k, v in fields.items()}
        shown["value"] = amount_text(parsed)
        metadata = {
            "price_list_ids": [str(pk) for pk in listed],
            "targets": [f"{key}:{pk}" for key, pk, _ in targets],
        }
        entity = Entity("promotion", promotion.pk, label)
        record(ctx, "promotion.created", entity, {k: [None, v] for k, v in shown.items()}, metadata)
    return promotion


def _move(
    ctx: TenantContext, promotion_id: UUID, to: str, action: str, *, from_: set[str]
) -> Promotion:
    alias = require_scope(ctx)
    with transaction.atomic(using=alias):
        locked = Promotion.objects.using(alias).select_for_update(no_key=True)
        promotion: Promotion = locked.get(pk=promotion_id, deleted_at__isnull=True)
        if promotion.status not in from_:
            raise PromotionStateConflict(promotion.status)
        if to == PUBLISHED and promotion.ends_at <= timezone.now():
            raise PromotionEnded(promotion_id)
        before, promotion.status = promotion.status, to
        promotion.updated_by_user_id, promotion.version = ctx.user_id, promotion.version + 1
        promotion.save(
            using=alias, update_fields=["status", "version", "updated_by_user_id", "updated_at"]
        )
        entity = Entity("promotion", promotion.pk, promotion.name)
        record(ctx, action, entity, {"status": [before, to]})
    return promotion


def publish_promotion(ctx: TenantContext, *, promotion_id: UUID) -> Promotion:
    """`DRAFT` → `PUBLISHED` (`promotion.published`): desde ahí `PricingService` la aplica en su
    ventana. `Promotion.DoesNotExist`, `PromotionStateConflict` o `PromotionEnded`."""
    return _move(ctx, promotion_id, PUBLISHED, "promotion.published", from_={DRAFT})


def cancel_promotion(ctx: TenantContext, *, promotion_id: UUID) -> Promotion:
    """`DRAFT` o `PUBLISHED` → `CANCELLED` (`promotion.cancelled`): el motor deja de aplicarla al
    instante. `Promotion.DoesNotExist` o `PromotionStateConflict` (ya cancelada)."""
    return _move(ctx, promotion_id, CANCELLED, "promotion.cancelled", from_={DRAFT, PUBLISHED})
