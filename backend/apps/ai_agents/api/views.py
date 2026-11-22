"""Pedir al copiloto una sugerencia de respuesta (F8-08h) o un resumen de la conversación (F8-08i;
ADR-034 §7-§10, tenancy-context §2).

Las únicas rutas de tenant sin el scope de la petición: el modelo responde en segundos y la llamada
va **fuera** de toda transacción. `TenantResolutionMiddleware` resuelve el tenant y no abre el scope
(`UNSCOPED`); `HasPermission` mira `conversations.view` **y** `ai.use` en uno corto (403 sin uno de
los dos) y el runtime abre los suyos. Antes de todo, el tope por persona
(`apps.ai_gateway.throttle`, 429; un solo contador para las dos); tras el permiso, el de la
organización (F8-08m: la del contexto, 429 `AI_ORG_RATE_LIMITED`). Fuera del alcance, de otra
organización o inexistente, el mismo 404, sin llamar ni reservar. Nada se envía: la sugerencia
lleva el texto ya marcado, el tipo y la posición de cada marca y el aviso de traspaso; nunca el
tramo original, la evidencia, los costos ni el cuerpo del proveedor. El resumen es solo para quien
atiende: el texto del modelo con sus tramos «sin verificar» (tipo y posición) y el aviso. Los
errores llevan solo su código.

Insertar (F8-08j, ADR-034 §9.2) es una ruta de tenant normal, en la transacción de la petición:
no llama al modelo ni cuenta en el tope por persona. Devuelve el texto de una sugerencia de quien la
pidió y lo audita en esa transacción."""

from collections.abc import Callable
from typing import Any
from uuid import UUID

from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework import serializers
from rest_framework.exceptions import NotFound
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.ai_agents import copilot, drafts
from apps.ai_gateway import throttle
from core.api.errors import INTERNAL_ERROR, PERMISSION_DENIED, ApiError
from core.api.schema import errors

# El código del runtime → su estado; el código sale tal cual, salvo `ACCESS_DENIED` (perdió `ai.use`
# o la membresía a mitad: el 403 de siempre). Cualquier otro: 500 `INTERNAL_ERROR`.
STATUS = {
    **dict.fromkeys(("AI_DISABLED", "AI_NOT_CONFIGURED", "AI_BUDGET_NOT_SET",
                     "AI_BUDGET_EXHAUSTED"), 409),
    **dict.fromkeys(("PROVIDER_UNAVAILABLE", "PROVIDER_ERROR", "CREDENTIALS_REJECTED",
                     "PROVIDER_BILLING", "AI_MALFORMED", "AI_TURN_INVALID"), 502),
    "AI_DEADLINE": 504, "ACCESS_DENIED": 403,
}  # fmt: skip
ORG_LIMITED = "AI_ORG_RATE_LIMITED"  # el tope de la organización (F8-08m), con `Retry-After`


class CopilotPositionSerializer(serializers.Serializer[Any]):
    start = serializers.IntegerField(help_text="Índice de la marca en `text`.")
    end = serializers.IntegerField(help_text="Fin (exclusivo) de la marca.")


class CopilotFindingSerializer(serializers.Serializer[Any]):
    type = serializers.CharField(help_text="El tipo del hallazgo (`FIGURE`, `LINK`…).")
    position = CopilotPositionSerializer(help_text="Dónde está su marca; nunca el texto original.")


class CopilotHandoffSerializer(serializers.Serializer[Any]):
    requested = serializers.BooleanField(
        help_text="El cliente pide una persona; en la sugerencia, `text` es el texto fijo."
    )
    reason = serializers.CharField(allow_null=True, help_text="El motivo (un código) o null.")


class CopilotSuggestionSerializer(serializers.Serializer[Any]):
    id = serializers.UUIDField(help_text="La sugerencia guardada.")
    text = serializers.CharField(help_text="Ya renderizado, con `[verificar]` en cada hallazgo.")
    findings = CopilotFindingSerializer(many=True)
    handoff = CopilotHandoffSerializer()
    evidence_expires_at = serializers.DateTimeField(
        allow_null=True, help_text="Cuándo caduca su evidencia; null: no cita ninguna."
    )


class CopilotMarkSerializer(serializers.Serializer[Any]):
    type = serializers.CharField(help_text="Qué se marca «sin verificar» (`FIGURE`, `LINK`…).")
    start = serializers.IntegerField(help_text="Índice del tramo en `text` (code points).")
    end = serializers.IntegerField(help_text="Fin (exclusivo) del tramo.")


class CopilotSummarySerializer(serializers.Serializer[Any]):
    id = serializers.UUIDField(help_text="El resumen guardado.")
    text = serializers.CharField(help_text="El resumen, sin sustituir nada; no se inserta.")
    marks = CopilotMarkSerializer(many=True)
    handoff = CopilotHandoffSerializer()
    generated_by_ai = serializers.BooleanField(help_text="Siempre true: «Generado por IA».")


def drafted(
    run: Callable[[Any, UUID], copilot.Draft], request: Request, conversation_id: UUID
) -> copilot.Draft:
    """`run` del runtime con el tenant de la petición; sus errores, como respuestas. Antes, el tope
    de la organización (F8-08m): DRF ya miró el permiso, así que un 403 no lo gasta; el runtime
    devuelve el pedido que rechaza sin llamar (`NotFound`)."""
    organization = request.tenant.organization_id  # la de la sesión, nunca la del cuerpo
    wait, window = throttle.organization_wait(organization)
    if wait:
        limited = ApiError(ORG_LIMITED, 429)
        limited.wait = wait  # el manejador de errores lo pasa a `Retry-After`
        raise limited
    try:
        return run(request.tenant, conversation_id)
    except copilot.NotFound:
        throttle.organization_refund(organization, window)  # fuera de alcance: no cuenta
        raise NotFound from None  # el mismo 404 que una conversación que no existe
    except copilot.CopilotError as stop:
        status = STATUS.get(stop.code, 500)
        code = {403: PERMISSION_DENIED, 500: INTERNAL_ERROR}.get(status, stop.code)
        raise ApiError(code, status) from None


