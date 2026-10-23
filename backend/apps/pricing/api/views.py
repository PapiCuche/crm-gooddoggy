"""Rutas de tenant de las listas de precios: `…/pricing/price-lists/` (F3-40, E03-01).

Leer pide `prices.view`, y cada lista sale solo si el actor tiene su `required_permission` o
`prices.manage`: la que no ve da el mismo 404 que una que no existe. Crear y cambiar piden
`prices.manage`.
"""

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
from apps.pricing import price_lists as commands
from apps.pricing.models import READ_PERMISSIONS, PriceList
from apps.pricing.selectors import visible_price_lists
from core.api.errors import ApiError
from core.api.schema import errors
from core.tenancy import context

CODE_IN_USE = "Ya hay una lista con ese código en la organización."
IS_DEFAULT = "Es la lista predeterminada de su moneda: elige otra como predeterminada antes."
INACTIVE = "Una lista inactiva no puede ser la predeterminada."
RESTRICTED = "La predeterminada la ve quien ve precios: debe pedir `prices.view`."
IMMUTABLE = "No se puede cambiar: crea otra lista si hace falta."
PERMISSION = "Quién la ve. Si no se envía, sale del tipo: mayorista, distribuidor, interno o "
PERMISSION += "`prices.view`."
AI = "Si un agente de IA de cara al cliente podrá comunicar sus precios (Fase 8)."


def _tenant() -> Any:
    tenant = context.current()
    assert tenant is not None  # noqa: S101 — `HasPermission` ya lo comprobó
    return tenant


def _visible(request: Request) -> QuerySet[PriceList]:
    ectx = request_context(request)
    assert ectx is not None  # noqa: S101 — `HasPermission` ya lo comprobó
    return visible_price_lists(ectx)


def _checked(cleaner: Any, value: Any) -> Any:
    """El mismo criterio que el comando, como un 400 en su campo."""
    try:
        return cleaner(value)
    except ValueError as error:
        text = f"{error}."
        raise serializers.ValidationError(text[:1].upper() + text[1:]) from None


class PriceListSerializer(serializers.Serializer[Any]):
    id = serializers.UUIDField()
    code = serializers.CharField(help_text="Clave estable, única en la organización. No cambia.")
    name = serializers.CharField()
    kind = serializers.ChoiceField(choices=PriceList.Kind.choices)
    currency = serializers.CharField(help_text="ISO 4217. No cambia.")
    is_default = serializers.BooleanField(help_text="La predeterminada de su moneda.")
    required_permission = serializers.ChoiceField(choices=READ_PERMISSIONS, help_text=PERMISSION)
    ai_exposable = serializers.BooleanField(help_text=AI)
    payment_condition = serializers.ChoiceField(
        choices=PriceList.PaymentCondition.choices, allow_null=True
    )
    is_active = serializers.BooleanField()
    sort_order = serializers.IntegerField()


class PriceListUpdateSerializer(serializers.Serializer[Any]):
    name = serializers.CharField(max_length=commands.NAME_MAX, required=False)
    kind = serializers.ChoiceField(choices=PriceList.Kind.choices, required=False)
    is_default = serializers.BooleanField(required=False)
    required_permission = serializers.ChoiceField(choices=READ_PERMISSIONS, required=False)
    ai_exposable = serializers.BooleanField(required=False)
    payment_condition = serializers.ChoiceField(
        choices=PriceList.PaymentCondition.choices, allow_null=True, required=False
    )
    is_active = serializers.BooleanField(required=False)
    sort_order = serializers.IntegerField(min_value=0, max_value=9999, required=False)

    def validate_name(self, value: str) -> str:
        return str(_checked(commands.price_list_name, value))


class PriceListCreateSerializer(PriceListUpdateSerializer):
    code = serializers.CharField(max_length=commands.CODE_MAX, help_text="Se guarda en mayúsculas.")
    name = serializers.CharField(max_length=commands.NAME_MAX)
    kind = serializers.ChoiceField(choices=PriceList.Kind.choices)
    currency = serializers.ChoiceField(
        choices=commands.CURRENCIES, required=False, help_text="Solo PEN por ahora."
    )

    def validate_code(self, value: str) -> str:
        return str(_checked(commands.price_list_code, value))


