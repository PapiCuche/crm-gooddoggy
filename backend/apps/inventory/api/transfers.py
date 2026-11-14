"""Ruta de tenant de las transferencias entre almacenes: `…/inventory/transfers/` (F3-31, E04-04).
El servicio es `apps.inventory.transfers` (F3-30); aquí van el permiso (`inventory.transfer`, sin
alcance en el catálogo: toda la organización), el cuerpo cerrado y los errores. No hay listado
propio: las dos patas se ven en el ledger con `?transfer_group_id=`."""

from typing import Any

from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.inventory import stock, transfers
from apps.inventory.api.stock import LIMIT, NOT_ENOUGH, MovementSerializer
from apps.inventory.api.views import _checked, _tenant
from core.api.errors import ApiError
from core.api.schema import errors
from core.api.strict import Closed, Id, Text, Whole

SAME = "Distinto del almacén de origen."
INACTIVE = {  # el almacén desactivado, solo en el texto: `fields` es de VALIDATION_ERROR (ADR-014)
    "from_warehouse_id": "El almacén de origen está desactivado.",
    "to_warehouse_id": "El almacén de destino está desactivado.",
}
REUSED = "Esa clave de idempotencia ya se usó para otro movimiento o por otra persona."


class TransferCreateSerializer(Closed, serializers.Serializer[Any]):
    variant_id = Id()
    from_warehouse_id = Id()
    to_warehouse_id = Id()
    quantity = Whole(min_value=1, max_value=stock.QUANTITY_MAX)
    reason = Text(max_length=stock.REASON_MAX)
    idempotency_key = Text(
        max_length=100,
        help_text="De 8 a 100 letras ASCII, cifras y `. _ : -`, sin empezar por `import:`.",
    )

    def validate_reason(self, value: str) -> str:
        return str(_checked(stock.reason, value))

    def validate_idempotency_key(self, value: str) -> str:
        return str(_checked(stock.idempotency_key, value))

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        if attrs["from_warehouse_id"] == attrs["to_warehouse_id"]:
            raise serializers.ValidationError({"to_warehouse_id": [SAME]})
        return attrs


class TransferSerializer(serializers.Serializer[Any]):
    transfer_group_id = serializers.UUIDField()
    legs = MovementSerializer(
        many=True, help_text="`TRANSFER_OUT` (origen) y `TRANSFER_IN` (destino), en ese orden."
    )


class TransfersView(APIView):
    required_permissions = {"POST": "inventory.transfer"}

    @extend_schema(
        operation_id="inventory_transfers_create",
        tags=["inventory"],
        request=TransferCreateSerializer,
        responses={
            201: TransferSerializer,
            200: TransferSerializer,
            **errors(400, 401, 403, 404, 409),
        },
        description="Saca unidades disponibles (`on_hand - reserved`) de un almacén y las mete en "
        "otro, en una sola operación con dos movimientos del mismo `transfer_group_id`. 200: la "
        "misma petición, de la misma persona y con esa `idempotency_key`, ya registrada; se "
        "devuelve sin repetirla. 400 por campo (variante o almacén que no existen, un servicio, "
        "el mismo almacén). 409 `INSUFFICIENT_STOCK`, `WAREHOUSE_INACTIVE` (el texto dice cuál), "
        "`STOCK_LIMIT` o `IDEMPOTENCY_KEY_REUSED` (otra petición, o de otra persona).",
    )
    def post(self, request: Request, **kwargs: Any) -> Response:
        wanted = TransferCreateSerializer(data=request.data)
        wanted.is_valid(raise_exception=True)
        try:
            legs, new = transfers.transfer(_tenant(), transfers.Wanted(**wanted.validated_data))
        except stock.BadReference as error:
            raise serializers.ValidationError({error.field: [str(error)]}) from None
        except transfers.WarehouseInactive as error:
            raise ApiError("WAREHOUSE_INACTIVE", 409, INACTIVE[error.field]) from None
        except stock.InsufficientStock:
            raise ApiError("INSUFFICIENT_STOCK", 409, NOT_ENOUGH) from None
        except stock.StockLimit:
            raise ApiError("STOCK_LIMIT", 409, LIMIT) from None
        except stock.KeyReused:
            raise ApiError("IDEMPOTENCY_KEY_REUSED", 409, REUSED) from None
        made = {"transfer_group_id": legs[0].transfer_group_id, "legs": legs}
        return Response(TransferSerializer(made).data, status=201 if new else 200)
