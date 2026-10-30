"""`PricingService` (F3-43; E03-06, E03-10; ADR-006 §2): la única autoridad de precios.

Dada una variante, una lista y un instante, dice el precio que rige y hasta cuándo, o que no hay
precio. Nunca lo estima. Solo lee: no importa `prices` ni `price_lists`, y fuera de
`apps.pricing` nadie lee sus modelos (contratos de import-linter). No lee costos: son de otro
servicio (ADR-006 §1) y `PriceResolution` no tiene dónde ponerlos.

Los pasos de ADR-006 §2 que ya tienen datos, en este orden: (1) la organización del
`tenant_scope`; (3) la lista, la pedida o la predeterminada de la moneda, activa y que la
audiencia ve, si no `ListNotVisible`; (2) la variante viva y `ACTIVE`, con su producto `ACTIVE`
(y `ai_visible` en los dos para la IA pública), si no `VARIANT_UNAVAILABLE`; (4) el precio que
rige en `[starts_at, ends_at)`, si no `NO_PRICE`; (5 a 7) la promoción que
rige (`discounts`). La lista va antes que la variante: quien no ve la lista no aprende nada de
la variante.

Consultas: la lista, las variantes, sus precios y las promociones (y `now()` si no llega `at`);
con alguna promoción, también sus objetivos y las categorías: sea cual sea el número de variantes.
"""

from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from django.db import connections
from django.db.models import QuerySet

from apps.access.selectors import ExecutionContext
from apps.catalog.selectors import categories, live_variants
from apps.pricing import discounts
from apps.pricing.models import PriceList, ProductPrice
from apps.pricing.selectors import (
    prices_at,
    promotion_items,
    promotions_from,
    visible_price_lists,
)
from core.tenancy.context import TenantContext
from core.tenancy.scope import require_scope

CURRENCY = "PEN"  # D-PRC-2: la moneda de la organización hasta que haya multimoneda
CHANNEL_MAX = 30  # el ancho de `promotions.channels`: PostgreSQL trunca en silencio lo que pase
ACTIVE = "ACTIVE"  # el de la variante y el del producto


def check_channel(value: object) -> str:
    """Un canal como se guarda y se consulta: texto de 1 a `CHANNEL_MAX` caracteres, sin espacios
    exteriores ni NUL (PostgreSQL lo rechaza con un error que aborta la transacción)."""
    if not isinstance(value, str) or not value or value != value.strip() or "\x00" in value:
        raise ValueError("channel: texto sin espacios exteriores ni NUL")
    if len(value) > CHANNEL_MAX:
        raise ValueError(f"channel de como mucho {CHANNEL_MAX} caracteres")
    return value


class Audience(StrEnum):
    HUMAN = "HUMAN"  # una persona: la lista por su `required_permission` o `prices.manage`
    PUBLIC_AI = "PUBLIC_AI"  # un agente de IA de cara al cliente (Fase 8): `ai_exposable`


class Reason(StrEnum):
    VARIANT_UNAVAILABLE = "VARIANT_UNAVAILABLE"
    NO_PRICE = "NO_PRICE"


class ListNotVisible(LookupError):
    """No hay lista: no existe, es de otra organización, está inactiva o borrada, la audiencia
    no la ve o la moneda no tiene predeterminada. La API lo traduce a 404."""


@dataclass(frozen=True, slots=True)
class PriceQuery:
    variant_id: UUID
    price_list_id: UUID | None = None  # sin ella: la predeterminada de la moneda
    at: datetime | None = None  # sin él: el `now()` de la transacción; con zona horaria
    audience: Audience = Audience.HUMAN
    channel: str | None = None  # sin él: solo las promociones de todos los canales


@dataclass(frozen=True, slots=True)
class PromotionApplied:
    """La promoción que rige, lo que se cita al cliente y a la IA. Nada más de ella."""

    id: UUID
    name: str
    terms: str
    ends_at: datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class PriceResolution:
    """La respuesta, y la evidencia que citan cotizaciones e IA (ADR-005). Sin campos de costo,
    nunca: un test fija la lista. Sin precio (`found=False`), sin importes."""

    found: bool
    reason: Reason | None
    variant_id: UUID
    price_list_id: UUID
    currency: str
    tax_included: bool | None = None
    base_amount: Decimal | None = None
    promotion: PromotionApplied | None = None
    final_amount: Decimal | None = None
    savings: Decimal | None = None
    valid_until: datetime | None = None  # el fin del precio; NULL: sin fin conocido
    price_id: UUID | None = None
    promotion_id: UUID | None = None
    resolved_at: datetime
    evidence_kind: Literal["PRICE"] = "PRICE"


def _instant(alias: str, at: datetime | None) -> datetime:
    if at is None:
        with connections[alias].cursor() as cursor:
            cursor.execute("SELECT now()")
            now: datetime = cursor.fetchone()[0]
        return now
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("at con zona horaria")
    return at


def _lists(ctx: ExecutionContext | TenantContext, audience: Audience) -> QuerySet[PriceList]:
    if audience is Audience.PUBLIC_AI:
        return PriceList.objects.filter(deleted_at__isnull=True, ai_exposable=True).exclude(
            required_permission="prices.view_internal"  # 04 §K.2: la interna, nunca
        )
    if not isinstance(ctx, ExecutionContext):
        raise TypeError("una persona resuelve con su ExecutionContext")
    return visible_price_lists(ctx)


