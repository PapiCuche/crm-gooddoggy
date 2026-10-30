"""Las promociones (F3-48d/e, E03-05; ADR-006 §2): `…/pricing/promotions/`. Leer pide
`prices.view` y `products.view` (como las rutas de precios: revela variantes y productos) y ve las
publicadas cuyas listas, activas, ve el actor, o todas con `prices.manage`; crear (siempre en
borrador), publicar y cancelar piden `prices.manage`. Lo que el actor no ve da el 404 de lo que no
existe. Importes en string decimal; ningún campo de costo ni de margen. La organización sale del
contexto. Estricto: un campo desconocido es 400 con su nombre, y un tipo que no es el del campo
(`"5"` por 5, `1` por `true`) es 400, nunca se convierte.
"""

from collections import defaultdict
from collections.abc import Mapping
from typing import Any
from uuid import UUID

from django.db.models import QuerySet
from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework import generics, serializers
from rest_framework.exceptions import NotFound
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.access.permissions import request_context
from apps.pricing import promotions as commands
from apps.pricing.api.prices import Amount, Instant
from apps.pricing.api.views import _tenant
from apps.pricing.models import DiscountType, Promotion, PromotionStatus
from apps.pricing.selectors import promotion_items, promotion_lists, visible_promotions
from core.api.errors import ApiError
from core.api.schema import errors

NOT_DRAFT = "Solo se publica una promoción en borrador."
CANCELLED = "La promoción ya está cancelada."
ENDED = "Ya pasó su fin: no se puede publicar."
UNKNOWN = "Campo desconocido."


class Strict:
    """Solo el tipo propio del campo: ni `"5"` ni `1` ni `5.0`, ni un número como texto."""

    kind: tuple[type, ...] = (str,)

    def to_internal_value(self, data: Any) -> Any:
        if type(data) not in self.kind:
            self.fail("invalid")  # type: ignore[attr-defined]
        return super().to_internal_value(data)  # type: ignore[misc]


class Text(Strict, serializers.CharField):
    pass


class Id(Strict, serializers.UUIDField):
    pass


class Whole(Strict, serializers.IntegerField):
    kind = (int,)


class Flag(Strict, serializers.BooleanField):
    kind = (bool,)


class Closed:
    """Un campo que el serializador no declara es 400 con su nombre (un `overide_value` no se
    pierde en silencio)."""

    def to_internal_value(self, data: Any) -> Any:
        if isinstance(data, Mapping):
            extra = {str(key): [UNKNOWN] for key in data if key not in self.fields}  # type: ignore[attr-defined]
            if extra:
                raise serializers.ValidationError(extra)
        return super().to_internal_value(data)  # type: ignore[misc]


class PromotionItemSerializer(Closed, serializers.Serializer[Any]):
    variant_id = Id(required=False, allow_null=True)
    product_id = Id(required=False, allow_null=True)
    category_id = Id(required=False, allow_null=True)
    brand_id = Id(required=False, allow_null=True)
    override_value = Amount(required=False, allow_null=True)


class PromotionSerializer(serializers.Serializer[Any]):
    id = serializers.UUIDField()
    name = serializers.CharField()
    description = serializers.CharField()
    terms = serializers.CharField()
    discount_type = serializers.ChoiceField(choices=DiscountType.choices)
    value = Amount()
    currency = serializers.CharField()
    starts_at = serializers.DateTimeField()
    ends_at = serializers.DateTimeField()
    status = serializers.ChoiceField(choices=PromotionStatus.choices)
    priority = serializers.IntegerField()
    ai_exposable = serializers.BooleanField()
    channels = serializers.ListField(child=serializers.CharField(), allow_null=True)
    version = serializers.IntegerField()
    created_at = serializers.DateTimeField()
    created_by_user_id = serializers.UUIDField(allow_null=True)
    items = PromotionItemSerializer(many=True, source="targets")
    price_list_ids = serializers.ListField(child=serializers.UUIDField(), source="listed")


class PromotionCreateSerializer(Closed, serializers.Serializer[Any]):
    name = Text(max_length=commands.NAME_MAX)
    description = Text(max_length=commands.TEXT_MAX, required=False, allow_blank=True)
    terms = Text(max_length=commands.TEXT_MAX, required=False, allow_blank=True)
    discount_type = serializers.ChoiceField(choices=DiscountType.choices)
    value = Amount()
    currency = Text(max_length=3, required=False)
    starts_at = Instant()
    ends_at = Instant()
    priority = Whole(required=False)
    ai_exposable = Flag(required=False)
    channels = serializers.ListField(
        child=Text(), allow_null=True, required=False, max_length=commands.CHANNELS_MAX
    )
    price_list_ids = serializers.ListField(child=Id(), min_length=1, max_length=commands.LISTS_MAX)
    items = PromotionItemSerializer(many=True, min_length=1, max_length=commands.ITEMS_MAX)


