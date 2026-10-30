"""Los precios de una lista (F3-44; E03-02, E03-03, E03-06; ADR-006 §2 y §3). `GET` pide
`prices.view`, `products.view` y la lista visible para el actor y activa (si no, 404); `POST`
pide `prices.manage` y responde la fila creada sin resolverla otra vez: en la misma transacción
`at=None` no la vería (empieza en `clock_timestamp()`). Importes en string decimal; ningún campo
de costo ni de margen.
"""

from typing import Any
from uuid import UUID

from django.core.exceptions import ObjectDoesNotExist
from django.db.models import QuerySet
from django.utils import timezone
from drf_spectacular.utils import extend_schema, extend_schema_field, extend_schema_view
from rest_framework import generics, serializers
from rest_framework.exceptions import NotFound
from rest_framework.request import Request
from rest_framework.response import Response

from apps.access.permissions import request_context
from apps.catalog.models import ProductVariant
from apps.catalog.selectors import live_variants
from apps.pricing import prices as commands
from apps.pricing import services
from apps.pricing.api.views import _tenant, _visible
from apps.pricing.models import PriceList, ProductPrice
from apps.pricing.selectors import next_prices, price_history, prices_at
from apps.pricing.text import REASON_MAX, REASON_MIN, change_reason
from core.api.errors import ApiError
from core.api.schema import errors
from core.money import AMOUNT, amount_text, parse_amount

UNCHANGED = "Es el precio que ya rige en ese momento: no se cambió nada."
SCHEDULED = "Ya hay un precio que empieza en ese instante: elige otro."
CONFLICT = "Otro cambio de precio de esta variante en esta lista llegó antes: recarga y reintenta."
PAST = "No puede ser en el pasado: sin él, rige desde ahora."
AUTHOR = "Quién lo fijó; `null` si no fue una persona. El nombre lo resuelve `users.view`."


@extend_schema_field({"type": "string", "pattern": f"^{AMOUNT.pattern}$"})
class Amount(serializers.CharField):
    """Un importe como string decimal (`"4399.00"`), nunca un número JSON: un `float` ya perdió
    los céntimos. Sale siempre con dos decimales."""

    def to_internal_value(self, data: Any) -> Any:
        if not isinstance(data, str):
            raise serializers.ValidationError('Un string decimal, como "4399.00".')
        try:
            value = parse_amount(data)
        except ValueError:
            raise serializers.ValidationError("Hasta 12 cifras enteras y 2 decimales.") from None
        if value <= 0:
            raise serializers.ValidationError("Mayor que cero.")
        return value

    def to_representation(self, value: Any) -> str:
        return amount_text(value)


class Instant(serializers.DateTimeField):
    """Con zona horaria: sin ella el instante es ambiguo (el servidor está en UTC)."""

    def enforce_timezone(self, value: Any) -> Any:
        if timezone.is_naive(value):
            raise serializers.ValidationError("Con zona horaria (`Z` o `-05:00`).")
        return super().enforce_timezone(value)


class PriceVariantSerializer(serializers.Serializer[Any]):
    id = serializers.UUIDField()
    sku = serializers.CharField()
    name = serializers.CharField()
    product_id = serializers.UUIDField()
    product_name = serializers.CharField(source="product.name")


class CurrentPriceSerializer(serializers.Serializer[Any]):
    price_id = serializers.UUIDField(source="pk")
    amount = Amount()
    currency = serializers.CharField()
    tax_included = serializers.BooleanField()
    starts_at = serializers.DateTimeField()
    ends_at = serializers.DateTimeField(allow_null=True)


class NextPriceSerializer(serializers.Serializer[Any]):
    price_id = serializers.UUIDField(source="pk")
    amount = Amount()
    starts_at = serializers.DateTimeField()


class PriceResolutionSerializer(serializers.Serializer[Any]):
    found = serializers.BooleanField()
    reason = serializers.ChoiceField(
        choices=[reason.value for reason in services.Reason], allow_null=True
    )
    final_amount = Amount(allow_null=True, help_text="Lo que paga el cliente, IGV incluido.")
    valid_until = serializers.DateTimeField(allow_null=True, help_text="`null`: sin fin.")


class ListPriceSerializer(serializers.Serializer[Any]):
    variant = PriceVariantSerializer()
    current = CurrentPriceSerializer(allow_null=True, help_text="El que rige ahora en la lista.")
    next = NextPriceSerializer(allow_null=True, help_text="El siguiente programado.")
    resolution = PriceResolutionSerializer(
        help_text="Lo que dice `PricingService`: sin precio si la variante no se vende "
        "(`VARIANT_UNAVAILABLE`) o no tiene precio vigente (`NO_PRICE`)."
    )


class PriceSerializer(serializers.Serializer[Any]):
    id = serializers.UUIDField()
    variant_id = serializers.UUIDField()
    price_list_id = serializers.UUIDField()
    amount = Amount()
    previous_amount = Amount(
        source="previous_price.amount", allow_null=True, help_text="El que regía al fijarlo."
    )
    currency = serializers.CharField()
    tax_included = serializers.BooleanField()
    starts_at = serializers.DateTimeField()
    ends_at = serializers.DateTimeField(allow_null=True)
    change_reason = serializers.CharField()
    created_at = serializers.DateTimeField()
    created_by_user_id = serializers.UUIDField(allow_null=True, help_text=AUTHOR)


class PriceSetSerializer(serializers.Serializer[Any]):
    variant_id = serializers.UUIDField()
    amount = Amount(help_text="IGV incluido. Mayor que cero.")
    starts_at = Instant(
        required=False, help_text="Desde cuándo rige: ahora o después, nunca antes. Sin él, ahora."
    )
    reason = serializers.CharField(max_length=REASON_MAX, help_text="Sin HTML.")

    def validate_reason(self, value: str) -> str:
        try:
            return change_reason(value)
        except ValueError:  # el mensaje nunca repite el motivo
            reason = f"De {REASON_MIN} a {REASON_MAX} caracteres, sin HTML."
            raise serializers.ValidationError(reason) from None


