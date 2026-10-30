"""CostService: el costo de una variante (F3-46; E03-07; ADR-006 §1 y §2, ADR-003 §4).

Es el único código que lee o escribe `variant_costs`. No comprueba permisos: los declararán las
rutas (`product_cost.view` y `product_cost.manage`, con step-up, F3-46c), la única pieza que lo
importa (contrato de import-linter). El motor de precios (`services`, `selectors`) no lo importa:
`PricingService` no lee costos. Nada del costo va a un log ni al outbox; la auditoría
(`cost.changed`) es el único registro y lleva el importe anterior y el nuevo.

`set_cost` sigue a `set_price`: bloqueo consultivo por variante (lo primero), "ahora" después
(`clock_timestamp()`), cierra el costo que rige e inserta el nuevo desde ese instante. No hay costos
retroactivos ni programados, ni un cambio que deje el mismo importe. La `EXCLUDE` es la última
barrera: un solape que llegue a la base es `CostConflict`, nunca un 500.
"""

import re
from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from django.db import IntegrityError, connections, transaction
from django.db.models import DateTimeField, Func, Q, QuerySet

from apps.audit.services import Entity, record
from apps.catalog.selectors import live_variants
from apps.pricing.models import COST_OVERLAP, CostSource, VariantCost
from core.money import amount_text, parse_amount
from core.money import currency as check_currency
from core.tenancy.context import ActorType, TenantContext
from core.tenancy.scope import require_scope
from core.text import line

REASON_MIN, REASON_MAX = 3, 500
CLOCK = Func(function="clock_timestamp", output_field=DateTimeField())  # el instante real
TAG = re.compile(r"<[A-Za-z/!?]")  # el comienzo de una etiqueta o un comentario HTML


class CostUnchanged(Exception):
    """Es el costo que ya rige: no se escribe nada (409 `COST_UNCHANGED`)."""


class CostConflict(Exception):
    """La `EXCLUDE` rechazó un solape: otro cambio de la variante llegó antes (409)."""


def change_reason(value: str) -> str:
    """El motivo como se guarda: una línea de 3 a 500 caracteres, sin etiquetas HTML."""
    text = line(value, REASON_MAX)
    if len(text) < REASON_MIN or TAG.search(text):
        raise ValueError(f"de {REASON_MIN} a {REASON_MAX} caracteres, sin HTML")
    return text


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
    `CostUnchanged` o `CostConflict`, siempre sin escribir nada."""
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
                cursor.execute("SELECT clock_timestamp()")  # con el bloqueo ya tomado
                now: datetime = cursor.fetchone()[0]
            variant = live_variants().using(alias).get(pk=variant_id)
            costs = VariantCost.objects.using(alias).filter(variant_id=variant_id)
            current: VariantCost | None = (
                costs.filter(Q(ends_at__isnull=True) | Q(ends_at__gt=now), starts_at__lte=now)
                .select_for_update(no_key=True)
                .first()
            )
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
            raise CostConflict(variant_id) from None
        raise
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
