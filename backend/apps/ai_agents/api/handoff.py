"""`GET` y `PUT …/ai/handoff-reply/` (F8-08l, #1046), como el modelo de la cuenta (F8-05b6):
`ai.manage` y después el segundo factor reciente; el `PUT`, en el tope de escrituras (429). La
organización, la de la ruta. Un texto inválido: 400 en `text`, un código y un mensaje por
problema, sin repetir lo enviado."""

from typing import Any

from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework import serializers
from rest_framework.exceptions import ErrorDetail
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.access.permissions import rbac_errors
from apps.ai_agents import replies
from apps.ai_agents.models import HANDOFF_MAX
from core.api.schema import errors
from core.tenancy import context

MANAGE = "ai.manage"
# Un mensaje por problema (`detectors.reply_problems`), con su código; ninguno repite el texto.
PROBLEMS = {
    "BODY": "Escribe un texto visible, sin caracteres ocultos ni de control.",
    "TOO_LONG": f"Como mucho {HANDOFF_MAX} caracteres.",
    "SCRIPT": "Solo letras del alfabeto latino.",
    "SOON": "Sin prometer atención inmediata ni rápida.",
    "FIGURE": "Sin cifras, importes ni monedas.",
    "DELIVERY": "Sin plazos, fechas, horarios ni entregas.",
    "DISCOUNT": "Sin descuentos, ofertas ni comparaciones de precio.",
    "AVAILABILITY": "Sin hablar de stock ni de disponibilidad de productos.",
    "TERMS": "Sin condiciones de pago, garantías, cambios ni devoluciones.",
    "LINK": "Sin enlaces, dominios ni correos.",
    "MARKUP": "Sin llaves ni la marca [verificar].",
}
OTHER = "El texto lleva algo que el copiloto no puede proponer."
LIMITED = " 429 `RATE_LIMITED`: el tope por persona de las escrituras, con `Retry-After`."


class AiHandoffReplySerializer(serializers.Serializer[Any]):
    text = serializers.CharField(help_text="Lo que el copiloto propone cuando piden una persona.")
    initial = serializers.BooleanField(
        help_text="true: el inicial del propietario (sin texto guardado, o el guardado ya no vale)."
    )
    changed_at = serializers.DateTimeField(allow_null=True, help_text="Último cambio; null: nunca.")


class AiHandoffReplyWriteSerializer(serializers.Serializer[Any]):
    text = serializers.CharField(
        help_text=f"De 1 a {HANDOFF_MAX} caracteres, sin cifras, plazos, prisas, promesas, "
        "enlaces ni la marca."
    )


def shown(reply: replies.Reply) -> dict[str, Any]:
    state = {"text": reply.text, "initial": reply.initial, "changed_at": reply.changed_at}
    return AiHandoffReplySerializer(state).data  # type: ignore[no-any-return]


ASKED = "`ai.manage` y segundo factor reciente"


@extend_schema_view(
    get=extend_schema(
        operation_id="ai_handoff_reply_retrieve", tags=["ai"],
        responses={200: AiHandoffReplySerializer, **errors(401, 403, 404)},
        description=f"El texto de traspaso que propone el copiloto ({ASKED}): el de la "
        "organización o, sin él, el inicial. No cuenta en el tope por persona de las escrituras.",
    ),
    put=extend_schema(
        operation_id="ai_handoff_reply_update", tags=["ai"],
        request=AiHandoffReplyWriteSerializer,
        responses={200: AiHandoffReplySerializer, **errors(400, 401, 403, 404, 429)},
        description=f"Cambia el texto de traspaso de la organización ({ASKED}) y lo audita "
        "(`ai.handoff_reply.changed`, sin el texto). 400 en `text`: un código y un mensaje por "
        "problema (`BODY`, `TOO_LONG`, `SCRIPT`, `SOON`, `FIGURE`, `DELIVERY`, `DISCOUNT`, "
        "`AVAILABILITY`, `TERMS`, `LINK`, `MARKUP`); nunca repite lo enviado." + LIMITED,
    ),
)  # fmt: skip
class HandoffReplyView(APIView):
    required_permissions = {"GET": MANAGE, "PUT": MANAGE}
    step_up_methods = {"GET", "PUT"}

    def get(self, request: Request, **kwargs: Any) -> Response:
        with rbac_errors():
            return Response(shown(replies.read(context.require())))

    def put(self, request: Request, **kwargs: Any) -> Response:
        wanted = AiHandoffReplyWriteSerializer(data=request.data)
        wanted.is_valid(raise_exception=True)
        try:
            with rbac_errors():
                done = replies.change(context.require(), wanted.validated_data["text"])
        except replies.InvalidReply as bad:
            found = [ErrorDetail(PROBLEMS.get(code, OTHER), code) for code in bad.problems]
            raise serializers.ValidationError({"text": found}) from None
        return Response(shown(done))
