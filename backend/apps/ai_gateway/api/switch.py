"""El interruptor de IA de la organización (F8-06; ADR-034 §5, §10), fuera de
`…/ai/provider-accounts/`: apagar es fácil y encender es difícil (`apps.ai_gateway.switch`). La
organización es la de la ruta; el cuerpo solo aporta `notice_confirmed` al encender."""

from typing import Any

from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework import serializers
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.access.permissions import AnyOf, rbac_errors
from apps.ai_gateway import switch
from apps.ai_gateway.api.views import LIMITED
from apps.ai_gateway.models import AISwitch
from core.api.schema import errors
from core.tenancy import context

NAME, NOTICE = "notice_confirmed", "Confirma que el aviso de tratamiento de datos está publicado."
EITHER = "`ai.kill_switch` o `ai.manage`, sin segundo factor"


class AiSwitchSerializer(serializers.Serializer[Any]):
    enabled = serializers.BooleanField(help_text="El interruptor de la organización.")
    global_enabled = serializers.BooleanField(help_text="`AI_ENABLED`: apagado, no hay IA.")
    changed_at = serializers.DateTimeField(allow_null=True, help_text="Último cambio; null: nunca.")


class AiSwitchOnSerializer(serializers.Serializer[Any]):  # el esquema; la vista lo comprueba
    notice_confirmed = serializers.BooleanField(help_text="Exactamente `true`: el aviso de datos.")


def shown(row: AISwitch | None) -> dict[str, Any]:
    state = {"enabled": bool(row and row.enabled), "global_enabled": switch.globally_on(),
             "changed_at": row.changed_at if row else None}  # fmt: skip
    return AiSwitchSerializer(state).data  # type: ignore[no-any-return]


@extend_schema_view(
    get=extend_schema(
        operation_id="ai_switch_retrieve", tags=["ai"],
        responses={200: AiSwitchSerializer, **errors(401, 403, 404)},
        description=f"El interruptor de la organización y el del despliegue ({EITHER}). La IA "
        "llama solo con los dos encendidos; los dos nacen apagados.",
    ),
)  # fmt: skip
class SwitchView(APIView):
    required_permissions = {"GET": AnyOf(switch.STOPPERS)}

    def get(self, request: Request, **kwargs: Any) -> Response:
        with rbac_errors():
            return Response(shown(switch.read(context.require())))


@extend_schema_view(
    post=extend_schema(
        operation_id="ai_switch_off", tags=["ai"], request=None,
        responses={200: AiSwitchSerializer, **errors(401, 403, 404)},
        description=f"Apaga la IA de la organización ({EITHER} ni tope por persona) y lo audita "
        "(`ai.kill_switch.activated`). La siguiente llamada recibe `AI_DISABLED`; la que ya "
        "reservó termina. Apagada ya, no cambia nada.",
    ),
)  # fmt: skip
class SwitchOffView(APIView):
    required_permissions = {"POST": AnyOf(switch.STOPPERS)}

    def post(self, request: Request, **kwargs: Any) -> Response:
        with rbac_errors():
            return Response(shown(switch.turn_off(context.require())))


@extend_schema_view(
    post=extend_schema(
        operation_id="ai_switch_on", tags=["ai"], request=AiSwitchOnSerializer,
        responses={200: AiSwitchSerializer, **errors(400, 401, 403, 404, 429)},
        description="Enciende la IA de la organización (`ai.manage` y segundo factor reciente) "
        "con la confirmación del aviso de datos y lo audita (`ai.kill_switch.deactivated`). No "
        "enciende `AI_ENABLED`. Encendida ya, no cambia nada." + LIMITED,
    ),
)  # fmt: skip
class SwitchOnView(APIView):
    required_permissions = {"POST": switch.MANAGE}
    step_up_methods = {"POST"}

    def post(self, request: Request, **kwargs: Any) -> Response:
        body = request.data if isinstance(request.data, dict) else {}
        try:  # exactamente `true`: ni "true" ni 1
            with rbac_errors():
                done = switch.turn_on(context.require(), notice_confirmed=body.get(NAME) is True)
        except switch.NoticeNotConfirmed:
            raise serializers.ValidationError({NAME: [NOTICE]}) from None
        return Response(shown(done))
