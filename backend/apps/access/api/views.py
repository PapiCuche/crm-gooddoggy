"""Rutas de tenant de `access`: el contexto propio (`…/me/`, F2-11) y los directorios de
miembros (`…/members/`, F2-16) y de roles (`…/roles/`, F2-22)."""

from typing import Any

from django.conf import settings
from django.db.models import QuerySet
from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework import generics, serializers
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.access import directory
from apps.access.catalog import Scope
from apps.access.models import Role
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


class RoleGrantSerializer(serializers.Serializer[Any]):
    code = serializers.CharField(help_text="Código del permiso.")
    scope = serializers.ChoiceField(
        choices=Scope.choices,
        allow_null=True,
        help_text="Alcance concedido; `null` si el permiso no admite alcance.",
    )


class RoleSerializer(serializers.Serializer[Any]):
    id = serializers.UUIDField()
    code = serializers.CharField()
    name = serializers.CharField()
    description = serializers.CharField(allow_blank=True)
    is_system = serializers.BooleanField(help_text="Nació de una plantilla del producto.")
    permissions = RoleGrantSerializer(many=True)
    members = serializers.IntegerField(help_text="Membresías con este rol, en cualquier estado.")


@extend_schema_view(
    get=extend_schema(
        operation_id="roles_list",
        tags=["roles"],
        responses={200: RoleSerializer(many=True), **errors(400, 401, 403, 404)},
    )
)
class RolesView(generics.ListAPIView):
    """Los roles de la organización, con lo que concede cada uno y cuántos miembros lo tienen.
    Paginado por orden de creación (ADR-016)."""

    required_permissions = {"GET": "roles.view"}
    serializer_class = RoleSerializer

    def get_queryset(self) -> QuerySet[Role]:
        ectx = request_context(self.request)
        assert ectx is not None  # noqa: S101 — `HasPermission` ya lo comprobó
        return directory.roles(ectx)

    def list(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        ectx = request_context(request)
        assert ectx is not None  # noqa: S101 — `HasPermission` ya lo comprobó
        page = self.paginate_queryset(self.filter_queryset(self.get_queryset()))
        ids = [role.pk for role in page]
        grants, members = directory.grants_by_role(ectx, ids), directory.members_by_role(ectx, ids)
        rows = [
            {
                "id": role.pk,
                "code": role.code,
                "name": role.name,
                "description": role.description,
                "is_system": role.is_system,
                "permissions": grants.get(role.pk, []),
                "members": members.get(role.pk, 0),
            }
            for role in page
        ]
        return self.get_paginated_response(RoleSerializer(rows, many=True).data)
