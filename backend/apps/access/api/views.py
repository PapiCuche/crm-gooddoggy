"""Rutas de tenant de `access`: el contexto propio (`…/me/`, F2-11) y el directorio de
miembros (`…/members/`, F2-16)."""

from typing import Any

from django.conf import settings
from django.db.models import QuerySet
from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework import generics, serializers
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.access.catalog import Scope
from apps.access.permissions import IsMember, request_context
from apps.access.selectors import memberships, organization_of, role_names, roles_by_membership
from core.api.schema import errors


class MemberUserSerializer(serializers.Serializer[Any]):
    id = serializers.UUIDField()
    email = serializers.CharField()
    first_name = serializers.CharField()
    last_name = serializers.CharField()


class MemberOrganizationSerializer(serializers.Serializer[Any]):
    id = serializers.UUIDField()
    slug = serializers.CharField()
    name = serializers.CharField()


class RoleNameSerializer(serializers.Serializer[Any]):
    code = serializers.CharField()
    name = serializers.CharField()


class GrantSerializer(serializers.Serializer[Any]):
    code = serializers.CharField()
    scopes = serializers.ListField(
        child=serializers.ChoiceField(choices=Scope.choices),
        help_text="Alcances concedidos. Vacío si el permiso no admite alcance.",
    )


class SelfContextSerializer(serializers.Serializer[Any]):
    user = MemberUserSerializer()
    organization = MemberOrganizationSerializer()
    membership_id = serializers.UUIDField()
    roles = RoleNameSerializer(many=True, help_text="Solo para mostrar: no autorizan nada.")
    permissions = GrantSerializer(many=True)


class SelfContextView(APIView):
    """Quién es el usuario en esta organización y qué puede hacer. Informa: no autoriza. La
    interfaz lo usa para construir la navegación; cada ruta sigue comprobando su permiso."""

    permission_classes = [IsMember]

    @extend_schema(
        operation_id="me_context",
        tags=["me"],
        responses={200: SelfContextSerializer, **errors(401, 403, 404)},
    )
    def get(self, request: Request, **kwargs: Any) -> Response:
        ectx = request_context(request)
        assert ectx is not None  # noqa: S101 — `IsMember` ya lo comprobó
        grants = [
            {"code": code, "scopes": sorted(scope for scope in scopes if scope is not None)}
            for code, scopes in sorted(ectx.permissions.items())
        ]
        payload = {
            "user": request.user,
            "organization": organization_of(ectx),
            "membership_id": ectx.membership_id,
            "roles": role_names(ectx),
            "permissions": grants,
        }
        return Response(SelfContextSerializer(payload).data)


class MemberSerializer(serializers.Serializer[Any]):
    id = serializers.UUIDField(help_text="Identificador de la membresía.")
    status = serializers.ChoiceField(choices=settings.MEMBERSHIP_STATUSES)
    joined_at = serializers.DateTimeField()
    user = MemberUserSerializer()
    roles = RoleNameSerializer(many=True, help_text="Solo para mostrar: no autorizan nada.")


@extend_schema_view(
    get=extend_schema(
        operation_id="members_list",
        tags=["members"],
        responses={200: MemberSerializer(many=True), **errors(400, 401, 403, 404)},
    )
)
class MembersView(generics.ListAPIView):
    """Quién pertenece a la organización, en cualquier estado, y con qué roles. Paginado por
    orden de alta (ADR-016)."""

    required_permissions = {"GET": "users.view"}
    serializer_class = MemberSerializer

    def get_queryset(self) -> QuerySet[Any]:
        ectx = request_context(self.request)
        assert ectx is not None  # noqa: S101 — `HasPermission` ya lo comprobó
        return memberships(ectx)

    def list(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        ectx = request_context(request)
        assert ectx is not None  # noqa: S101 — `HasPermission` ya lo comprobó
        page = self.paginate_queryset(self.filter_queryset(self.get_queryset()))
        roles = roles_by_membership(ectx, [member.pk for member in page])  # una consulta, no N
        rows = [
            {
                "id": member.pk,
                "status": member.status,
                "joined_at": member.created_at,
                "user": member.user,
                "roles": roles.get(member.pk, []),
            }
            for member in page
        ]
        return self.get_paginated_response(MemberSerializer(rows, many=True).data)
