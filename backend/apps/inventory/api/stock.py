"""Rutas de tenant de las existencias: `…/inventory/levels/` y `…/inventory/movements/` (F3-11,
E04-02 y E04-03). El ledger se filtra también por transferencia (F3-31)."""

from typing import Any

from django.db.models import QuerySet
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema, extend_schema_field, extend_schema_view
from rest_framework import generics, serializers
from rest_framework.request import Request
from rest_framework.response import Response

from apps.inventory import stock
from apps.inventory.api.views import _checked, _tenant
from apps.inventory.models import InventoryLevel, InventoryMovement
from apps.inventory.selectors import levels, movements
from core.api.errors import ApiError
from core.api.schema import errors
from core.api.strict import Checked, Id

NOT_ENOUGH = "No hay stock suficiente: no puede quedar en negativo ni por debajo de lo reservado."
INACTIVE = "El almacén está desactivado."
REUSED = "Esa clave de idempotencia ya se usó para otro movimiento."
LIMIT = f"El saldo no puede pasar de {stock.ON_HAND_MAX} unidades."
REFUSED = "Solo una persona o el sistema mueve, reserva o transfiere stock."


def refused() -> ApiError:
    """`stock.ActorRefused` (y `stock.ActorMismatch`) como lo responden movimientos,
    importaciones, reservas y transferencias (F3-39, F3-39b): 403, nunca un 400 ni un 500."""
    return ApiError("ACTOR_NOT_ALLOWED", 403, REFUSED)


def key_field() -> Checked:
    """La `idempotency_key` de movimientos, transferencias y reservas, con una sola regla (F3-39):
    texto tal cual, sin recortar (una clave con espacios alrededor es otra clave, y no vale)."""
    return Checked(
        stock.idempotency_key,
        max_length=100,
        trim_whitespace=False,
        help_text="De 8 a 100 letras ASCII, cifras y `. _ : -`, sin empezar por `import:`. Va "
        "tal cual: con espacios alrededor es 400, no se recorta. Repetirla no duplica la petición.",
    )


class CellFilterSerializer(serializers.Serializer[Any]):
    variant_id = serializers.UUIDField(required=False, help_text="Solo los de esa variante.")
    warehouse_id = serializers.UUIDField(required=False, help_text="Solo los de ese almacén.")


class LevelFilterSerializer(CellFilterSerializer):
    has_stock = serializers.BooleanField(
        required=False,
        help_text="`true`: solo las filas con existencias (`on_hand > 0`); `false`: solo las "
        "que se quedaron en cero.",
    )


class MovementFilterSerializer(CellFilterSerializer):
    transfer_group_id = Id(
        required=False, help_text="Solo las dos patas de esa transferencia (F3-31)."
    )


def _filters(request: Request, serializer: type[CellFilterSerializer]) -> dict[str, Any]:
    wanted = serializer(data=request.query_params.dict())
    wanted.is_valid(raise_exception=True)
    filters = dict(wanted.validated_data)
    if "has_stock" in filters:
        filters["on_hand__gt" if filters.pop("has_stock") else "on_hand"] = 0
    return filters


class VariantLabelSerializer(serializers.Serializer[Any]):
    """Lo justo para nombrar la variante de una fila, sin pedirla aparte."""

    id = serializers.UUIDField()
    sku = serializers.CharField()
    name = serializers.CharField(help_text="El del producto y, si la tiene, su etiqueta.")
    product_id = serializers.UUIDField()
    is_deleted = serializers.SerializerMethodField(
        help_text="Borrada en el catálogo: su saldo y su historial siguen aquí."
    )

    @extend_schema_field(OpenApiTypes.BOOL)
    def get_is_deleted(self, variant: Any) -> bool:
        return variant.deleted_at is not None


class LevelSerializer(serializers.Serializer[Any]):
    id = serializers.UUIDField()
    variant_id = serializers.UUIDField()
    variant = VariantLabelSerializer(read_only=True)
    warehouse_id = serializers.UUIDField()
    on_hand = serializers.IntegerField(help_text="Las unidades que hay.")
    reserved = serializers.IntegerField(help_text="Las apartadas para cotizaciones y pedidos.")
    available = serializers.IntegerField(
        help_text="`on_hand - reserved`: las que se pueden vender."
    )


@extend_schema_view(
    get=extend_schema(
        operation_id="inventory_levels_list",
        tags=["inventory"],
        parameters=[LevelFilterSerializer],
        responses={200: LevelSerializer(many=True), **errors(400, 401, 403, 404)},
        description="El saldo de cada variante en cada almacén que tuvo movimientos, con su SKU "
        "y su nombre, paginado por creación (ADR-016).",
    )
)
class LevelsView(generics.ListAPIView):
    required_permissions = {"GET": "inventory.view"}
    serializer_class = LevelSerializer

    def get_queryset(self) -> QuerySet[InventoryLevel]:
        return levels().filter(**_filters(self.request, LevelFilterSerializer))


