"""Rutas de tenant de `members`: el estado de un miembro, suspendido o activo (F2-19)."""

from typing import Any
from uuid import UUID

from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.exceptions import NotFound
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.access.selectors import AccessDenied, Denied
from apps.members.services import ACTIVE, SUSPENDED, set_member_status
from apps.organizations.services import InvalidTransition
from core.api.errors import ApiError
from core.api.schema import errors
from core.tenancy import context


class MemberStatusChangeSerializer(serializers.Serializer[Any]):
    status = serializers.ChoiceField(choices=[ACTIVE, SUSPENDED])


class MemberStatusSerializer(serializers.Serializer[Any]):
    id = serializers.UUIDField(help_text="Identificador de la membresía.")
    status = serializers.ChoiceField(choices=settings.MEMBERSHIP_STATUSES)


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
            set_member_status(tenant, membership_id=membership_id, status=status)
        except ObjectDoesNotExist:
            raise NotFound from None
        except AccessDenied as denied:
            if denied.reason is not Denied.LAST_OWNER:
                raise
            message = "Debe quedar al menos un Owner activo en la organización."
            raise ApiError("LAST_OWNER", 409, message) from None
        except InvalidTransition:
            message = "Solo se suspende a un miembro activo y se reactiva a uno suspendido."
            raise ApiError("INVALID_TRANSITION", 409, message) from None
        return Response(MemberStatusSerializer({"id": membership_id, "status": status}).data)
