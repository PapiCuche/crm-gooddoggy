"""Rutas de tenant de `members`: el estado de un miembro, suspendido o activo (F2-19), su
sucursal (F2-69) y las invitaciones a la organización (F2-80)."""

from typing import Any
from uuid import UUID

from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.access.permissions import rbac_errors
from apps.access.services import UnknownRole
from apps.members.services import (
    ACTIVE,
    SUSPENDED,
    InvalidEmail,
    invite,
    set_member_branch,
    set_member_status,
)
from apps.organizations.services import (
    INVITATION_ROLES_MAX,
    INVITATION_STATUSES,
    AlreadyMember,
    InvalidTransition,
    InvitationPending,
    TooManyInvitations,
    TooManyPending,
    UnknownBranch,
)
from core.api.errors import ApiError
from core.api.schema import errors
from core.tenancy import context


class MemberStatusChangeSerializer(serializers.Serializer[Any]):
    status = serializers.ChoiceField(choices=[ACTIVE, SUSPENDED])


class MemberStatusSerializer(serializers.Serializer[Any]):
    id = serializers.UUIDField(help_text="Identificador de la membresía.")
    status = serializers.ChoiceField(choices=[ACTIVE, SUSPENDED])


class MemberStatusView(APIView):
    """Suspende (`SUSPENDED`) o reactiva (`ACTIVE`) a un miembro. Repetir la petición no cambia
    nada. 403: sin `users.manage`, uno mismo, o un miembro con un rol que el actor no podría
    asignar. 409 `LAST_OWNER`: sería el último Owner activo. 409 `INVALID_TRANSITION`: la
    membresía está invitada o dada de baja."""

    required_permissions = {"PUT": "users.manage"}

    @extend_schema(
        operation_id="members_set_status",
        tags=["members"],
        request=MemberStatusChangeSerializer,
        responses={200: MemberStatusSerializer, **errors(400, 401, 403, 404, 409)},
    )
    def put(self, request: Request, membership_id: UUID, **kwargs: Any) -> Response:
        wanted = MemberStatusChangeSerializer(data=request.data)
        wanted.is_valid(raise_exception=True)
        status = wanted.validated_data["status"]
        tenant = context.current()
        assert tenant is not None  # noqa: S101 — `HasPermission` ya lo comprobó
        try:
            with rbac_errors():
                set_member_status(tenant, membership_id=membership_id, status=status)
        except InvalidTransition:
            message = "Solo se suspende a un miembro activo y se reactiva a uno suspendido."
            raise ApiError("INVALID_TRANSITION", 409, message) from None
        return Response(MemberStatusSerializer({"id": membership_id, "status": status}).data)


class MemberBranchChangeSerializer(serializers.Serializer[Any]):
    branch_id = serializers.UUIDField(
        allow_null=True,
        help_text="La sucursal de la organización, o `null` para dejarlo sin ninguna.",
    )


# La misma forma que `default_branch` en el directorio de miembros (`apps.access`, F2-70).
class MemberBranchRefSerializer(serializers.Serializer[Any]):
    id = serializers.UUIDField()
    code = serializers.CharField()
    name = serializers.CharField()


class MemberBranchSerializer(serializers.Serializer[Any]):
    id = serializers.UUIDField(help_text="Identificador de la membresía.")
    default_branch = MemberBranchRefSerializer(allow_null=True)


UNKNOWN_BRANCH = "No es una sucursal de esta organización."