def shown(draft: copilot.Draft) -> dict[str, Any]:
    found = [{"type": f.type, "position": {"start": f.start, "end": f.end}} for f in draft.findings]
    handoff = {"requested": draft.handoff_reason is not None, "reason": draft.handoff_reason}
    return CopilotSuggestionSerializer({  # type: ignore[no-any-return]
        "id": draft.suggestion_id, "text": draft.text, "findings": found, "handoff": handoff,
        "evidence_expires_at": draft.expires_at,
    }).data  # fmt: skip


ASKED = "`conversations.view` con su alcance y `ai.use`; 403 `PERMISSION_DENIED` sin uno de los dos"
REFUSED = (
    "404: fuera de alcance, de otra organización o inexistente. 409 `AI_DISABLED`, "
    "`AI_NOT_CONFIGURED`, `AI_BUDGET_NOT_SET` o `AI_BUDGET_EXHAUSTED`. 502 `PROVIDER_UNAVAILABLE`, "
    "`PROVIDER_ERROR`, `CREDENTIALS_REJECTED`, `PROVIDER_BILLING`, `AI_MALFORMED` o "
    "`AI_TURN_INVALID`. 504 `AI_DEADLINE`. 429 `RATE_LIMITED`: el tope por persona (uno para "
    "sugerir y resumir), con `Retry-After`. 429 `AI_ORG_RATE_LIMITED`: el de la organización "
    "(uno para sugerir y resumir; tras el permiso), con `Retry-After`."
)


@extend_schema_view(
    post=extend_schema(
        operation_id="conversations_copilot_suggest", tags=["ai"], request=None,
        responses={201: CopilotSuggestionSerializer,
                   **errors(401, 403, 404, 409, 429, 502, 504)},
        description=f"Pide al copiloto una sugerencia de respuesta ({ASKED}). No envía nada: la "
        f"sugerencia se guarda y se audita (`ai.suggestion.created`). {REFUSED}",
    ),
)  # fmt: skip
class SuggestView(APIView):
    required_permissions = {"POST": ("conversations.view", "ai.use")}

    def post(self, request: Request, conversation_id: UUID, **kwargs: Any) -> Response:
        draft = drafted(copilot.suggest, request, conversation_id)
        return Response(shown(draft), status=201)


@extend_schema_view(
    post=extend_schema(
        operation_id="conversations_copilot_summary", tags=["ai"], request=None,
        responses={201: CopilotSummarySerializer,
                   **errors(401, 403, 404, 409, 429, 502, 504)},
        description=f"Pide al copiloto un resumen de la conversación para quien atiende ({ASKED}),"
        " sin herramientas. Se muestra «Generado por IA», con sus cifras, términos y enlaces "
        "marcados «sin verificar» (`marks`), y no se inserta en el compositor. No envía nada: se "
        f"guarda y se audita (`ai.summary.created`). {REFUSED}",
    ),
)  # fmt: skip
class SummaryView(APIView):
    required_permissions = {"POST": ("conversations.view", "ai.use")}

    def post(self, request: Request, conversation_id: UUID, **kwargs: Any) -> Response:
        draft = drafted(copilot.summarize, request, conversation_id)
        marks = [{"type": f.type, "start": f.start, "end": f.end} for f in draft.findings]
        handoff = {"requested": draft.handoff_reason is not None, "reason": draft.handoff_reason}
        body = CopilotSummarySerializer({"id": draft.suggestion_id, "text": draft.text,
                                         "marks": marks, "handoff": handoff,
                                         "generated_by_ai": True}).data  # fmt: skip
        return Response(body, status=201)


class CopilotInsertSerializer(serializers.Serializer[Any]):
    text = serializers.CharField(help_text="El texto guardado, con sus `[verificar]`.")


@extend_schema_view(
    post=extend_schema(
        operation_id="conversations_copilot_suggestion_insert", tags=["ai"], request=None,
        responses={200: CopilotInsertSerializer, **errors(401, 403, 404, 409)},
        description="Devuelve el texto de una sugerencia para el compositor y audita "
        "`ai.suggestion.inserted` en la misma transacción (`conversations.view` con su alcance y "
        "`ai.use`; 403 `PERMISSION_DENIED` sin uno de los dos). No envía nada ni llama al modelo. "
        "404: fuera de alcance, de otra organización, conversación o persona, inexistente, un "
        "resumen o ya purgada (30 días). 409 `AI_EVIDENCE_EXPIRED`: su evidencia caducó; hay que "
        "regenerarla.",
    ),
)  # fmt: skip
class InsertView(APIView):
    required_permissions = {"POST": ("conversations.view", "ai.use")}

    def post(
        self, request: Request, conversation_id: UUID, suggestion_id: UUID, **kwargs: Any
    ) -> Response:
        try:
            text = drafts.insert(request.tenant, conversation_id, suggestion_id)
        except drafts.NotFound:
            raise NotFound from None
        except drafts.EvidenceExpired:
            raise ApiError("AI_EVIDENCE_EXPIRED", 409) from None
        return Response(CopilotInsertSerializer({"text": text}).data)
