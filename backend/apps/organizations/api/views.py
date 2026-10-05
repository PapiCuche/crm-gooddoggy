"""Rutas de `organizations`: las de plataforma (`/api/v1/me/…`, sin tenant; ADR-014 §4) y las
de tenant (`…/branches/`, F2-43 y F2-44)."""

from typing import Any
from uuid import UUID

from django.db.models import QuerySet
from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework import generics, serializers
from rest_framework.exceptions import NotFound
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.organizations.branches import (
    CLEAN,
    CODE_MAX,
    NAME_MAX,
    TEXT_MAX,
    TIMEZONE_MAX,
    BranchCodeTaken,
    create_branch,
    update_branch,
)
from apps.organizations.models import Branch
from apps.organizations.selectors import branches, organizations_for_user
from core.api.errors import ApiError
from core.api.permissions import Authenticated
from core.api.schema import errors
from core.tenancy import context


class OrganizationSummarySerializer(serializers.Serializer[Any]):
    id = serializers.UUIDField()
    slug = serializers.CharField()
    name = serializers.CharField()


class MyOrganizationsView(APIView):
    permission_classes = [Authenticated]

    @extend_schema(
        operation_id="me_organizations",
        tags=["me"],
        responses={200: OrganizationSummarySerializer(many=True), **errors(401)},
    )
    def get(self, request: Request) -> Response:
        """Organizaciones del usuario: membresía activa y organización no suspendida. No abre
        un `tenant_scope`: lee con `user_scope`, que solo deja ver las membresías propias."""
        found = organizations_for_user(request.user)
        return Response(OrganizationSummarySerializer(found, many=True).data)


class BranchSerializer(serializers.Serializer[Any]):
    id = serializers.UUIDField()
    code = serializers.CharField(help_text="Clave corta de la sucursal, única en la organización.")
    name = serializers.CharField()
    address = serializers.CharField(allow_blank=True)
    district = serializers.CharField(allow_blank=True)
    city = serializers.CharField(allow_blank=True)
    phone = serializers.CharField(allow_blank=True)
    timezone = serializers.CharField(help_text="Zona horaria, por su nombre IANA.")
    is_active = serializers.BooleanField()


class BranchUpdateSerializer(serializers.Serializer[Any]):
    """Lo que se envía cambia; lo que no, se queda como está."""

    name = serializers.CharField(max_length=NAME_MAX, required=False)
    address = serializers.CharField(
        max_length=TEXT_MAX["address"], required=False, allow_blank=True
    )
    district = serializers.CharField(
        max_length=TEXT_MAX["district"], required=False, allow_blank=True
    )
    city = serializers.CharField(max_length=TEXT_MAX["city"], required=False, allow_blank=True)
    phone = serializers.CharField(max_length=TEXT_MAX["phone"], required=False, allow_blank=True)
    timezone = serializers.CharField(
        max_length=TIMEZONE_MAX, required=False, help_text="Zona horaria, por su nombre IANA."
    )
    is_active = serializers.BooleanField(required=False)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        """El mismo criterio que los comandos, como un 400 por campo."""
        problems = {}
        for field, value in attrs.items():
            try:
                attrs[field] = CLEAN[field](value)
            except ValueError as error:
                text = f"{error}."
                problems[field] = [text[:1].upper() + text[1:]]
        if problems:
            raise serializers.ValidationError(problems)
        return attrs


class BranchCreateSerializer(BranchUpdateSerializer):
    code = serializers.CharField(
        max_length=CODE_MAX, help_text="Se guarda en mayúsculas y no cambia después."
    )
    name = serializers.CharField(max_length=NAME_MAX)
    is_active = None  # nace activa


TAKEN = "Ya existe una sucursal con ese código en la organización."


@extend_schema_view(
    get=extend_schema(
        operation_id="branches_list",
        tags=["branches"],
        responses={200: BranchSerializer(many=True), **errors(400, 401, 403, 404)},
    ),
    post=extend_schema(
        operation_id="branches_create",
        tags=["branches"],
        request=BranchCreateSerializer,
        responses={201: BranchSerializer, **errors(400, 401, 403, 404, 409)},
        description="Crea una sucursal activa. `timezone` es `America/Lima` si no se envía. "
        "409 `BRANCH_CODE_TAKEN`: ya hay en la organización una sucursal con ese código.",
    ),
)
class BranchesView(generics.ListAPIView):
    """Las sucursales de la organización, activas e inactivas. Paginado por orden de creación
    (ADR-016). `POST` crea una (F2-44)."""

    required_permissions = {"GET": "organization.view", "POST": "branches.manage"}
    serializer_class = BranchSerializer

    def get_queryset(self) -> QuerySet[Branch]:
        return branches()

    def post(self, request: Request, **kwargs: Any) -> Response:
        wanted = BranchCreateSerializer(data=request.data)
        wanted.is_valid(raise_exception=True)
        tenant = context.current()
        assert tenant is not None  # noqa: S101 — `HasPermission` ya lo comprobó
        try:
            branch = create_branch(tenant, **wanted.validated_data)
        except BranchCodeTaken:
            raise ApiError("BRANCH_CODE_TAKEN", 409, TAKEN) from None
        return Response(BranchSerializer(branch).data, status=201)


class BranchView(APIView):
    """Una sucursal: `PATCH` cambia sus datos o la desactiva (`is_active`). No hay borrado."""

    required_permissions = {"PATCH": "branches.manage"}

    @extend_schema(
        operation_id="branches_update",
        tags=["branches"],
        request=BranchUpdateSerializer,
        responses={200: BranchSerializer, **errors(400, 401, 403, 404)},
        description="Cambia lo que se envía; lo demás se queda como está, y el código no "
        "cambia. 404: la sucursal no es de la organización.",
    )
    def patch(self, request: Request, branch_id: UUID, **kwargs: Any) -> Response:
        wanted = BranchUpdateSerializer(data=request.data)
        wanted.is_valid(raise_exception=True)
        tenant = context.current()
        assert tenant is not None  # noqa: S101 — `HasPermission` ya lo comprobó
        try:
            branch = update_branch(tenant, branch_id=branch_id, **wanted.validated_data)
        except Branch.DoesNotExist:
            raise NotFound from None
        return Response(BranchSerializer(branch).data)