class ProductFilterSerializer(serializers.Serializer[Any]):
    product_id = serializers.UUIDField(required=False, help_text="Solo las de ese producto.")


class PriceListFilterSerializer(serializers.Serializer[Any]):
    price_list_id = serializers.UUIDField(
        required=False, help_text="Solo el de esa lista. 404 si el actor no la ve."
    )


def _filter(
    request: Request, by: type[serializers.Serializer[Any]] = ProductFilterSerializer
) -> dict[str, Any]:
    wanted = by(data=request.query_params.dict())
    wanted.is_valid(raise_exception=True)
    return dict(wanted.validated_data)


def _price_list(request: Request, price_list_id: UUID) -> PriceList:
    """La lista, si el actor la ve y está activa: si no, el 404 de una que no existe."""
    found: PriceList | None = _visible(request).filter(pk=price_list_id, is_active=True).first()
    if found is None:
        raise NotFound
    return found


@extend_schema_view(
    get=extend_schema(
        operation_id="price_list_prices_list",
        tags=["pricing"],
        parameters=[ProductFilterSerializer],
        responses={200: ListPriceSerializer(many=True), **errors(400, 401, 403, 404)},
        description="Las variantes vivas por creación (ADR-016), con el precio vigente, el "
        "próximo y lo que resuelve `PricingService`. 404: lista oculta o inactiva.",
    ),
    post=extend_schema(
        operation_id="price_list_prices_create",
        tags=["pricing"],
        request=PriceSetSerializer,
        responses={201: PriceSerializer, **errors(400, 401, 403, 404, 409)},
        description="Fija el precio de una variante, desde ahora o programado. 409 "
        "`PRICE_UNCHANGED`, `PRICE_ALREADY_SCHEDULED` o `PRICE_CONFLICT`.",
    ),
)
class ListPricesView(generics.ListAPIView):
    required_permissions = {"GET": ("prices.view", "products.view"), "POST": "prices.manage"}
    serializer_class = ListPriceSerializer

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
                resolved = services.resolve_many(ectx, ids, price_list.pk)
            except services.ListNotVisible:  # se desactivó entre las dos lecturas
                raise NotFound from None
            when = resolved[ids[0]].resolved_at  # el mismo instante para las tres columnas
            ruling = {p.variant_id: p for p in prices_at(ids, price_list.pk, when)}
            following = {p.variant_id: p for p in next_prices(ids, price_list.pk, when)}
            rows = [
                {
                    "variant": variant,
                    "current": ruling.get(variant.pk),
                    "next": following.get(variant.pk),
                    "resolution": resolved[variant.pk],
                }
                for variant in page
            ]
        return self.get_paginated_response(ListPriceSerializer(rows, many=True).data)

    def post(self, request: Request, price_list_id: UUID, **kwargs: Any) -> Response:
        price_list = _price_list(request, price_list_id)
        wanted = PriceSetSerializer(data=request.data)
        wanted.is_valid(raise_exception=True)
        try:  # `variant_id`, `amount`, `reason` y `starts_at`, ya validados
            price = commands.set_price(
                _tenant(), price_list_id=price_list.pk, **wanted.validated_data
            )
        except commands.RetroactivePrice:
            raise serializers.ValidationError({"starts_at": [PAST]}) from None
        except ObjectDoesNotExist:  # la variante o la lista ya no están vivas
            raise NotFound from None
        except commands.PriceUnchanged:
            raise ApiError("PRICE_UNCHANGED", 409, UNCHANGED) from None
        except commands.PriceAlreadyScheduled:
            raise ApiError("PRICE_ALREADY_SCHEDULED", 409, SCHEDULED) from None
        except commands.PriceConflict:
            raise ApiError("PRICE_CONFLICT", 409, CONFLICT) from None
        return Response(PriceSerializer(price).data, status=201)


@extend_schema_view(
    get=extend_schema(
        operation_id="variant_price_history_list",
        tags=["pricing"],
        parameters=[PriceListFilterSerializer],
        responses={200: PriceSerializer(many=True), **errors(400, 401, 403, 404)},
        description="Los precios de la variante en las listas que el actor ve, activas o no, del "
        "más nuevo al más antiguo (ADR-016), con su motivo y su autor. La variante borrada "
        "conserva su historial. 404: la variante o la lista de `price_list_id` no existe, es de "
        "otra organización o el actor no la ve.",
    ),
)
class PriceHistoryView(generics.ListAPIView):
    """El historial de una variante (F3-44b, E03-04). Pide también `products.view`, como los
    precios de una lista: revela la variante. El autor solo por `created_by_user_id`."""

    required_permissions = {"GET": ("prices.manage", "products.view")}
    serializer_class = PriceSerializer
    ordering = "-id"

    def get_queryset(self) -> QuerySet[ProductPrice]:
        variant_id = self.kwargs["variant_id"]
        wanted = _filter(self.request, PriceListFilterSerializer)
        if not ProductVariant.objects.filter(pk=variant_id).exists():  # viva o borrada
            raise NotFound
        ectx = request_context(self.request)
        assert ectx is not None  # noqa: S101 — `HasPermission` ya lo comprobó
        rows = price_history(ectx, variant_id).select_related("previous_price")
        if "price_list_id" in wanted:
            if not _visible(self.request).filter(pk=wanted["price_list_id"]).exists():
                raise NotFound
            rows = rows.filter(price_list_id=wanted["price_list_id"])
        return rows
