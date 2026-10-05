"""Rutas de tenant de `access`: el contexto propio (`…/me/`, F2-11), los directorios de
miembros (`…/members/`, F2-16) y de roles (`…/roles/`, F2-22) y los roles de un miembro
(`…/members/{id}/roles/{role_id}/`, F2-25)."""

from collections.abc import Callable
from typing import Any
from uuid import UUID

from django.conf import settings
from django.db.models import QuerySet
from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework import generics, serializers
from rest_framework.exceptions import NotFound
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.access import directory
from apps.access.catalog import Scope
from apps.access.models import Role
from apps.access.permissions import IsMember, rbac_errors, request_context
from apps.access.selectors import (
    UnknownPermission,
    memberships,
    organization_of,
    role_names,
    roles_by_membership,
)
from apps.access.services import (
    DESCRIPTION_MAX,
    NAME_MAX,
    RoleNameTaken,
    ScopeMismatch,
    assign_role,
    create_role,
    grant_permission,
    remove_role,
    role_name,
)
from core.api.errors import ApiError
from core.api.schema import errors
from core.tenancy import context


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


class MemberRoleSerializer(RoleNameSerializer):
    id = serializers.UUIDField(help_text="El que piden las rutas que asignan o quitan el rol.")


class MemberSerializer(serializers.Serializer[Any]):
    id = serializers.UUIDField(help_text="Identificador de la membresía.")
    status = serializers.ChoiceField(choices=settings.MEMBERSHIP_STATUSES)
    joined_at = serializers.DateTimeField()
    user = MemberUserSerializer()
    roles = MemberRoleSerializer(
        many=True,
        help_text="No autorizan nada. `id` nombra el rol en las rutas que lo asignan o quitan.",
    )


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


class RoleCreateSerializer(serializers.Serializer[Any]):
    name = serializers.CharField(max_length=NAME_MAX)
    description = serializers.CharField(
        max_length=DESCRIPTION_MAX, required=False, allow_blank=True
    )

    def validate_name(self, value: str) -> str:
        try:
            return role_name(value)  # el mismo criterio que el servicio, como un 400
        except ValueError as error:
            raise serializers.ValidationError(str(error)) from None

    def validate_description(self, value: str) -> str:
        if not value.isprintable():  # saltos de línea, nulos, controles
            raise serializers.ValidationError("Solo texto imprimible.")
        return value


@extend_schema_view(
    get=extend_schema(
        operation_id="roles_list",
        tags=["roles"],
        responses={200: RoleSerializer(many=True), **errors(400, 401, 403, 404)},
    ),
    post=extend_schema(
        operation_id="roles_create",
        tags=["roles"],
        request=RoleCreateSerializer,
        responses={201: RoleSerializer, **errors(400, 401, 403, 404, 409)},
        description="Crea un rol propio, vacío: sin permisos y sin miembros. El código lo "
        "genera el servidor. 409 `ROLE_NAME_TAKEN`: ya hay en la organización un rol cuyo "
        "nombre se lee igual (mayúsculas, espacios repetidos, formas Unicode). 409 "
        "`LAST_OWNER`: la organización no tiene rol Owner y no admite ningún cambio.",
    ),
)
class RolesView(generics.ListAPIView):
    """Los roles de la organización, con lo que concede cada uno y cuántos miembros lo tienen.
    Paginado por orden de creación (ADR-016). `POST` crea un rol propio (F2-29)."""

    required_permissions = {"GET": "roles.view", "POST": "roles.manage"}
    serializer_class = RoleSerializer

    def post(self, request: Request, **kwargs: Any) -> Response:
        wanted = RoleCreateSerializer(data=request.data)
        wanted.is_valid(raise_exception=True)
        tenant = context.current()
        assert tenant is not None  # noqa: S101 — `HasPermission` ya lo comprobó
        try:
            with rbac_errors():
                role = create_role(tenant, **wanted.validated_data)
        except RoleNameTaken:
            message = "Ya existe un rol con ese nombre en la organización."
            raise ApiError("ROLE_NAME_TAKEN", 409, message) from None
        row = {
            "id": role.pk,
            "code": role.code,
            "name": role.name,
            "description": role.description,
            "is_system": role.is_system,
            "permissions": [],
            "members": 0,
        }
        return Response(RoleSerializer(row).data, status=201)

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


