"""Tomar, transferir y soltar una conversación (F6-05a, E06, ADR-007).

`POST …/conversations/{id}/assign/` con `{action, user_id?, assignment_version?}`: los permisos
`conversations.assign` y `conversations.view`, comprobados antes de leer el cuerpo, y el cuerpo
antes que el 404. La conversación ha de estar en el alcance de `conversations.view` de quien llama
(si no, el mismo 404 que una desconocida o ajena). La respuesta no lleva el contacto ni texto
alguno: quien transfiere puede dejar de verla.
"""

from typing import Any

from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework import serializers
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.access.permissions import rbac_errors
from apps.conversations import commands, policy
from apps.conversations.api import inputs
from apps.conversations.models import Status
from core.api.errors import ApiError
from core.api.schema import errors
from core.tenancy import context

CONFLICT = (
    "Otra persona cambió la asignación de esta conversación. Actualiza y vuelve a intentarlo."
)
CLOSED = "La conversación está cerrada: ya no se puede asignar."
BAD_USER = "No es una persona de la organización que pueda atender esta conversación."


class AssignmentSerializer(serializers.Serializer[Any]):
    id = serializers.UUIDField()
    number = serializers.CharField()
    status = serializers.ChoiceField(choices=Status.choices)
    assignee_kind = serializers.CharField(help_text="`USER`, `TEAM` o `UNASSIGNED`.")
    assigned_user_id = serializers.UUIDField(allow_null=True)
    assigned_team_id = serializers.UUIDField(allow_null=True)
    assigned_at = serializers.DateTimeField(allow_null=True)
    assignment_version = serializers.IntegerField(help_text="La que se manda para no pisar a otra.")
    version = serializers.IntegerField()


@extend_schema_view(
    post=extend_schema(
        operation_id="conversation_assign",
        tags=["conversations"],
        request=inputs.AssignSerializer,
        responses={200: AssignmentSerializer, **errors(400, 401, 403, 404, 409)},
        description="Toma, transfiere o suelta la conversación (`conversations.assign` y "
        "`conversations.view` sobre ella). `TAKE`: solo si nadie la tiene. `TRANSFER` a "
        "`user_id`, un miembro activo con `conversations.view` (400 si no, con el mismo error "
        "para cualquiera). `RELEASE`: la deja sin responsable (una `OPEN` vuelve a la cola, "
        "`PENDING`). 409 `ASSIGNMENT_CONFLICT`: otra persona la cambió o la tomó antes, o la "
        "versión no es la actual; `CONVERSATION_CLOSED`. 404: no existe o fuera del alcance.",
    ),
)
class AssignView(APIView):
    required_permissions = {"POST": ("conversations.assign", "conversations.view")}

    def post(self, request: Request, **kwargs: Any) -> Response:
        wanted = inputs.AssignSerializer(data=request.data)  # el cuerpo, antes del 404
        wanted.is_valid(raise_exception=True)
        data, tenant = wanted.validated_data, context.current()
        assert tenant is not None  # noqa: S101 — `HasPermission` ya lo comprobó
        found: dict[str, Any] = {"conversation_id": self.kwargs["conversation_id"]}
        found["expected_version"] = data.get("assignment_version")
        try:
            with rbac_errors():  # el comando vuelve a comprobar permiso y alcance con la fila
                match data["action"]:
                    case "TAKE":
                        done = commands.take_conversation(tenant, **found)
                    case "RELEASE":
                        done = commands.release_conversation(tenant, **found)
                    case _:
                        done = commands.transfer_conversation(
                            tenant, user_id=data["user_id"], **found
                        )
        except commands.AssignmentConflict:
            raise ApiError("ASSIGNMENT_CONFLICT", 409, CONFLICT) from None
        except policy.ConversationClosed:
            raise ApiError("CONVERSATION_CLOSED", 409, CLOSED) from None
        except commands.BadReference:
            raise serializers.ValidationError({"user_id": [BAD_USER]}) from None
        return Response(AssignmentSerializer(done).data)
