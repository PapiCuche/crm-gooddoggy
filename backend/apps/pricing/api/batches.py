"""El lote de edición masiva de precios (F3-49b; E03-08; ADR-006 §3, 04 §K.5): crearlo con su
previsualización y verlo. Las dos rutas piden `prices.bulk_update`★, `prices.view` y
`products.view` (la selección nombra categorías, marcas, productos y variantes) antes de leer
nada, y la lista visible: un lote de una lista oculta da el 404 de uno que no existe. Crear no
cambia ningún precio: sin step-up (aplicar lo pedirá). Solo una persona crea un lote: otro actor
recibe un 403, nunca un 400. Importes en string decimal; ningún campo de costo ni de margen.
Cancelar y las líneas, F3-49c.
"""

from typing import Any
from uuid import UUID

from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.exceptions import NotFound
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.access.permissions import request_context
from apps.pricing import batches
from apps.pricing.api.prices import AUTHOR, Amount, Instant
from apps.pricing.api.views import _tenant
from apps.pricing.models import BatchKind, BatchStatus, PriceChangeBatch
from apps.pricing.selectors import visible_batches
from apps.pricing.text import REASON_MAX
from core.api.errors import ApiError
from core.api.schema import errors
from core.api.strict import Choice, Closed, Id, Text, sentence
from core.money import Rounding

ROUNDINGS = [rule.value for rule in Rounding]
PERMISSIONS = ("prices.bulk_update", "prices.view", "products.view")
REFUSED = "Solo una persona edita precios en lote."
TOO_MANY = "La selección tiene {} variantes y el tope es {}: acota los filtros."


class BatchFiltersSerializer(Closed, serializers.Serializer[Any]):
    category_id = Id(required=False, allow_null=True, help_text="Con sus subcategorías.")
    brand_id = Id(required=False, allow_null=True)
    product_id = Id(required=False, allow_null=True)
    variant_ids = serializers.ListField(
        child=Id(), required=False, allow_null=True, min_length=1, max_length=batches.LINES_MAX
    )


class BatchCreateSerializer(Closed, serializers.Serializer[Any]):
    price_list_id = Id(help_text="Una lista activa que el actor ve.")
    operation = Choice(choices=batches.OPERATIONS)
    value = Amount(help_text="Un importe, o un porcentaje hasta 100, con 2 decimales como mucho.")
    rounding = Choice(choices=ROUNDINGS, required=False, help_text="Sin él, `CENTS`.")
    filters = BatchFiltersSerializer(
        help_text="Al menos uno; se combinan con «y». Solo las variantes vivas."
    )
    reason = Text(max_length=REASON_MAX, help_text="Sin HTML.")
    effective_at = Instant(
        required=False, help_text="Desde cuándo regirá: en el futuro. Sin él, al aplicar."
    )


class BatchSummarySerializer(serializers.Serializer[Any]):
    lines = serializers.IntegerField(help_text="Una línea por variante de la selección.")
    changes = serializers.IntegerField(help_text="Las que cambiarán (`PENDING`).")
    no_price = serializers.IntegerField(help_text="Sin precio en la lista en esa fecha.")
    unchanged = serializers.IntegerField(help_text="Quedan igual con el redondeo.")
    errors = serializers.IntegerField(
        help_text="El resultado no es mayor que cero o pasa del tope."
    )
    alerts = serializers.IntegerField(help_text="Una variación de más del 30 %.")
    currency = serializers.CharField()


class BatchSerializer(serializers.Serializer[Any]):
    id = serializers.UUIDField()
    kind = serializers.ChoiceField(choices=BatchKind.choices)
    status = serializers.ChoiceField(choices=BatchStatus.choices)
    price_list_id = serializers.UUIDField()
    operation = serializers.ChoiceField(choices=batches.OPERATIONS, source="params.operation")
    value = serializers.CharField(source="params.value", help_text="Como se guardó: `5.00`.")
    rounding = serializers.ChoiceField(choices=ROUNDINGS, source="params.rounding")
    filters = BatchFiltersSerializer(source="params.filters")
    reason = serializers.CharField()
    effective_at = serializers.DateTimeField(allow_null=True, help_text="`null`: al aplicar.")
    summary = BatchSummarySerializer()
    version = serializers.IntegerField()
    created_at = serializers.DateTimeField()
    created_by_user_id = serializers.UUIDField(allow_null=True, help_text=AUTHOR)
    applied_at = serializers.DateTimeField(allow_null=True)
    applied_by_user_id = serializers.UUIDField(allow_null=True)


class BatchesView(APIView):
    required_permissions = {"POST": PERMISSIONS}

    @extend_schema(
        operation_id="price_batches_create",
        tags=["pricing"],
        request=BatchCreateSerializer,
        responses={201: BatchSerializer, **errors(400, 401, 403, 404)},
        description="Crea el lote (`PREVIEWED`) con una línea por variante: el precio de antes y "
        "el de después. No cambia ningún precio. 400 en su campo (los de la selección, dentro de "
        f"`filters`); más de {batches.LINES_MAX} variantes, 400 en `filters` con el recuento. "
        "403 `ACTOR_NOT_ALLOWED`: no es una persona.",
    )
    def post(self, request: Request, **kwargs: Any) -> Response:
        tenant = _tenant()
        try:  # el actor, antes que el cuerpo: un actor rechazado nunca recibe un 400
            batches.require_person(tenant)
        except batches.ActorRefused:
            raise ApiError("ACTOR_NOT_ALLOWED", 403, REFUSED) from None
        wanted = BatchCreateSerializer(data=request.data)
        wanted.is_valid(raise_exception=True)
        try:
            made = batches.create_batch(tenant, **wanted.validated_data)
        except batches.Invalid as error:
            detail = {error.field: [sentence(error)]}
            nested = error.field in batches.FILTERS
            raise serializers.ValidationError({"filters": detail} if nested else detail) from None
        except batches.TooManyLines as error:
            too_many = TOO_MANY.format(error.count, batches.LINES_MAX)
            raise serializers.ValidationError({"filters": [too_many]}) from None
        return Response(BatchSerializer(made).data, status=201)


class BatchView(APIView):
    required_permissions = {"GET": PERMISSIONS}

    @extend_schema(
        operation_id="price_batches_retrieve",
        tags=["pricing"],
        responses={200: BatchSerializer, **errors(401, 403, 404)},
        description="Un lote con sus recuentos. 404: no existe, es de otra organización o de una "
        "lista que el actor no ve.",
    )
    def get(self, request: Request, batch_id: UUID, **kwargs: Any) -> Response:
        ectx = request_context(request)
        assert ectx is not None  # noqa: S101 — `HasPermission` ya lo comprobó
        found: PriceChangeBatch | None = visible_batches(ectx).filter(pk=batch_id).first()
        if found is None:
            raise NotFound
        return Response(BatchSerializer(found).data)
