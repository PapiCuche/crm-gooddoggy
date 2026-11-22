"""`POST`/`DELETE …/conversations/{id}/human-request/` (F8-08n; ADR-034 §8): pedir (`AGENT`) o
cerrar la solicitud de atención humana, con `conversations.view` y su alcance (si no, el mismo 404);
idempotentes. No envía nada al cliente ni asigna la conversación.

`PUT …/conversations/{id}/ai-exclusion/` (F8-13): excluirla de la IA (`conversations.view` y su
alcance) o volver a incluirla (además `ai.manage` y el segundo factor reciente), con su versión."""

from typing import Any
from uuid import UUID

from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework import serializers
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.access.permissions import rbac_errors, request_context
from apps.access.selectors import has_permission
from apps.conversations import attention, policy
from apps.conversations.models import Conversation, HumanRequest
from core.api.errors import PERMISSION_DENIED, ApiError
from core.api.schema import errors
from core.api.step_up import require_recent_mfa
from core.api.strict import Closed, Flag, Whole

CLOSED = "La conversación está cerrada: ya no se puede pedir que la atienda una persona."


class HumanRequestSerializer(serializers.Serializer[Any]):
    reason = serializers.ChoiceField(
        source="human_request_reason",
        choices=HumanRequest.choices,
        help_text="Del copiloto: `ASKED_FOR_PERSON`, `UPSET` o `MODEL`; `AGENT`: quien atiende.",
    )
    requested_at = serializers.DateTimeField(source="human_requested_at")
    requested_by_user_id = serializers.UUIDField(source="human_requested_by_user_id")

    def to_representation(self, instance: Conversation) -> dict[str, Any] | None:
        if instance.human_requested_at is None:
            return None
        return super().to_representation(instance)  # type: ignore[no-any-return]


def human_request() -> HumanRequestSerializer:
    return HumanRequestSerializer(
        source="*",
        allow_null=True,
        help_text="Alguien pidió que una persona la atienda; `null` si no. Se cierra cuando una "
        "persona responde al cliente o la toma, o a mano.",
    )


class HumanRequestStateSerializer(serializers.Serializer[Any]):
    id = serializers.UUIDField()
    human_request = human_request()


@extend_schema_view(
    post=extend_schema(
        operation_id="conversation_human_request_create",
        tags=["conversations"],
        request=None,
        responses={200: HumanRequestStateSerializer, **errors(401, 403, 404, 409)},
        description="Pide que una persona la atienda (`AGENT`); una abierta no cambia. Nada sale "
        "al cliente. 409 `CONVERSATION_CLOSED`; 404: no existe o fuera del alcance.",
    ),
    delete=extend_schema(
        operation_id="conversation_human_request_close",
        tags=["conversations"],
        responses={200: HumanRequestStateSerializer, **errors(401, 403, 404)},
        description="Cierra la solicitud; sin una abierta no cambia nada. 404: no existe o fuera "
        "del alcance.",
    ),
)
class HumanRequestView(APIView):
    required_permissions = {"POST": "conversations.view", "DELETE": "conversations.view"}

    def post(self, request: Request, conversation_id: UUID, **kwargs: Any) -> Response:
        try:
            with rbac_errors():  # el comando vuelve a comprobar permiso y alcance con la fila
                convo, _ = attention.request_human(
                    request.tenant, conversation_id=conversation_id, reason=HumanRequest.AGENT
                )
        except policy.ConversationClosed:
            raise ApiError("CONVERSATION_CLOSED", 409, CLOSED) from None
        return Response(HumanRequestStateSerializer(convo).data)

    def delete(self, request: Request, conversation_id: UUID, **kwargs: Any) -> Response:
        with rbac_errors():
            convo, _ = attention.close_request(request.tenant, conversation_id=conversation_id)
        return Response(HumanRequestStateSerializer(convo).data)


STALE = "Otra persona cambió esta conversación. Actualiza y vuelve a intentarlo."


class AiExclusionInputSerializer(Closed, serializers.Serializer[Any]):
    excluded = Flag(help_text="`true`: excluirla de la IA; `false`: volver a incluirla.")
    version = Whole(min_value=1, max_value=2**31 - 1, help_text="La leída; si cambió, 409.")


class AiExclusionStateSerializer(serializers.Serializer[Any]):
    id = serializers.UUIDField()
    ai_excluded_at = serializers.DateTimeField(allow_null=True)
    version = serializers.IntegerField()


@extend_schema_view(
    put=extend_schema(
        operation_id="conversation_ai_exclusion_set",
        tags=["conversations"],
        request=AiExclusionInputSerializer,
        responses={200: AiExclusionStateSerializer, **errors(400, 401, 403, 404, 409)},
        description="Excluye la conversación de la IA (`conversations.view` y su alcance): el "
        "copiloto ya no la lee ni la manda a un proveedor (409 `AI_EXCLUDED`). Volver a incluirla "
        "pide además `ai.manage` y el segundo factor reciente (403 `STEP_UP_REQUIRED`). La misma "
        "marca no cambia nada. 409 `CONVERSATION_VERSION_CONFLICT`; 404: no existe o fuera del "
        "alcance.",
    ),
)
class AiExclusionView(APIView):
    required_permissions = {"PUT": "conversations.view"}

    def put(self, request: Request, conversation_id: UUID, **kwargs: Any) -> Response:
        wanted = AiExclusionInputSerializer(data=request.data)
        wanted.is_valid(raise_exception=True)
        data = wanted.validated_data
        if not data["excluded"]:  # incluir: el permiso y después el segundo factor
            ectx = request_context(request)
            if ectx is None or not has_permission(ectx, attention.MANAGE_AI):
                raise ApiError(PERMISSION_DENIED, 403)
            require_recent_mfa(request)
        try:
            with rbac_errors():  # el comando vuelve a comprobar permisos y alcance con la fila
                convo, _ = attention.set_ai_excluded(
                    request.tenant, conversation_id=conversation_id, **data
                )
        except attention.VersionConflict:
            raise ApiError("CONVERSATION_VERSION_CONFLICT", 409, STALE) from None
        return Response(AiExclusionStateSerializer(convo).data)
