"""Rutas de tenant del catálogo: `…/brands/` (F3-01) y `…/categories/` (F3-04), E02-01."""

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
    BrandSlugTaken,
    brand_name,
    brand_slug,
    create_brand,
    delete_brand,
    rename_brand,
)
from apps.catalog.categories import (
    BadParent,
    CategoryHasChildren,
    CategorySlugTaken,
    category_name,
    create_category,
    delete_category,
    move_category,
    rename_category,
)
from apps.catalog.models import Brand, Category
from apps.catalog.selectors import brands, categories
from apps.catalog.slugs import SLUG_MAX, NoSlug
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


class CategorySerializer(serializers.Serializer[Any]):
    id = serializers.UUIDField()
    parent_id = serializers.UUIDField(allow_null=True, help_text="La categoría madre, o `null`.")
    name = serializers.CharField()
    slug = serializers.CharField(help_text="Clave estable, única entre las hermanas.")
    path = serializers.CharField(help_text="La rama con los slugs: `/celulares/iphone/`.")
    depth = serializers.IntegerField(help_text="Nivel en el árbol, desde 1.")


class CategoryCreateSerializer(serializers.Serializer[Any]):
    name = serializers.CharField(max_length=NAME_MAX)
    slug = serializers.CharField(
        max_length=SLUG_MAX,
        required=False,
        help_text="Si no se envía, se forma con el nombre. No cambia después.",
    )
    parent_id = serializers.UUIDField(
        required=False, allow_null=True, help_text="Dentro de qué categoría; sin él, en la raíz."
    )

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        return _clean(attrs, {"name": category_name, "slug": brand_slug, "parent_id": lambda v: v})


class CategoryUpdateSerializer(serializers.Serializer[Any]):
    name = serializers.CharField(max_length=NAME_MAX, required=False)
    parent_id = serializers.UUIDField(
        required=False,
        allow_null=True,
        help_text="Mueve la categoría con toda su rama; `null` la lleva a la raíz.",
    )

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        if not attrs:
            raise serializers.ValidationError("Envía `name`, `parent_id` o los dos.")
        return _clean(attrs, {"name": category_name, "parent_id": lambda v: v})


CATEGORY_TAKEN = "Ya hay una categoría con ese slug dentro de la misma categoría."
HAS_CHILDREN = "La categoría tiene subcategorías: muévelas o bórralas antes."


def _bad_parent(error: BadParent) -> serializers.ValidationError:
    text = f"{error}."
    return serializers.ValidationError({"parent_id": [text[:1].upper() + text[1:]]})


@extend_schema_view(
    get=extend_schema(
        operation_id="categories_list",
        tags=["catalog"],
        responses={200: CategorySerializer(many=True), **errors(400, 401, 403, 404)},
        description="Las categorías de la organización, sin las borradas, paginadas por "
        "creación (ADR-016). El árbol se arma con `parent_id` (o ordenando por `path`).",
    ),
    post=extend_schema(
        operation_id="categories_create",
        tags=["catalog"],
        request=CategoryCreateSerializer,
        responses={201: CategorySerializer, **errors(400, 401, 403, 404, 409)},
        description="Crea una categoría en la raíz o dentro de `parent_id` (hasta 5 niveles). "
        "409 `CATEGORY_SLUG_TAKEN`: la madre ya tiene una con ese slug.",
    ),
)
class CategoriesView(generics.ListAPIView):
    required_permissions = {"GET": "products.view", "POST": "categories.manage"}
    serializer_class = CategorySerializer

    def get_queryset(self) -> QuerySet[Category]:
        return categories()

    def post(self, request: Request, **kwargs: Any) -> Response:
        wanted = CategoryCreateSerializer(data=request.data)
        wanted.is_valid(raise_exception=True)
        try:
            category = create_category(_tenant(), **wanted.validated_data)
        except CategorySlugTaken:
            raise ApiError("CATEGORY_SLUG_TAKEN", 409, CATEGORY_TAKEN) from None
        except BadParent as error:
            raise _bad_parent(error) from None
        except NoSlug as error:
            text = f"{error}."
            raise serializers.ValidationError({"slug": [text[:1].upper() + text[1:]]}) from None
        return Response(CategorySerializer(category).data, status=201)


class CategoryView(APIView):
    """Una categoría: `PATCH` la renombra y/o la mueve con su rama; `DELETE` la borra."""

    required_permissions = {"PATCH": "categories.manage", "DELETE": "categories.manage"}

    @extend_schema(
        operation_id="categories_update",
        tags=["catalog"],
        request=CategoryUpdateSerializer,
        responses={200: CategorySerializer, **errors(400, 401, 403, 404, 409)},
        description="`name` la renombra (el slug no cambia). `parent_id` la mueve con toda su "
        "rama, sin entrar en sí misma ni pasar de 5 niveles. 404: no es de la organización o "
        "está borrada.",
    )
    def patch(self, request: Request, category_id: UUID, **kwargs: Any) -> Response:
        wanted = CategoryUpdateSerializer(data=request.data)
        wanted.is_valid(raise_exception=True)
        tenant, data = _tenant(), wanted.validated_data
        try:
            # Renombrar y mover, o nada: una respuesta de error deshace la transacción de la
            # petición (`tenant_scope`), también lo que ya se hubiera renombrado.
            if "name" in data:
                category = rename_category(tenant, category_id=category_id, name=data["name"])
            if "parent_id" in data:
                category = move_category(
                    tenant, category_id=category_id, parent_id=data["parent_id"]
                )
        except Category.DoesNotExist:
            raise NotFound from None
        except CategorySlugTaken:
            raise ApiError("CATEGORY_SLUG_TAKEN", 409, CATEGORY_TAKEN) from None
        except BadParent as error:
            raise _bad_parent(error) from None
        return Response(CategorySerializer(category).data)

    @extend_schema(
        operation_id="categories_delete",
        tags=["catalog"],
        responses={204: None, **errors(401, 403, 404, 409)},
        description="Borra la categoría (borrado lógico). 409 `CATEGORY_HAS_CHILDREN`: tiene "
        "subcategorías. 404: no es de la organización o ya está borrada.",
    )
    def delete(self, request: Request, category_id: UUID, **kwargs: Any) -> Response:
        try:
            delete_category(_tenant(), category_id=category_id)
        except Category.DoesNotExist:
            raise NotFound from None
        except CategoryHasChildren:
            raise ApiError("CATEGORY_HAS_CHILDREN", 409, HAS_CHILDREN) from None
        return Response(status=204)