def _loaded(promotions: list[Promotion]) -> list[Promotion]:
    """Cada promoción con sus objetivos y listas, en dos consultas para todas."""
    ids = [promotion.pk for promotion in promotions]
    targets, listed = defaultdict(list), defaultdict(list)
    for target in promotion_items(ids).order_by("id"):
        targets[target.promotion_id].append(target)
    for link in promotion_lists(ids).order_by("id"):
        listed[link.promotion_id].append(link.price_list_id)
    for promotion in promotions:
        promotion.targets, promotion.listed = targets[promotion.pk], listed[promotion.pk]  # type: ignore[attr-defined]
    return promotions


def _visible(request: Request) -> QuerySet[Promotion]:
    ectx = request_context(request)
    assert ectx is not None  # noqa: S101 — `HasPermission` ya lo comprobó
    return visible_promotions(ectx)


@extend_schema_view(
    get=extend_schema(
        operation_id="promotions_list",
        tags=["pricing"],
        responses={200: PromotionSerializer(many=True), **errors(400, 401, 403, 404)},
        description="Con `prices.manage` todas; si no, las publicadas cuyas listas, activas, ve "
        "el actor.",
    ),
    post=extend_schema(
        operation_id="promotions_create",
        tags=["pricing"],
        request=PromotionCreateSerializer,
        responses={201: PromotionSerializer, **errors(400, 401, 403, 404)},
        description="Crea una promoción en borrador; no se aplica hasta publicarla.",
    ),
)
class PromotionsView(generics.ListAPIView):
    required_permissions = {"GET": ("prices.view", "products.view"), "POST": "prices.manage"}
    serializer_class = PromotionSerializer

    def get_queryset(self) -> QuerySet[Promotion]:
        return _visible(self.request)

    def list(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        page = self.paginate_queryset(self.filter_queryset(self.get_queryset())) or []
        return self.get_paginated_response(PromotionSerializer(_loaded(page), many=True).data)

    def post(self, request: Request, **kwargs: Any) -> Response:
        wanted = PromotionCreateSerializer(data=request.data)
        wanted.is_valid(raise_exception=True)
        try:
            created = commands.create_promotion(_tenant(), **wanted.validated_data)
        except commands.Invalid as error:
            raise serializers.ValidationError({error.field: [f"{error}."]}) from None
        return Response(PromotionSerializer(_loaded([created])[0]).data, status=201)


class PromotionView(APIView):
    required_permissions = {"GET": ("prices.view", "products.view")}

    @extend_schema(
        operation_id="promotions_retrieve",
        tags=["pricing"],
        responses={200: PromotionSerializer, **errors(401, 403, 404)},
        description="Una promoción con sus objetivos y listas.",
    )
    def get(self, request: Request, promotion_id: UUID, **kwargs: Any) -> Response:
        found = _visible(request).filter(pk=promotion_id).first()
        if found is None:
            raise NotFound
        return Response(PromotionSerializer(_loaded([found])[0]).data)


def _moved(run: Any, promotion_id: UUID) -> Response:
    """El comando de cambio de estado, con sus fallos como respuesta; el cuerpo, el del detalle."""
    try:
        moved: Promotion = run(_tenant(), promotion_id=promotion_id)
    except Promotion.DoesNotExist:
        raise NotFound from None
    except commands.PromotionStateConflict as error:
        if error.status == PromotionStatus.CANCELLED and run is commands.cancel_promotion:
            raise ApiError("PROMOTION_ALREADY_CANCELLED", 409, CANCELLED) from None
        raise ApiError("PROMOTION_NOT_DRAFT", 409, NOT_DRAFT) from None
    except commands.PromotionEnded:
        raise ApiError("PROMOTION_ENDED", 409, ENDED) from None
    return Response(PromotionSerializer(_loaded([moved])[0]).data)


class PromotionPublishView(APIView):
    required_permissions = {"POST": "prices.manage"}

    @extend_schema(
        operation_id="promotions_publish",
        tags=["pricing"],
        request=None,
        responses={200: PromotionSerializer, **errors(401, 403, 404, 409)},
        description="Publica una promoción en borrador: `PricingService` la aplica entre "
        "`starts_at` y `ends_at`. 409 `PROMOTION_NOT_DRAFT` (ya publicada o cancelada) o "
        "`PROMOTION_ENDED`.",
    )
    def post(self, request: Request, promotion_id: UUID, **kwargs: Any) -> Response:
        return _moved(commands.publish_promotion, promotion_id)


class PromotionCancelView(APIView):
    required_permissions = {"POST": "prices.manage"}

    @extend_schema(
        operation_id="promotions_cancel",
        tags=["pricing"],
        request=None,
        responses={200: PromotionSerializer, **errors(401, 403, 404, 409)},
        description="Cancela una promoción en borrador o publicada: deja de aplicarse al "
        "instante. 409 `PROMOTION_ALREADY_CANCELLED`.",
    )
    def post(self, request: Request, promotion_id: UUID, **kwargs: Any) -> Response:
        return _moved(commands.cancel_promotion, promotion_id)