class MemberBranchView(APIView):
    """Asigna a un miembro su sucursal, la cambia o se la quita (`null`). Su sucursal es lo que
    alcanza una concesión con alcance `BRANCH`: las reglas son las de suspenderlo. Repetir la
    petición no cambia nada. 400: `branch_id` no es una sucursal de la organización. 403: sin
    `users.manage`, uno mismo, o un miembro con un rol que el actor no podría asignar. 409
    `LAST_OWNER`: la organización no tiene rol Owner y no admite ningún cambio."""

    required_permissions = {"PUT": "users.manage"}

    @extend_schema(
        operation_id="members_set_branch",
        tags=["members"],
        request=MemberBranchChangeSerializer,
        responses={200: MemberBranchSerializer, **errors(400, 401, 403, 404, 409)},
    )
    def put(self, request: Request, membership_id: UUID, **kwargs: Any) -> Response:
        wanted = MemberBranchChangeSerializer(data=request.data)
        wanted.is_valid(raise_exception=True)
        tenant = context.current()
        assert tenant is not None  # noqa: S101 — `HasPermission` ya lo comprobó
        try:
            with rbac_errors():
                branch = set_member_branch(
                    tenant,
                    membership_id=membership_id,
                    branch_id=wanted.validated_data["branch_id"],
                )
        except UnknownBranch:
            raise serializers.ValidationError({"branch_id": [UNKNOWN_BRANCH]}) from None
        row = {"id": membership_id, "default_branch": branch._asdict() if branch else None}
        return Response(MemberBranchSerializer(row).data)


class InviteSerializer(serializers.Serializer[Any]):
    email = serializers.CharField(max_length=254, help_text="El correo de la persona invitada.")
    role_ids = serializers.ListField(
        child=serializers.UUIDField(),
        min_length=1,
        max_length=INVITATION_ROLES_MAX,
        help_text="Los roles de la organización que tendrá al aceptar, sin repetir.",
    )

    def validate_role_ids(self, value: list[UUID]) -> list[UUID]:
        if len(set(value)) != len(value):
            raise serializers.ValidationError("Un rol está repetido.")
        return value


class InvitationSerializer(serializers.Serializer[Any]):
    id = serializers.UUIDField()
    email = serializers.CharField(help_text="El correo invitado, en la forma en que se guarda.")
    role_ids = serializers.ListField(child=serializers.UUIDField())
    status = serializers.ChoiceField(choices=INVITATION_STATUSES)
    expires_at = serializers.DateTimeField()


# Lo que `create_invitation` rechaza → código, estado y texto de la respuesta.
_REFUSED = {
    AlreadyMember: ("ALREADY_MEMBER", 409, "Ese correo ya es miembro de la organización."),
    InvitationPending: ("INVITATION_PENDING", 409, "Ese correo ya tiene una invitación pendiente."),
    TooManyPending: ("INVITATION_LIMIT", 409, "Hay demasiadas invitaciones pendientes."),
    TooManyInvitations: ("RATE_LIMITED", 429, "Demasiadas invitaciones en las últimas 24 horas."),
}


class InvitationsView(APIView):
    """Deja creada la invitación de una persona, pendiente y con los roles que tendrá al
    aceptar. Todavía no envía el correo. 400: correo al que no se puede escribir, o `role_ids`
    vacío, con un rol repetido o que no es de la organización. 403: sin `users.invite` y
    `users.manage`, o con un rol que el actor no podría asignar. 409: `ALREADY_MEMBER`,
    `INVITATION_PENDING` o `INVITATION_LIMIT` (50 pendientes). 429: 100 invitaciones en 24 h."""

    required_permissions = {"POST": ("users.invite", "users.manage")}

    @extend_schema(
        operation_id="invitations_create",
        tags=["members"],
        request=InviteSerializer,
        responses={201: InvitationSerializer, **errors(400, 401, 403, 404, 409, 429)},
    )
    def post(self, request: Request, **kwargs: Any) -> Response:
        wanted = InviteSerializer(data=request.data)
        wanted.is_valid(raise_exception=True)
        tenant = context.current()
        assert tenant is not None  # noqa: S101 — `HasPermission` ya lo comprobó
        try:
            with rbac_errors():
                invitation = invite(tenant, **wanted.validated_data)
        except InvalidEmail:
            message = "No es una dirección de correo a la que se pueda invitar."
            raise serializers.ValidationError({"email": [message]}) from None
        except UnknownRole:
            message = "No es un rol de esta organización."
            raise serializers.ValidationError({"role_ids": [message]}) from None
        except tuple(_REFUSED) as refused:
            raise ApiError(*_REFUSED[type(refused)]) from None
        return Response(InvitationSerializer(invitation._asdict()).data, status=201)
