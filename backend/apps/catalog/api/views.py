"""Rutas de tenant del catálogo: `…/brands/` (F3-01, E02-01)."""

from typing import Any
from uuid import UUID

from django.db.models import QuerySet
from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework import generics, serializers
from rest_framework.exceptions import NotFound
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.catalog.brands import (
    NAME_MAX,
    SLUG_MAX,
    BrandSlugTaken,
    NoSlug,
    brand_name,
    brand_slug,
    create_brand,
    delete_brand,
    rename_brand,
)
from apps.catalog.models import Brand
from apps.catalog.selectors import brands
from core.api.errors import ApiError
from core.api.schema import errors
from core.tenancy import context

TAKEN = "Ya existe una marca con ese slug en la organización."


class BrandSerializer(serializers.Serializer[Any]):
    id = serializers.UUIDField()
    name = serializers.CharField()
    slug = serializers.CharField(help_text="Clave estable de la marca, única en la organización.")


def _clean(attrs: dict[str, Any], cleaners: dict[str, Any]) -> dict[str, Any]:
    """El mismo criterio que el comando, como un 400 por campo."""
    problems = {}
    for field, value in attrs.items():
        try:
            attrs[field] = cleaners[field](value)
        except ValueError as error:
            text = f"{error}."
            problems[field] = [text[:1].upper() + text[1:]]
    if problems:
        raise serializers.ValidationError(problems)
    return attrs


class BrandCreateSerializer(serializers.Serializer[Any]):
    name = serializers.CharField(max_length=NAME_MAX)
    slug = serializers.CharField(
        max_length=SLUG_MAX,
        required=False,
        help_text="Si no se envía, se forma con el nombre. No cambia después.",
    )

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        return _clean(attrs, {"name": brand_name, "slug": brand_slug})


class BrandUpdateSerializer(serializers.Serializer[Any]):
    name = serializers.CharField(max_length=NAME_MAX)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        return _clean(attrs, {"name": brand_name})


def _tenant() -> Any:
    tenant = context.current()
    assert tenant is not None  # noqa: S101 — `HasPermission` ya lo comprobó
    return tenant


@extend_schema_view(
    get=extend_schema(
        operation_id="brands_list",
        tags=["catalog"],
        responses={200: BrandSerializer(many=True), **errors(400, 401, 403, 404)},
    ),
    post=extend_schema(
        operation_id="brands_create",
        tags=["catalog"],
        request=BrandCreateSerializer,
        responses={201: BrandSerializer, **errors(400, 401, 403, 404, 409)},
        description="Crea una marca. Sin `slug`, se forma con el nombre. 409 "
        "`BRAND_SLUG_TAKEN`: ya hay una marca con ese slug en la organización.",
    ),
)
class BrandsView(generics.ListAPIView):
    """Las marcas de la organización, sin las borradas. Paginado por orden de creación
    (ADR-016). `POST` crea una."""

    required_permissions = {"GET": "products.view", "POST": "brands.manage"}
    serializer_class = BrandSerializer

    def get_queryset(self) -> QuerySet[Brand]:
        return brands()

    def post(self, request: Request, **kwargs: Any) -> Response:
        wanted = BrandCreateSerializer(data=request.data)
        wanted.is_valid(raise_exception=True)
        try:
            brand = create_brand(_tenant(), **wanted.validated_data)
        except BrandSlugTaken:
            raise ApiError("BRAND_SLUG_TAKEN", 409, TAKEN) from None
        except NoSlug as error:
            text = f"{error}."
            raise serializers.ValidationError({"slug": [text[:1].upper() + text[1:]]}) from None
        return Response(BrandSerializer(brand).data, status=201)


class BrandView(APIView):
    """Una marca: `PATCH` la renombra (el slug no cambia) y `DELETE` la borra."""

    required_permissions = {"PATCH": "brands.manage", "DELETE": "brands.manage"}

    @extend_schema(
        operation_id="brands_update",
        tags=["catalog"],
        request=BrandUpdateSerializer,
        responses={200: BrandSerializer, **errors(400, 401, 403, 404)},
        description="Cambia el nombre. Enviar el que ya tiene no escribe nada. 404: la marca "
        "no es de la organización o está borrada.",
    )
    def patch(self, request: Request, brand_id: UUID, **kwargs: Any) -> Response:
        wanted = BrandUpdateSerializer(data=request.data)
        wanted.is_valid(raise_exception=True)
        try:
            brand = rename_brand(_tenant(), brand_id=brand_id, **wanted.validated_data)
        except Brand.DoesNotExist:
            raise NotFound from None
        return Response(BrandSerializer(brand).data)

    @extend_schema(
        operation_id="brands_delete",
        tags=["catalog"],
        responses={204: None, **errors(401, 403, 404)},
        description="Borra la marca (borrado lógico) y libera su slug. 404: no es de la "
        "organización o ya está borrada.",
    )
    def delete(self, request: Request, brand_id: UUID, **kwargs: Any) -> Response:
        try:
            delete_brand(_tenant(), brand_id=brand_id)
        except Brand.DoesNotExist:
            raise NotFound from None
        return Response(status=204)
