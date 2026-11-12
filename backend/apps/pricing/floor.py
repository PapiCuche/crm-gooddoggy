"""El suelo de costo de un precio escrito a mano (F5-09b2; ADR-006 §1): ¿el precio cubre el costo?
Lo único que sale de aquí es un sí o un no: ni el costo ni la diferencia llegan a quien pregunta, a
un log ni a un error. Falla cerrado: sin costo vigente, o en otra moneda, el precio no se admite.
Es, con `costs`, el único código que lee costos (contrato de import-linter)."""

from collections.abc import Mapping
from decimal import Decimal
from uuid import UUID

from apps.pricing import costs
from core.tenancy.context import TenantContext


def covers_cost(
    ctx: TenantContext, wanted: Mapping[UUID, Decimal], currency: str, tax_rate: Decimal
) -> bool:
    """Todos los precios `wanted` ({variante: precio con IGV incluido}) llegan al costo que rige
    hoy **y a todo costo programado a futuro** (el mayor manda), puesto también con IGV (el costo
    sin IGV se sube por `tax_rate`). `False` si alguno queda por debajo o no hay con qué
    compararlo."""
    ruling, later = costs.current_costs(ctx, wanted), costs.scheduled_costs(ctx, wanted)
    for variant, price in wanted.items():
        found = ruling.get(variant)
        if found is None:
            return False
        for cost in (found, *later.get(variant, [])):
            if cost.currency != currency:
                return False
            if price < (cost.amount if cost.tax_included else cost.amount * (1 + tax_rate)):
                return False
    return True