@extend_schema_view(
    get=extend_schema(
        operation_id="price_lists_list",
        tags=["pricing"],
        responses={200: PriceListSerializer(many=True), **errors(400, 401, 403, 404)},
        description="Las listas que el actor puede ver (su `required_permission`, o todas con "
        "`prices.manage`), activas o no. Paginado por creación (ADR-016).",
    ),
    post=extend_schema(
        operation_id="price_lists_create",
        tags=["pricing"],
        request=PriceListCreateSerializer,
        responses={201: PriceListSerializer, **errors(400, 401, 403, 404, 409)},
        description="Crea una lista. Es la predeterminada de su moneda si se pide (la anterior "
        "deja de serlo) o, sin `is_default`, si es la primera activa con `prices.view`; solo "
        "una así puede serlo (400 en `is_default`). 409 `PRICE_LIST_CODE_TAKEN`.",
    ),
)
class PriceListsView(generics.ListAPIView):
    required_permissions = {"GET": "prices.view", "POST": "prices.manage"}
    serializer_class = PriceListSerializer

    def get_queryset(self) -> QuerySet[PriceList]:
        return _visible(self.request)

    def post(self, request: Request, **kwargs: Any) -> Response:
        wanted = PriceListCreateSerializer(data=request.data)
        wanted.is_valid(raise_exception=True)
        try:
            price_list = commands.create_price_list(_tenant(), **wanted.validated_data)
        except commands.PriceListCodeTaken:
            raise ApiError("PRICE_LIST_CODE_TAKEN", 409, CODE_IN_USE) from None
        except commands.InactiveDefault:
            raise serializers.ValidationError({"is_default": [INACTIVE]}) from None
        except commands.RestrictedDefault:
            raise serializers.ValidationError({"is_default": [RESTRICTED]}) from None
        return Response(PriceListSerializer(price_list).data, status=201)


class PriceListView(APIView):
    required_permissions = {"GET": "prices.view", "PATCH": "prices.manage"}

    @extend_schema(
        operation_id="price_lists_retrieve",
        tags=["pricing"],
        responses={200: PriceListSerializer, **errors(401, 403, 404)},
        description="Una lista. 404: no existe, no es de la organización o el actor no la ve.",
    )
    def get(self, request: Request, price_list_id: UUID, **kwargs: Any) -> Response:
        found = _visible(request).filter(pk=price_list_id).first()
        if found is None:
            raise NotFound
        return Response(PriceListSerializer(found).data)

    @extend_schema(
        operation_id="price_lists_update",
        tags=["pricing"],
        request=PriceListUpdateSerializer,
        responses={200: PriceListSerializer, **errors(400, 401, 403, 404, 409)},
        description="Cambia lo que se envía. `code` o `currency` dan 400 en ese campo: no "
        "cambian. `is_default=true` la hace la predeterminada de su moneda (la anterior deja de "
        "serlo); debe estar activa y pedir `prices.view` (400 en `is_default`). 409 "
        "`PRICE_LIST_IS_DEFAULT`: la predeterminada no se desactiva, no deja de serlo ni cambia "
        "de `required_permission`; antes hay que elegir otra.",
    )
    def patch(self, request: Request, price_list_id: UUID, **kwargs: Any) -> Response:
        # Un cuerpo que no es un objeto (`5`, `null`, una lista) lo rechaza el serializador.
        sent = request.data if isinstance(request.data, Mapping) else {}
        fixed = [field for field in ("code", "currency") if field in sent]
        if fixed:
            raise serializers.ValidationError({field: [IMMUTABLE] for field in fixed})
        wanted = PriceListUpdateSerializer(data=request.data)
        wanted.is_valid(raise_exception=True)
        try:
            price_list = commands.update_price_list(
                _tenant(), price_list_id=price_list_id, **wanted.validated_data
            )
        except PriceList.DoesNotExist:
            raise NotFound from None
        except commands.PriceListIsDefault:
            raise ApiError("PRICE_LIST_IS_DEFAULT", 409, IS_DEFAULT) from None
        except commands.InactiveDefault:
            raise serializers.ValidationError({"is_default": [INACTIVE]}) from None
        except commands.RestrictedDefault:
            raise serializers.ValidationError({"is_default": [RESTRICTED]}) from None
        return Response(PriceListSerializer(price_list).data)
