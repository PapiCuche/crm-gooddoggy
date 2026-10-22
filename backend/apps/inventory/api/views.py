"""Rutas de tenant del inventario: `…/warehouses/` (F3-08, E04-01)."""

from typing import Any
from uuid import UUID

from django.db.models import QuerySet
from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework import generics, serializers
from rest_framework.exceptions import NotFound
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.inventory import warehouses as commands
from apps.inventory.models import Warehouse
from apps.inventory.selectors import warehouses
from core.api.errors import ApiError
from core.api.schema import errors
from core.tenancy import context

CODE_IN_USE = "Ya hay un almacén con ese código en la organización."
UNKNOWN = "No existe en esta organización."
SELLABLE = "Si su stock cuenta para la venta (el que verán la IA y el cliente)."


def _tenant() -> Any:
    tenant = context.current()
    assert tenant is not None  # noqa: S101 — `HasPermission` ya lo comprobó
    return tenant


def _checked(cleaner: Any, value: Any) -> Any:
    """El mismo criterio que el comando, como un 400 en su campo."""
    try:
        return cleaner(value)
    except ValueError as error:
        text = f"{error}."
        raise serializers.ValidationError(text[:1].upper() + text[1:]) from None


class WarehouseSerializer(serializers.Serializer[Any]):
    id = serializers.UUIDField()
    branch_id = serializers.UUIDField(help_text="La sucursal del almacén. No cambia.")
    code = serializers.CharField(help_text="Clave estable, única en la organización. No cambia.")
    name = serializers.CharField()
    type = serializers.ChoiceField(choices=Warehouse.Type.choices)
    is_sellable = serializers.BooleanField(help_text=SELLABLE)
    is_active = serializers.BooleanField()


class WarehouseUpdateSerializer(serializers.Serializer[Any]):
    name = serializers.CharField(max_length=commands.NAME_MAX, required=False)
    type = serializers.ChoiceField(choices=Warehouse.Type.choices, required=False)
    is_sellable = serializers.BooleanField(required=False)
    is_active = serializers.BooleanField(required=False)

    def validate_name(self, value: str) -> str:
        return str(_checked(commands.warehouse_name, value))


class WarehouseCreateSerializer(WarehouseUpdateSerializer):
    branch_id = serializers.UUIDField()
    code = serializers.CharField(max_length=commands.CODE_MAX, help_text="Se guarda en mayúsculas.")
    name = serializers.CharField(max_length=commands.NAME_MAX)

    def validate_code(self, value: str) -> str:
        return str(_checked(commands.warehouse_code, value))


@extend_schema_view(
    get=extend_schema(
        operation_id="warehouses_list",
        tags=["inventory"],
        responses={200: WarehouseSerializer(many=True), **errors(400, 401, 403, 404)},
        description="Los almacenes de la organización, activos o no, sin los borrados. "
        "Paginado por creación (ADR-016).",
    ),
    post=extend_schema(
        operation_id="warehouses_create",
        tags=["inventory"],
        request=WarehouseCreateSerializer,
        responses={201: WarehouseSerializer, **errors(400, 401, 403, 404, 409)},
        description="Crea un almacén activo en una sucursal de la organización. 409 "
        "`WAREHOUSE_CODE_TAKEN`: ya hay uno con ese código.",
    ),
)
class WarehousesView(generics.ListAPIView):
    required_permissions = {"GET": "inventory.view", "POST": "warehouses.manage"}
    serializer_class = WarehouseSerializer

    def get_queryset(self) -> QuerySet[Warehouse]:
        return warehouses()

    def post(self, request: Request, **kwargs: Any) -> Response:
        wanted = WarehouseCreateSerializer(data=request.data)
        wanted.is_valid(raise_exception=True)
        try:
            warehouse = commands.create_warehouse(_tenant(), **wanted.validated_data)
        except commands.WarehouseCodeTaken:
            raise ApiError("WAREHOUSE_CODE_TAKEN", 409, CODE_IN_USE) from None
        except commands.UnknownBranch:
            raise serializers.ValidationError({"branch_id": [UNKNOWN]}) from None
        return Response(WarehouseSerializer(warehouse).data, status=201)


class WarehouseView(APIView):
    required_permissions = {"PATCH": "warehouses.manage"}

    @extend_schema(
        operation_id="warehouses_update",
        tags=["inventory"],
        request=WarehouseUpdateSerializer,
        responses={200: WarehouseSerializer, **errors(400, 401, 403, 404)},
        description="Cambia el nombre, el tipo, si cuenta para la venta o si está activo. "
        "404: no es de la organización o está borrado.",
    )
    def patch(self, request: Request, warehouse_id: UUID, **kwargs: Any) -> Response:
        wanted = WarehouseUpdateSerializer(data=request.data)
        wanted.is_valid(raise_exception=True)
        try:
            warehouse = commands.update_warehouse(
                _tenant(), warehouse_id=warehouse_id, **wanted.validated_data
            )
        except Warehouse.DoesNotExist:
            raise NotFound from None
        return Response(WarehouseSerializer(warehouse).data)
