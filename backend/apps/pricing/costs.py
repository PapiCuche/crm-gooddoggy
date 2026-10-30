"""CostService: el costo de una variante (F3-46; E03-07; ADR-006 §1 y §2, ADR-003 §4).

Es el único código que lee o escribe `variant_costs`. No comprueba permisos: los declararán las
rutas (`product_cost.view` y `product_cost.manage`, con step-up, F3-46c), la única pieza que lo
importa (contrato de import-linter). El motor de precios (`services`, `selectors`) no lo importa:
`PricingService` no lee costos. Nada del costo va a un log ni al outbox; la auditoría
(`cost.changed`) es el único registro y lleva el importe anterior y el nuevo.

`set_cost` sigue a `set_price`: bloqueo consultivo por variante (lo primero), "ahora" después
(`clock_timestamp()`), cierra el costo que rige e inserta el nuevo desde ese instante. No hay
costos retroactivos ni programados, ni un cambio que deje el mismo importe. Una fila con fecha
futura (reservada a IMPORT y PURCHASE) bloquea `set_cost` con `CostConflict` hasta que empiece. La
`EXCLUDE` es la última barrera: un solape que llegue a la base es `CostConflict`, nunca un 500.
"""

from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from django.db import IntegrityError, connections, transaction
from django.db.models import DateTimeField, Func, Q, QuerySet

from apps.audit.services import Entity, record
from apps.catalog.selectors import live_variants
from apps.pricing.models import COST_OVERLAP, CostSource, VariantCost
from apps.pricing.text import change_reason
from core.money import amount_text, parse_amount
from core.money import currency as check_currency
from core.tenancy.context import ActorType, TenantContext
from core.tenancy.scope import require_scope

CLOCK = Func(function="clock_timestamp", output_field=DateTimeField())  # el instante real


class CostUnchanged(Exception):
    """Es el costo que ya rige: no se escribe nada (409 `COST_UNCHANGED`)."""


class CostConflict(Exception):
    """La `EXCLUDE` rechazó un solape (409): otro cambio de la variante llegó antes, o hay un costo
    con fecha futura (reservado a IMPORT y PURCHASE), que bloquea `set_cost` hasta que empiece."""

    def __init__(self) -> None:
        super().__init__("variant_costs: la vigencia se solapa con otro costo de la variante")


class CostTooSoon(Exception):
    """El costo vigente empezó en este mismo instante: se reintenta (409)."""


class CostRejected(Exception):
    """La base rechazó la fila. Sin su mensaje: lleva el importe y el motivo (ADR-006 §1)."""

    def __init__(self) -> None:
        super().__init__("variant_costs: la base rechazó el costo")


def _now(alias: str) -> datetime:
    """El instante real, no el inicio de la transacción: se lee con el bloqueo ya tomado."""
    with connections[alias].cursor() as cursor:
        cursor.execute("SELECT clock_timestamp()")
        now: datetime = cursor.fetchone()[0]
    return now


def set_cost(
    ctx: TenantContext,
    *,
    variant_id: UUID,
    amount: str | Decimal,
    reason: str,
    currency: str = "PEN",
) -> VariantCost:
    """Fija el costo desde ahora (IGV incluido, origen MANUAL) y lo audita (`cost.changed`).
    `ValueError` por el importe, la moneda o el motivo, `ProductVariant.DoesNotExist` (borrada),
    `CostUnchanged`, `CostTooSoon`, `CostConflict` o `CostRejected`, siempre sin escribir nada."""
    alias, value, why = require_scope(ctx), parse_amount(amount), change_reason(reason)
    money = check_currency(currency)
    if value <= 0:
        raise ValueError("mayor que cero")
    try:
        with transaction.atomic(using=alias):  # savepoint: todo o nada
            with connections[alias].cursor() as cursor:  # un cambio a la vez por variante
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    [f"variant_costs:{ctx.organization_id}:{variant_id}"],
                )
            now = _now(alias)  # con el bloqueo ya tomado
            variant = live_variants().using(alias).get(pk=variant_id)
            costs = VariantCost.objects.using(alias).filter(variant_id=variant_id)
            current: VariantCost | None = (
                costs.filter(Q(ends_at__isnull=True) | Q(ends_at__gt=now), starts_at__lte=now)
                .select_for_update(no_key=True)
                .first()
            )
            if current is not None and current.starts_at >= now:
                raise CostTooSoon(variant_id)
            if current is not None and (current.amount, current.currency) == (value, money):
                raise CostUnchanged(variant_id)
            if current is not None:
                costs.filter(pk=current.pk).update(ends_at=now)
            user = ctx.user_id if ctx.actor_type == ActorType.USER else None
            cost: VariantCost = VariantCost.objects.using(alias).create(
                variant_id=variant_id,
                amount=value,
                currency=money,
                tax_included=True,
                starts_at=now,
                source=CostSource.MANUAL,
                change_reason=why,
                created_by_user_id=user,
            )
            before = amount_text(current.amount) if current else None
            record(
                ctx,
                "cost.changed",
                Entity("variant_cost", cost.pk, variant.sku),
                {"amount": [before, amount_text(value)]},
                {
                    "variant_id": str(variant_id),
                    "currency": money,
                    "starts_at": now.isoformat(),
                    "reason": why,
                },
            )
    except IntegrityError as error:
        if COST_OVERLAP in str(error):
            raise CostConflict from None
        raise CostRejected from None  # el mensaje de un CHECK trae la fila entera
    return cost


def current_costs(ctx: TenantContext, variant_ids: Iterable[UUID]) -> dict[UUID, VariantCost]:
    """El costo que rige hoy de cada variante que lo tiene, en una sola consulta."""
    alias = require_scope(ctx)
    ruling = VariantCost.objects.using(alias).filter(
        Q(ends_at__isnull=True) | Q(ends_at__gt=CLOCK),
        variant_id__in=list(variant_ids),
        starts_at__lte=CLOCK,
    )
    return {UUID(str(cost.variant_id)): cost for cost in ruling}


def cost_history(ctx: TenantContext, variant_id: UUID) -> QuerySet[VariantCost]:
    """Todos los costos de la variante, el más reciente primero."""
    alias = require_scope(ctx)
    return VariantCost.objects.using(alias).filter(variant_id=variant_id).order_by("-starts_at")
