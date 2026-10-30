"""Los márgenes de una lista de precios (F3-46d; E03-07; ADR-006 §1 y §2, ADR-003 §4): la única
vista que junta precio y costo. Pide `product_cost.view`, `prices.view` y `products.view`, la lista
visible y activa (si no, 404) y un segundo factor reciente (`step_up_methods`).

El precio es lo que paga el cliente según `PricingService` (con la promoción que rige); el costo,
lo que dice `CostService`. ADR-006 no define un precio neto de IGV: el margen se calcula sobre los
importes tal como se guardan, y cada lado dice su moneda y su `tax_included`. Si no coinciden, o
falta uno, no hay margen y `reason` dice por qué (nunca una cuenta entre monedas). Una consulta de
cada fuente por página. La primera lectura de la sesión por lista deja `cost.viewed` (sin importes).
"""

from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID

from django.db.models import QuerySet
from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework import generics, serializers
from rest_framework.exceptions import NotFound
from rest_framework.request import Request
from rest_framework.response import Response

from apps.access.permissions import HasPermission, request_context
from apps.audit.services import Entity
from apps.catalog.models import ProductVariant
from apps.catalog.selectors import live_variants
from apps.pricing import costs, services
from apps.pricing.api.costs import viewed
from apps.pricing.api.prices import (
    Amount,
    PriceVariantSerializer,
    ProductFilterSerializer,
    _filter,
    _price_list,
)
from apps.pricing.api.views import _tenant
from core.api.schema import errors
from core.money import amount_text, round_amount

TENTH = Decimal("0.1")


class Why(StrEnum):
    """Por qué una fila no tiene margen."""

    VARIANT_UNAVAILABLE = "VARIANT_UNAVAILABLE"  # la variante no se vende
    NO_PRICE = "NO_PRICE"
    NO_COST = "NO_COST"
    CURRENCY_MISMATCH = "CURRENCY_MISMATCH"
    TAX_MISMATCH = "TAX_MISMATCH"  # uno con IGV y el otro sin


def margin(price: Decimal, cost: Decimal) -> dict[str, str]:
    """Importe y % sobre el precio, un decimal, `ROUND_HALF_UP`: 4399 y 3500 dan 899 y 20.4."""
    gain = round_amount(price - cost)
    percent = ((price - cost) * 100 / price).quantize(TENTH, rounding=ROUND_HALF_UP)
    return {"amount": amount_text(gain), "percent": f"{percent:f}"}


def row(variant: ProductVariant, sold: services.PriceResolution, cost: Any) -> dict[str, Any]:
    """La fila de una variante; `cost` es su costo vigente o `None`."""
    price = sold.final_amount if sold.found and sold.final_amount else None
    out: dict[str, Any] = {"variant": variant, "price": None, "cost": None, "margin": None}
    out["reason"] = None
    if price is not None:
        out["price"] = {
            "amount": price,
            "currency": sold.currency,
            "tax_included": sold.tax_included,
        }
    if cost is not None:
        out["cost"] = {
            "amount": cost.amount,
            "currency": cost.currency,
            "tax_included": cost.tax_included,
        }
    if price is None:
        out["reason"] = (
            Why.NO_PRICE if sold.reason is services.Reason.NO_PRICE else Why.VARIANT_UNAVAILABLE
        )
    elif cost is None:
        out["reason"] = Why.NO_COST
    elif sold.currency != cost.currency:
        out["reason"] = Why.CURRENCY_MISMATCH
    elif sold.tax_included != cost.tax_included:
        out["reason"] = Why.TAX_MISMATCH
    else:
        out["margin"] = margin(price, cost.amount)
    return out


class MarginSideSerializer(serializers.Serializer[Any]):
    amount = Amount()
    currency = serializers.CharField()
    tax_included = serializers.BooleanField(allow_null=True)


class MarginFigureSerializer(serializers.Serializer[Any]):
    amount = serializers.CharField(
        help_text="Precio menos costo; negativo si se vende con pérdida."
    )
    percent = serializers.CharField(help_text="Sobre el precio, con un decimal (`20.4`).")


class ListMarginSerializer(serializers.Serializer[Any]):
    variant = PriceVariantSerializer()
    price = MarginSideSerializer(
        allow_null=True, help_text="Lo que paga el cliente (con la promoción que rige)."
    )
    cost = MarginSideSerializer(allow_null=True, help_text="El costo vigente.")
    margin = MarginFigureSerializer(allow_null=True, help_text="`null` si `reason` no es `null`.")
    reason = serializers.ChoiceField(choices=[w.value for w in Why], allow_null=True)


@extend_schema_view(
    get=extend_schema(
        operation_id="price_list_margins_list",
        tags=["pricing"],
        parameters=[ProductFilterSerializer],
        responses={200: ListMarginSerializer(many=True), **errors(400, 401, 403, 404)},
        description="Las variantes vivas por creación (ADR-016), con el precio final, el costo "
        "vigente y el margen. Pide un segundo factor reciente: 403 `STEP_UP_REQUIRED`. La primera "
        "lectura de la sesión deja `cost.viewed`. 404: lista oculta o inactiva.",
    )
)
class MarginsView(generics.ListAPIView):
    permission_classes = [HasPermission]
    required_permissions = {"GET": ("product_cost.view", "prices.view", "products.view")}
    step_up_methods = {"GET"}
    serializer_class = ListMarginSerializer

    def get_queryset(self) -> QuerySet[ProductVariant]:
        return live_variants().filter(**_filter(self.request))

    def list(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        price_list = _price_list(request, kwargs["price_list_id"])
        page = self.paginate_queryset(self.filter_queryset(self.get_queryset())) or []
        ids, rows = [variant.pk for variant in page], []
        if ids:
            ectx = request_context(request)
            assert ectx is not None  # noqa: S101 — `HasPermission` ya lo comprobó
            try:
                sold = services.resolve_many(ectx, ids, price_list.pk)
            except services.ListNotVisible:  # se desactivó entre las dos lecturas
                raise NotFound from None
            ruling = costs.current_costs(_tenant(), ids)
            rows = [row(v, sold[v.pk], ruling.get(UUID(str(v.pk)))) for v in page]
        viewed(request, Entity("price_list", price_list.pk, price_list.name))
        return self.get_paginated_response(ListMarginSerializer(rows, many=True).data)
