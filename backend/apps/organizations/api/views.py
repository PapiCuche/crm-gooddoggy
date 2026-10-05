"""Rutas de `organizations`: las de plataforma (`/api/v1/me/…`, sin tenant; ADR-014 §4) y las
de tenant (`…/branches/`, F2-43)."""

from typing import Any

from django.db.models import QuerySet
from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework import generics, serializers
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.organizations.models import Branch
from apps.organizations.selectors import branches, organizations_for_user
from core.api.permissions import Authenticated
from core.api.schema import errors


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


@extend_schema_view(
    get=extend_schema(
        operation_id="branches_list",
        tags=["branches"],
        responses={200: BranchSerializer(many=True), **errors(400, 401, 403, 404)},
    )
)
class BranchesView(generics.ListAPIView):
    """Las sucursales de la organización, activas e inactivas. Paginado por orden de creación
    (ADR-016)."""

    required_permissions = {"GET": "organization.view"}
    serializer_class = BranchSerializer

    def get_queryset(self) -> QuerySet[Branch]:
        return branches()
