"""El costo de una variante (F3-46c; E03-07; ADR-006 §1 y §2, ADR-003 §4): `…/pricing/variants/
{id}/costs/`, la única ruta que muestra un costo. Leer pide `product_cost.view` y `products.view`;
fijar, además `product_cost.manage`. Las dos piden un segundo factor de hace 15 minutos como mucho
(`step_up_methods`). Una variante inexistente, ajena o borrada da el 404 de lo que no existe.
Estricto: un campo desconocido o un tipo distinto es 400.

La primera lectura de la sesión por variante deja `cost.viewed` (sin importes): la lista vive en
la sesión del servidor, acotada; pasado el tope se audita cada lectura.
"""

from typing import Any
from uuid import UUID

from django.core.exceptions import ObjectDoesNotExist
from django.db.models import QuerySet
from drf_spectacular.utils import extend_schema, extend_schema_serializer, extend_schema_view
from rest_framework import generics, serializers
from rest_framework.exceptions import NotFound
from rest_framework.request import Request
from rest_framework.response import Response

from apps.access.permissions import HasPermission
from apps.audit.services import Entity, record
from apps.catalog.models import ProductVariant
from apps.catalog.selectors import live_variants
from apps.pricing import costs
from apps.pricing.api.prices import AUTHOR, Amount
from apps.pricing.api.promotions import Closed, Text
from apps.pricing.api.views import _tenant
from apps.pricing.models import CostSource
from apps.pricing.text import REASON_MAX, REASON_MIN, change_reason
from core.api.errors import ApiError
from core.api.schema import errors

UNCHANGED = "Es el costo que ya rige: no se cambió nada."
CONFLICT = "Otro cambio de costo de esta variante llegó antes, o hay uno con fecha futura."
TOO_SOON = "El costo vigente empezó hace un instante: reintenta."
REJECTED = "La base de datos rechazó el costo."
SEEN = "cost_viewed"  # en la sesión: las variantes cuyo costo ya se auditó como visto
SEEN_MAX = 200


class CostSerializer(serializers.Serializer[Any]):
    id = serializers.UUIDField()
    amount = Amount(help_text="IGV incluido.")
    currency = serializers.CharField()
    tax_included = serializers.BooleanField()
    source = serializers.ChoiceField(choices=CostSource.choices)
    change_reason = serializers.CharField()
    starts_at = serializers.DateTimeField()
    ends_at = serializers.DateTimeField(allow_null=True, help_text="`null`: es el que rige.")
    created_by_user_id = serializers.UUIDField(allow_null=True, help_text=AUTHOR)


@extend_schema_serializer(many=False)  # ya es la página: spectacular no la envuelve otra vez
class CostPageSerializer(serializers.Serializer[Any]):
    current = CostSerializer(allow_null=True, help_text="El que rige ahora; `null` si no hay.")
    results = CostSerializer(many=True, help_text="El historial, del más nuevo al más antiguo.")
    next = serializers.CharField(
        allow_null=True, help_text="Cursor de la página siguiente; `null` en la última."
    )


class CostSetSerializer(Closed, serializers.Serializer[Any]):
    amount = Amount(help_text="IGV incluido. Mayor que cero.")
    reason = Text(max_length=REASON_MAX, help_text="Sin HTML.")

    def validate_reason(self, value: str) -> str:
        try:
            return change_reason(value)
        except ValueError:  # el mensaje nunca repite el motivo
            reason = f"De {REASON_MIN} a {REASON_MAX} caracteres, sin HTML."
            raise serializers.ValidationError(reason) from None


def _variant(variant_id: UUID) -> ProductVariant:
    found: ProductVariant | None = live_variants().filter(pk=variant_id).first()
    if found is None:
        raise NotFound
    return found


def viewed(request: Request, entity: Entity) -> None:
    """Audita `cost.viewed` la primera vez que esta sesión lee costos de esa entidad (una variante,
    o la lista de precios de los márgenes). Sin importes."""
    seen = request.session.get(SEEN)
    seen = [s for s in seen if isinstance(s, str)] if isinstance(seen, list) else []
    key = str(entity.id)
    if key in seen:
        return
    record(_tenant(), "cost.viewed", entity)
    if len(seen) < SEEN_MAX:
        request.session[SEEN] = [*seen, key]


@extend_schema_view(
    get=extend_schema(
        operation_id="cost_history_list",
        tags=["pricing"],
        responses={200: CostPageSerializer, **errors(400, 401, 403, 404)},
        description="El costo vigente y su historial, del más nuevo al más antiguo, con su "
        "motivo y su autor. Pide un segundo factor reciente: 403 `STEP_UP_REQUIRED`. La primera "
        "lectura de la sesión deja `cost.viewed` en la auditoría. 404: la variante no existe, "
        "es de otra organización o está borrada.",
    ),
    post=extend_schema(
        operation_id="cost_create",
        tags=["pricing"],
        request=CostSetSerializer,
        responses={201: CostSerializer, **errors(400, 401, 403, 404, 409)},
        description="Fija el costo de la variante desde ahora. 409 `COST_UNCHANGED`, "
        "`COST_CONFLICT` o `COST_TOO_SOON`; 400 `COST_REJECTED`.",
    ),
)
class CostsView(generics.ListAPIView):
    permission_classes = [HasPermission]
    required_permissions = {
        "GET": ("product_cost.view", "products.view"),
        "POST": ("product_cost.manage", "product_cost.view", "products.view"),
    }
    step_up_methods = {"GET", "POST"}
    serializer_class = CostSerializer
    ordering = "-id"  # UUIDv7: el orden en que se fijaron

    def get_queryset(self) -> QuerySet[Any]:
        return costs.cost_history(_tenant(), self.kwargs["variant_id"])

    def list(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        variant = _variant(kwargs["variant_id"])
        page = self.paginate_queryset(self.filter_queryset(self.get_queryset())) or []
        ruling = costs.current_costs(_tenant(), [variant.pk]).get(variant.pk)
        viewed(request, Entity("variant", variant.pk, variant.sku))
        body = self.get_paginated_response(CostSerializer(page, many=True).data).data
        return Response({"current": CostSerializer(ruling).data if ruling else None, **body})

    def post(self, request: Request, variant_id: UUID, **kwargs: Any) -> Response:
        wanted = CostSetSerializer(data=request.data)
        wanted.is_valid(raise_exception=True)
        try:
            cost = costs.set_cost(
                _tenant(),
                variant_id=variant_id,
                amount=wanted.validated_data["amount"],
                reason=wanted.validated_data["reason"],
            )
        except ObjectDoesNotExist:  # la variante no existe o ya no está viva
            raise NotFound from None
        except costs.CostUnchanged:
            raise ApiError("COST_UNCHANGED", 409, UNCHANGED) from None
        except costs.CostConflict:
            raise ApiError("COST_CONFLICT", 409, CONFLICT) from None
        except costs.CostTooSoon:
            raise ApiError("COST_TOO_SOON", 409, TOO_SOON) from None
        except costs.CostRejected:
            raise ApiError("COST_REJECTED", 400, REJECTED) from None
        return Response(CostSerializer(cost).data, status=201)
