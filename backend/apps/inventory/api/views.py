"""Rutas de tenant de los almacenes: `…/warehouses/` (F3-08 y F3-11, E04-01)."""

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

from apps.inventory import warehouses as commands
from apps.inventory.models import Warehouse
from apps.inventory.selectors import warehouses
from core.api.errors import ApiError
from core.api.schema import errors
from core.tenancy import context

CODE_IN_USE = "Ya hay un almacén con ese código en la organización."
UNKNOWN = "No existe en esta organización."
HAS_STOCK = "El almacén tiene existencias: sácalas o ajústalas a cero antes."
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
        description="Crea un almacén en una sucursal de la organización, activo salvo "
        "`is_active=false`. 409 `WAREHOUSE_CODE_TAKEN`: ya hay uno con ese código.",
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


IMMUTABLE = "No se puede cambiar: crea otro almacén si hace falta."


class WarehouseView(APIView):
    required_permissions = {"PATCH": "warehouses.manage", "DELETE": "warehouses.manage"}

    @extend_schema(
        operation_id="warehouses_update",
        tags=["inventory"],
        request=WarehouseUpdateSerializer,
        responses={200: WarehouseSerializer, **errors(400, 401, 403, 404)},
        description="Cambia el nombre, el tipo, si cuenta para la venta o si está activo. "
        "`code` o `branch_id` dan 400 en ese campo: no cambian. "
        "404: no es de la organización o está borrado.",
    )
    def patch(self, request: Request, warehouse_id: UUID, **kwargs: Any) -> Response:
        # El código y la sucursal no cambian: pedirlo es un error del cliente, no se ignora.
        # Un cuerpo que no es un objeto (`5`, `null`, una lista) lo rechaza el serializador.
        sent = request.data if isinstance(request.data, Mapping) else {}
        fixed = [field for field in ("code", "branch_id") if field in sent]
        if fixed:
            raise serializers.ValidationError({field: [IMMUTABLE] for field in fixed})
        wanted = WarehouseUpdateSerializer(data=request.data)
        wanted.is_valid(raise_exception=True)
        try:
            warehouse = commands.update_warehouse(
                _tenant(), warehouse_id=warehouse_id, **wanted.validated_data
            )
        except Warehouse.DoesNotExist:
            raise NotFound from None
        return Response(WarehouseSerializer(warehouse).data)

    @extend_schema(
        operation_id="warehouses_delete",
        tags=["inventory"],
        responses={204: None, **errors(401, 403, 404, 409)},
        description="Borra el almacén (borrado lógico) y libera su código. 409 "
        "`WAREHOUSE_HAS_STOCK`: tiene existencias. 404: no es de la organización o ya está "
        "borrado.",
    )
    def delete(self, request: Request, warehouse_id: UUID, **kwargs: Any) -> Response:
        try:
            commands.delete_warehouse(_tenant(), warehouse_id=warehouse_id)
        except Warehouse.DoesNotExist:
            raise NotFound from None
        except commands.WarehouseHasStock:
            raise ApiError("WAREHOUSE_HAS_STOCK", 409, HAS_STOCK) from None
        return Response(status=204)
