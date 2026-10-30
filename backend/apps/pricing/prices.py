"""Fijar el precio de una variante en una lista (F3-42; E03-02, E03-03; ADR-006 §3, 04 §K.4).

No comprueba permisos: los declarará la API (`prices.manage`, F3-44); fuera de `apps.pricing`
nadie lo importa (contrato de import-linter). Sin precios retroactivos: rige desde el instante en
que se obtiene el bloqueo o desde uno futuro (programado). Cierra el precio que rige en ese
instante e inserta el nuevo; si ya hay uno programado después, el nuevo termina donde empieza ese.

Concurrencia: los cambios de un mismo par variante × lista van de uno en uno (bloqueo consultivo
de la transacción, lo primero que toma) y "ahora" se lee después (`clock_timestamp()`): un cambio
que esperó el bloqueo rige después del que lo tenía, nunca antes ni encima de él. Después, el
precio que rige `FOR NO KEY UPDATE`. La
variante y la lista no se bloquean: un precio que entra a la vez que se borra su variante o se
desactiva su lista no lo ofrece el motor (F3-43). La `EXCLUDE` es la última barrera: un solape que
llegue a la base es `PriceConflict` (409), nunca un 500.
"""

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from django.db import IntegrityError, connections, transaction
from django.db.models import Q

from apps.audit.services import Entity, record
from apps.catalog.selectors import live_variants
from apps.pricing.models import OVERLAP, PriceList, ProductPrice
from apps.pricing.text import change_reason
from core.money import amount_text, parse_amount
from core.outbox import emit
from core.tenancy.context import ActorType, TenantContext
from core.tenancy.scope import require_scope


class RetroactivePrice(ValueError):
    """`starts_at` ya pasó: un precio rige desde ahora o desde después (400)."""


class PriceAlreadyScheduled(Exception):
    """Ya hay un precio que empieza justo en ese `starts_at` (409 `PRICE_ALREADY_SCHEDULED`)."""


class PriceUnchanged(Exception):
    """Es el importe que ya rige en `starts_at`: no se escribe nada (409 `PRICE_UNCHANGED`)."""


class PriceConflict(Exception):
    """La `EXCLUDE` rechazó un solape: otro cambio del par llegó antes (409 `PRICE_CONFLICT`)."""


def _lock(alias: str, ctx: TenantContext, variant_id: UUID, price_list_id: UUID) -> None:
    """Un cambio a la vez por par variante × lista, hasta el fin de la transacción."""
    with connections[alias].cursor() as cursor:
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            [f"product_prices:{ctx.organization_id}:{variant_id}:{price_list_id}"],
        )


def _now(alias: str) -> datetime:
    """El instante real, no el inicio de la transacción: se lee con el bloqueo ya tomado."""
    with connections[alias].cursor() as cursor:
        cursor.execute("SELECT clock_timestamp()")
        now: datetime = cursor.fetchone()[0]
    return now


def set_price(
    ctx: TenantContext,
    *,
    variant_id: UUID,
    price_list_id: UUID,
    amount: str | Decimal,
    reason: str,
    starts_at: datetime | None = None,
) -> ProductPrice:
    """Fija el precio desde `starts_at` (sin él, desde ahora), lo audita (`price.changed`, o
    `price.scheduled` si empieza después) y deja `price.changed` en el outbox. `ValueError` por
    el importe o el motivo, `RetroactivePrice`, `ProductVariant.DoesNotExist` o
    `PriceList.DoesNotExist` (borrada o inactiva), `PriceAlreadyScheduled`, `PriceUnchanged` o
    `PriceConflict`, siempre sin escribir nada."""
    alias, value, why = require_scope(ctx), parse_amount(amount), change_reason(reason)
    if value <= 0:
        raise ValueError("mayor que cero")
    if starts_at is not None and starts_at.tzinfo is None:
        raise ValueError("starts_at con zona horaria")
    try:
        with transaction.atomic(using=alias):  # savepoint: todo o nada
            _lock(alias, ctx, variant_id, price_list_id)
            now = _now(alias)  # después del bloqueo: los cambios se aplican en su orden
            if starts_at is not None and starts_at < now:
                raise RetroactivePrice("starts_at")
            start = starts_at or now
            variant = live_variants().using(alias).get(pk=variant_id)
            lists = PriceList.objects.using(alias).filter(deleted_at__isnull=True, is_active=True)
            price_list: PriceList = lists.get(pk=price_list_id)
            pair = ProductPrice.objects.using(alias).filter(
                variant_id=variant_id, price_list_id=price_list_id
            )
            current: ProductPrice | None = (
                pair.filter(Q(ends_at__isnull=True) | Q(ends_at__gt=start), starts_at__lte=start)
                .select_for_update(no_key=True)
                .first()
            )
            if current is not None and current.starts_at == start:
                raise PriceAlreadyScheduled(start)
            if current is not None and current.amount == value:
                raise PriceUnchanged(value)
            following = pair.filter(starts_at__gt=start).order_by("starts_at").first()
            if current is not None:
                pair.filter(pk=current.pk).update(ends_at=start)
            user = ctx.user_id if ctx.actor_type == ActorType.USER else None
            price: ProductPrice = ProductPrice.objects.using(alias).create(
                variant_id=variant_id,
                price_list_id=price_list_id,
                amount=value,
                currency=price_list.currency,
                tax_included=True,  # D-PRC-1: IGV incluido en esta serie
                starts_at=start,
                ends_at=following.starts_at if following else None,
                change_reason=why,
                previous_price=current,
                created_by_user_id=user,
            )
            before = amount_text(current.amount) if current else None
            entity = Entity("product_price", price.pk, f"{variant.sku} @ {price_list.code}")
            metadata = {
                "variant_id": str(variant_id),
                "price_list_id": str(price_list_id),
                "currency": price.currency,
                "starts_at": start.isoformat(),
                "ends_at": price.ends_at.isoformat() if price.ends_at else None,
                "reason": why,
            }
            action = "price.scheduled" if start > now else "price.changed"
            record(ctx, action, entity, {"amount": [before, amount_text(value)]}, metadata)
            payload = {k: metadata[k] for k in ("variant_id", "price_list_id", "starts_at")}
            emit(
                ctx,
                "price.changed",
                aggregate_type="product_price",
                aggregate_id=price.pk,
                payload=payload,
            )
    except IntegrityError as error:
        if OVERLAP in str(error):
            raise PriceConflict(variant_id) from None
        raise
    return price
