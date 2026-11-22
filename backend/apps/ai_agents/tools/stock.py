"""`get_stock` (F8-07d; ADR-034 §6, §13 D-F8-07): la disponibilidad de una variante publicada, en
franja, con `inventory.view` de la persona. Suma `available` (lo que hay menos lo reservado) de los
almacenes vivos, activos y `is_sellable` (decisión del orquestador en #770); un producto que no
lleva stock es `NOT_TRACKED`. El modelo solo ve la franja; el número exacto va en la evidencia
(`facts`), para el vendedor. Nunca almacenes, ubicaciones, `on_hand`, reservas ni costos."""

from datetime import datetime
from typing import Any

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db.models import Sum

from apps.access.selectors import ExecutionContext
from apps.ai_agents.fields import Uuid
from apps.ai_agents.registry import Audience, Found, NotFound, Risk, ToolSpec, register
from apps.ai_agents.tools.catalog import published_variants
from apps.inventory.selectors import levels, warehouses

AVAILABLE, FEW_UNITS, OUT_OF_STOCK = "AVAILABLE", "FEW_UNITS", "OUT_OF_STOCK"
NOT_TRACKED = "NOT_TRACKED"


def few_units() -> int:
    """El tope de «pocas unidades» del servidor; `ImproperlyConfigured` si no es un entero >= 1."""
    few = settings.AI_STOCK_FEW_UNITS
    if type(few) is not int or few < 1:
        raise ImproperlyConfigured("AI_STOCK_FEW_UNITS")
    return few


def band(units: int | None, few: int) -> str:
    """La franja de `units` disponibles; `None` es un producto sin stock."""
    if units is None:
        return NOT_TRACKED
    if units <= 0:
        return OUT_OF_STOCK
    return FEW_UNITS if units <= few else AVAILABLE


def _stock(ectx: ExecutionContext, args: dict[str, Any], as_of: datetime) -> Found:
    few = few_units()
    variant = published_variants().filter(pk=args["variant_id"]).first()
    if variant is None:
        raise NotFound
    units = None
    if variant.product.tracks_inventory:
        sellable = warehouses().filter(is_active=True, is_sellable=True)
        found = levels().filter(variant_id=variant.pk, warehouse__in=sellable)
        units = found.aggregate(total=Sum("available"))["total"] or 0
    data = {"variant_id": str(variant.pk), "availability": band(units, few)}
    return Found(data, (variant.product_id, variant.pk), {"available": units})


GET_STOCK = register(ToolSpec(
    name="get_stock", permission="inventory.view", handler=_stock, precheck=few_units,
    description="La disponibilidad para la venta de una variante publicada (de "
    "`get_product_variants`): AVAILABLE, FEW_UNITS (pocas unidades), OUT_OF_STOCK (agotado) o "
    "NOT_TRACKED (no lleva stock). Nunca da el número de unidades: no lo inventes ni lo prometas.",
    fields={"variant_id": Uuid("El `variant_id` que devolvió `get_product_variants`")},
    output={"variant_id": str, "availability": str}, risk=Risk.READ,
    audience=Audience.PUBLIC_SAFE, kind="STOCK",
))  # fmt: skip
