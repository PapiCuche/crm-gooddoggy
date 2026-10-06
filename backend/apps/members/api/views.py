"""Rutas de tenant de `members`: el estado de un miembro, suspendido o activo (F2-19), y su
sucursal (F2-69)."""

from typing import Any
from uuid import UUID

from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.access.permissions import rbac_errors
from apps.members.services import ACTIVE, SUSPENDED, set_member_branch, set_member_status
from apps.organizations.services import InvalidTransition, UnknownBranch
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