def _price_list(
    ctx: ExecutionContext | TenantContext, alias: str, audience: Audience, wanted: UUID | None
) -> PriceList:
    lists = _lists(ctx, audience).using(alias).filter(is_active=True)
    lists = lists.filter(pk=wanted) if wanted else lists.filter(currency=CURRENCY, is_default=True)
    found: PriceList | None = lists.first()
    if found is None:
        raise ListNotVisible(wanted)
    return found


def _sellable(variant: Any, audience: Audience) -> bool:
    product = variant.product
    if variant.status != ACTIVE or product.status != ACTIVE:
        return False
    return audience is Audience.HUMAN or (variant.ai_visible and product.ai_visible)


def _resolution(
    variant_id: UUID,
    price_list: PriceList,
    at: datetime,
    price: ProductPrice | None,
    why: Reason,
    discount: tuple[discounts.Applied | None, datetime | None] | None = None,
) -> PriceResolution:
    empty = PriceResolution(
        found=False,
        reason=why,
        variant_id=variant_id,
        price_list_id=price_list.pk,
        currency=price_list.currency,
        resolved_at=at,
    )
    if price is None:
        return empty
    applied, until = discount if discount else (None, price.ends_at)
    final = applied.final_amount if applied else price.amount
    promotion = applied.promotion if applied else None
    return replace(
        empty,
        found=True,
        reason=None,
        tax_included=price.tax_included,
        base_amount=price.amount,
        final_amount=final,
        savings=price.amount - final,
        valid_until=until,
        price_id=price.pk,
        promotion=(
            PromotionApplied(promotion.pk, promotion.name, promotion.terms, promotion.ends_at)
            if promotion
            else None
        ),
        promotion_id=promotion.pk if promotion else None,
    )


def _candidates(
    alias: str, price_list: PriceList, when: datetime, channel: str | None, audience: Audience
) -> list[discounts.Candidate]:
    rows = list(
        promotions_from(when, price_list, channel, ai_only=audience is Audience.PUBLIC_AI).using(
            alias
        )
    )
    items: dict[UUID, list[Any]] = {row.pk: [] for row in rows}
    for item in promotion_items(items).using(alias):
        items[item.promotion_id].append(item)
    return [discounts.Candidate(row, tuple(items[row.pk])) for row in rows]


def _targets(
    alias: str, found: Iterable[Any], candidates: list[discounts.Candidate]
) -> dict[UUID, discounts.Target]:
    """Lo que alcanza a cada variante; las categorías de los objetivos y las de los productos,
    en una consulta, y de ahí las antecesoras por `path`."""
    wanted = {i.category_id for c in candidates for i in c.items if i.category_id}
    variants = list(found)
    wanted |= {v.product.category_id for v in variants if v.product.category_id}
    paths = {c.pk: c.path for c in categories().using(alias).filter(pk__in=wanted)}
    targets = {}
    for variant in variants:
        own = paths.get(variant.product.category_id, "")
        near = {pk: len(path) for pk, path in paths.items() if own.startswith(path)}
        targets[variant.pk] = discounts.Target(
            variant.pk, variant.product_id, variant.product.brand_id, near
        )
    return targets


def resolve_many(
    ctx: ExecutionContext | TenantContext,
    variant_ids: Iterable[UUID],
    price_list_id: UUID | None,
    at: datetime | None = None,
    *,
    audience: Audience = Audience.HUMAN,
    channel: str | None = None,
) -> dict[UUID, PriceResolution]:
    """`resolve` para una página de variantes en la misma lista e instante, en el orden en que
    llegan (sin repetidas), en un canal si se dice. Las mismas consultas para una variante que
    para cien."""
    if channel is not None:
        check_channel(channel)
    audience = Audience(audience)  # "PUBLIC_AI" como texto también; otro valor, `ValueError`
    tenant = ctx.tenant if isinstance(ctx, ExecutionContext) else ctx
    alias = require_scope(tenant)
    when = _instant(alias, at)
    price_list = _price_list(ctx, alias, audience, price_list_id)
    wanted = list(dict.fromkeys(UUID(str(pk)) for pk in variant_ids))
    found = live_variants().using(alias).filter(pk__in=wanted)
    sellable = {variant.pk for variant in found if _sellable(variant, audience)}
    ruling = {p.variant_id: p for p in prices_at(sellable, price_list.pk, when).using(alias)}
    candidates = _candidates(alias, price_list, when, channel, audience) if ruling else []
    targets = (
        _targets(alias, [v for v in found if v.pk in ruling], candidates) if candidates else {}
    )

    def discount(pk: UUID) -> tuple[discounts.Applied | None, datetime | None] | None:
        if pk not in targets:
            return None
        base = ruling[pk].amount
        won = discounts.select(candidates, targets[pk], when, base)
        until = discounts.valid_until(candidates, targets[pk], when, base, won, ruling[pk].ends_at)
        return won, until

    return {
        pk: _resolution(
            pk,
            price_list,
            when,
            ruling.get(pk),
            Reason.NO_PRICE if pk in sellable else Reason.VARIANT_UNAVAILABLE,
            discount(pk),
        )
        for pk in wanted
    }


def resolve(ctx: ExecutionContext | TenantContext, query: PriceQuery) -> PriceResolution:
    """El precio de una variante en una lista en un instante (ADR-006 §2). Una persona pasa su
    `ExecutionContext`; la IA pública (`PUBLIC_AI`) basta con el `TenantContext`. `ValueError` si
    `at` no tiene zona horaria; `ListNotVisible` si no hay lista que ver."""
    page = resolve_many(
        ctx,
        [query.variant_id],
        query.price_list_id,
        query.at,
        audience=query.audience,
        channel=query.channel,
    )
    return page[UUID(str(query.variant_id))]