class MemberRoleView(APIView):
    """Un rol de un miembro: `PUT` lo asigna y `DELETE` se lo quita. Las reglas son las de
    `access.services` (ADR-003 §5): nadie cambia sus propios roles; el actor cubre todas las
    concesiones del rol; lo sensible, solo un Owner; siempre queda un Owner activo."""

    required_permissions = {"PUT": "users.manage", "DELETE": "users.manage"}

    def _change(self, service: Callable[..., None], membership_id: UUID, role_id: UUID) -> Response:
        tenant = context.current()
        assert tenant is not None  # noqa: S101 — `HasPermission` ya lo comprobó
        with rbac_errors():
            service(tenant, membership_id=membership_id, role_id=role_id)
        return Response(status=204)

    @extend_schema(
        operation_id="members_roles_assign",
        tags=["members"],
        request=None,
        responses={204: None, **errors(401, 403, 404, 409)},
        description="Asigna el rol al miembro. Repetirlo no cambia nada. 403: sin "
        "`users.manage`, uno mismo, o un rol que el actor no cubre. 404: el miembro o el rol "
        "no son de la organización. 409 `LAST_OWNER`: la organización no tiene rol Owner y "
        "no admite ningún cambio.",
    )
    def put(self, request: Request, membership_id: UUID, role_id: UUID, **kwargs: Any) -> Response:
        return self._change(assign_role, membership_id, role_id)

    @extend_schema(
        operation_id="members_roles_remove",
        tags=["members"],
        responses={204: None, **errors(401, 403, 404, 409)},
        description="Quita el rol al miembro. 403: como al asignarlo. 404: el miembro no "
        "tiene ese rol, o no son de la organización. 409 `LAST_OWNER`: sería el último Owner "
        "activo, o la organización no tiene rol Owner.",
    )
    def delete(
        self, request: Request, membership_id: UUID, role_id: UUID, **kwargs: Any
    ) -> Response:
        return self._change(remove_role, membership_id, role_id)


class GrantScopeSerializer(serializers.Serializer[Any]):
    scope = serializers.ChoiceField(
        choices=Scope.choices,
        required=False,
        allow_null=True,
        default=None,
        help_text="Alcance de la concesión. Se omite, o es nulo, en un permiso sin alcance.",
        error_messages={"invalid_choice": "No es un alcance válido."},  # sin repetir lo recibido
    )


class RolePermissionView(APIView):
    """Un permiso de un rol: `PUT` lo deja concedido con ese alcance. Las reglas son las de
    `access.services.grant_permission` (ADR-003 §5): nadie concede lo que no tiene ni con más
    alcance; lo sensible, solo un Owner; nadie cambia un rol que tiene asignado; el rol Owner
    no se edita."""

    required_permissions = {"PUT": "roles.manage"}

    @extend_schema(
        operation_id="roles_permissions_grant",
        tags=["roles"],
        request=GrantScopeSerializer,
        responses={204: None, **errors(400, 401, 403, 404, 409)},
        description="Deja el rol con ese permiso y ese alcance: lo concede o cambia el alcance "
        "que tenía. Repetirlo no cambia nada. Los miembros del rol lo reciben en su siguiente "
        "petición. 400: el alcance no corresponde al permiso. 403: sin `roles.manage`, un rol "
        "que el actor tiene asignado, el rol Owner, una concesión (la nueva o la anterior) que "
        "el actor no cubre, o un permiso sensible si el actor no es Owner. 404: el rol no es "
        "de la organización o el permiso no existe. 409 `LAST_OWNER`: la organización no "
        "tiene rol Owner y no admite ningún cambio.",
    )
    def put(self, request: Request, role_id: UUID, code: str, **kwargs: Any) -> Response:
        wanted = GrantScopeSerializer(data=request.data)
        wanted.is_valid(raise_exception=True)
        tenant = context.current()
        assert tenant is not None  # noqa: S101 — `HasPermission` ya lo comprobó
        try:
            with rbac_errors():
                grant_permission(tenant, role_id=role_id, code=code, **wanted.validated_data)
        except UnknownPermission:
            raise NotFound from None
        except ScopeMismatch:
            message = "El alcance no corresponde a este permiso."
            raise serializers.ValidationError({"scope": message}) from None
        return Response(status=204)