class MovementSerializer(serializers.Serializer[Any]):
    id = serializers.UUIDField()
    variant_id = serializers.UUIDField()
    variant = VariantLabelSerializer(read_only=True)
    warehouse_id = serializers.UUIDField()
    movement_type = serializers.ChoiceField(choices=InventoryMovement.Type.choices)
    quantity = serializers.IntegerField(help_text="El cambio en `on_hand`, con su signo.")
    on_hand_after = serializers.IntegerField()
    reserved_after = serializers.IntegerField()
    reason = serializers.CharField()
    reference_type = serializers.ChoiceField(choices=InventoryMovement.Reference.choices)
    reference_id = serializers.UUIDField(
        allow_null=True, help_text="La importación o la transferencia que lo originó."
    )
    transfer_group_id = serializers.UUIDField(
        allow_null=True, help_text="El mismo en las dos patas de una transferencia."
    )
    other_warehouse_id = serializers.UUIDField(
        allow_null=True,
        read_only=True,
        help_text="En una pata de transferencia, el almacén de la otra pata; si no, `null`.",
    )
    created_by_user_id = serializers.UUIDField(allow_null=True)
    created_at = serializers.DateTimeField()


class MovementCreateSerializer(serializers.Serializer[Any]):
    variant_id = serializers.UUIDField()
    warehouse_id = serializers.UUIDField()
    movement_type = serializers.ChoiceField(choices=stock.KINDS)
    quantity = serializers.IntegerField(
        min_value=0,
        max_value=stock.QUANTITY_MAX,
        help_text="Lo que entra o sale (desde 1) o, en un ajuste, lo contado.",
    )
    reason = serializers.CharField(max_length=stock.REASON_MAX)
    idempotency_key = key_field()

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        if attrs["quantity"] == 0 and attrs["movement_type"] != stock.Type.ADJUSTMENT:
            raise serializers.ValidationError({"quantity": ["Desde 1, salvo en un ajuste."]})
        return attrs

    def validate_reason(self, value: str) -> str:
        return str(_checked(stock.reason, value))


@extend_schema_view(
    get=extend_schema(
        operation_id="inventory_movements_list",
        tags=["inventory"],
        parameters=[MovementFilterSerializer],
        responses={200: MovementSerializer(many=True), **errors(400, 401, 403, 404)},
        description="El ledger: los movimientos, con el SKU y el nombre de su variante, del más "
        "reciente al más antiguo (ADR-016).",
    ),
    post=extend_schema(
        operation_id="inventory_movements_create",
        tags=["inventory"],
        request=MovementCreateSerializer,
        responses={
            201: MovementSerializer,
            200: MovementSerializer,
            **errors(400, 401, 403, 404, 409),
        },
        description="Registra un ingreso, una salida o un ajuste (lo contado) y deja el saldo "
        "como queda. 200: esa `idempotency_key` ya registró este mismo movimiento, que se "
        "devuelve sin repetirlo. 403 `ACTOR_NOT_ALLOWED`: un agente de IA o una integración no "
        "mueve stock. 409 `INSUFFICIENT_STOCK`, `WAREHOUSE_INACTIVE`, "
        "`IDEMPOTENCY_KEY_REUSED` o `STOCK_LIMIT` (el saldo pasaría de 2147483647).",
    ),
)
class MovementsView(generics.ListAPIView):
    required_permissions = {"GET": "inventory.view", "POST": "inventory.adjust"}
    serializer_class = MovementSerializer
    ordering = "-id"  # UUIDv7: el orden en que se registraron

    def get_queryset(self) -> QuerySet[InventoryMovement]:
        return movements().filter(**_filters(self.request, MovementFilterSerializer))

    def post(self, request: Request, **kwargs: Any) -> Response:
        wanted = MovementCreateSerializer(data=request.data)
        wanted.is_valid(raise_exception=True)
        try:
            movement, new = stock.move(_tenant(), stock.Wanted(**wanted.validated_data))
        except stock.ActorRefused:
            raise refused() from None
        except stock.BadReference as error:
            raise serializers.ValidationError({error.field: [str(error)]}) from None
        except stock.InsufficientStock:
            raise ApiError("INSUFFICIENT_STOCK", 409, NOT_ENOUGH) from None
        except stock.WarehouseInactive:
            raise ApiError("WAREHOUSE_INACTIVE", 409, INACTIVE) from None
        except stock.StockLimit:
            raise ApiError("STOCK_LIMIT", 409, LIMIT) from None
        except stock.KeyReused:
            raise ApiError("IDEMPOTENCY_KEY_REUSED", 409, REUSED) from None
        return Response(MovementSerializer(movement).data, status=201 if new else 200)
