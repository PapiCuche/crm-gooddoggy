"""El runtime del copiloto (F8-08g; ADR-034 §2, §5, §6, §7, §9; ADR-005 Partes C, D y E;
tenancy-context §6): una invocación de «sugerir» de punta a punta, **fuera** de toda transacción.

1. Un scope corto de preparación: la persona (actor `USER`) con `ai.use` y la conversación en el
   alcance de su `conversations.view`; la ajena, la inexistente y la que no alcanza dan el mismo
   `NotFound`, sin llamar al proveedor ni reservar. Ahí se construye el contexto (F8-08d).
2. Cada llamada al modelo por `gateway.complete` (kill switch, presupuesto, reserva y ajuste), sin
   transacción abierta y solo si cabe en lo que queda de `AI_COPILOT_DEADLINE`.
3. Cada tool call en su propio scope corto de solo lectura, con los permisos de la persona leídos
   de nuevo (`registry.invoke_read_only`); la evidencia, por `tool_use_id`. Pasadas
   `AI_COPILOT_TOOL_CALLS`, las demás se contestan con un error fijo y se pide la respuesta final.
   Una llamada solo empieza si su respuesta aún se podría contestar dentro de `AI_TURN_LIMITS`.
4. El Output Guard (F8-08e, F8-08f) sobre la respuesta final, con `now` del reloj de la base tras
   las tools. Un problema o un hallazgo de los detectores pide **un** reintento con el motivo
   estructurado: códigos, ids `c<n>`, nombres de tools, tipos y posiciones; nunca texto del
   modelo ni del cliente. Si vuelve a fallar, el borrador sale con sus hallazgos.
5. `records.save` en su propio scope: el run con las reservas del gateway, las tool calls con su
   evidencia y el borrador. Un error a mitad deja el run con su código y sin borrador.

Las tools las fija el código (las de `SUGGEST`); la organización y la persona salen de `ctx`, nunca
del modelo. El log lleva solo el `ai_run_id`, códigos y duraciones."""

import json
import time
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import datetime
from uuid import UUID

from django.conf import settings
from django.db import connections

from apps.access.selectors import AccessDenied, execution_context, has_permission, scoped
from apps.ai_agents import context, detectors, guard, records, registry
from apps.ai_agents.context import Purpose
from apps.ai_agents.registry import Evidence
from apps.ai_gateway import gateway
from apps.ai_gateway.gateway import GatewayError
from apps.ai_gateway.llm import Block, LLMRequest, LLMResponse, Outcome, Text, ToolResult, Turn
from apps.conversations.models import Conversation
from core.observability.logging import get_logger
from core.tenancy.context import TenantContext
from core.tenancy.scope import require_scope, tenant_scope
from core.text import VERIFY_MARK

logger = get_logger(__name__)
clock = time.monotonic  # el reloj del plazo total
CALL_SECONDS = 10  # una llamada al modelo no empieza con menos tiempo que esto por delante
EXTRA_CALLS = (
    3  # además de las tool calls: la ronda contestada con el error, la final y el reintento
)
TEXT_MAX = 10_000  # el CHECK de `ai_suggestions.text`
LIMITED = '{"error":{"code":"TOOL_LIMIT"},"ok":false}'  # el sobre de error de `registry`
FINISH = "Ya no quedan llamadas a herramientas: responde ahora con la salida estructurada."
HITS = "Tramos sin evidencia del texto renderizado (tipo y posición); quítalos o usa claims: {}"
WHOLE = (0, len(VERIFY_MARK))


class NotFound(Exception):  # noqa: N818 — ajena, inexistente, fuera de alcance o sin `ai.use`
    pass


class CopilotError(Exception):  # noqa: N818 — solo el código; el run ya quedó registrado
    def __init__(self, code: str, run_id: UUID) -> None:
        super().__init__(code)
        self.code, self.run_id = code, run_id


class _Stop(Exception):  # noqa: N818
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Draft:
    run_id: UUID
    suggestion_id: UUID
    text: str  # ya renderizado, con sus marcas
    findings: tuple[records.Finding, ...]
    handoff_reason: str | None
    expires_at: datetime | None


@dataclass
class _State:  # lo que se registra, también si la invocación se corta
    conversation_id: UUID
    request: LLMRequest
    deadline: float
    tools_left: int
    calls_left: int
    actions: list[records.Action] = field(default_factory=list)
    evidence: dict[str, Evidence | None] = field(default_factory=dict)
    reservations: list[UUID] = field(default_factory=list)
    retried: bool = False


def _prepare(ctx: TenantContext, conversation_id: UUID) -> tuple[LLMRequest, tuple[str, ...]]:
    """El scope corto de preparación: la persona, sus permisos, el alcance y el contexto."""
    with tenant_scope(ctx):
        try:
            ectx = execution_context(ctx)  # solo una persona con membresía activa
        except AccessDenied:
            raise NotFound from None
        mine = scoped(ectx, "conversations.view", Conversation.objects.filter(pk=conversation_id))
        if not has_permission(ectx, "ai.use") or not mine.exists():
            raise NotFound
        request = context.build(ctx, conversation_id, Purpose.SUGGEST)
        return request, context.customer_since_reply(ctx, conversation_id)


def _call(ctx: TenantContext, state: _State) -> LLMResponse:
    """Una llamada al modelo, si cabe en el plazo y en los topes; su reserva queda en el run."""
    left = state.deadline - clock()
    if left < CALL_SECONDS:
        raise _Stop("AI_DEADLINE")
    if not state.calls_left or len(state.request.turns) + 2 > settings.AI_TURN_LIMITS["turns"]:
        raise _Stop(Outcome.AI_TURN_INVALID)  # su respuesta ya no se podría contestar
    state.calls_left -= 1
    try:
        reply = gateway.complete(ctx, state.request, seconds=left)
    except GatewayError as error:
        state.reservations += [error.reservation_id] if error.reservation_id else []
        raise _Stop(error.outcome) from None
    state.reservations += [reply.reservation_id] if reply.reservation_id else []
    return reply


def _extend(state: _State, reply: LLMResponse, answer: tuple[Block, ...]) -> None:
    """La respuesta del modelo tal cual y la contestación; el gateway valida el turno (y sus
    límites) antes de reservar."""
    conversation = (*state.request.turns, Turn("assistant", reply.blocks), Turn("user", answer))
    state.request = replace(state.request, turns=conversation)


def _tools(ctx: TenantContext, state: _State, reply: LLMResponse) -> tuple[Block, ...]:
    """Cada tool call en su scope de solo lectura; pasado el tope, el error fijo."""
    results: list[Block] = []
    for call in reply.tool_calls:
        if state.tools_left:
            state.tools_left -= 1
            done = registry.invoke_read_only(ctx, call)
            state.evidence[call.id] = done.evidence
            state.actions.append(
                records.Action(call.id, call.name, done.code, call.input, done.evidence)
            )
            results.append(done.result(call.id))
        else:
            state.actions.append(records.Action(call.id, call.name, "TOOL_LIMIT", call.input))
            results.append(ToolResult(call.id, (Text(LIMITED),), is_error=True))
    return tuple(results) if state.tools_left else (*results, Text(FINISH))


def _now(ctx: TenantContext) -> datetime:
    """El reloj de la base, tras las tools: la evidencia recién leída nunca es «del futuro»."""
    with tenant_scope(ctx), connections[require_scope(ctx)].cursor() as cursor:
        cursor.execute("SELECT clock_timestamp()")
        now: datetime = cursor.fetchone()[0]
    return now


def _verdict(
    ctx: TenantContext, state: _State, text: str, customer: tuple[str, ...]
) -> tuple[guard.Rendered, detectors.Handoff, str]:
    """El borrador ya sustituido, el traspaso y el motivo del reintento (vacío si no hace falta)."""
    why = ""
    try:
        rendered = guard.render(json.loads(text), state.evidence, _now(ctx))
    except ValueError, RecursionError:  # `Malformed` o un JSON ilegible: no se renderiza nada
        rendered = guard.Rendered(VERIFY_MARK, (guard.Finding("MALFORMED", *WHOLE),), (), False,
                                  None, (WHOLE,))  # fmt: skip
        why = guard.Malformed.FEEDBACK
    draft, handoff = detectors.suggestion(rendered, customer)
    if handoff.requested:  # el texto fijo: nada que reintentar
        return draft, handoff, ""
    hits = [f"{f.type} {f.start}-{f.end}" for f in draft.findings if f.type in detectors.PATTERNS]
    lines = (why, draft.feedback(), HITS.format(", ".join(hits)) if hits else "")
    return draft, handoff, "\n".join(line for line in lines if line)


def _answer(
    ctx: TenantContext, state: _State, customer: tuple[str, ...]
) -> tuple[guard.Rendered, detectors.Handoff]:
    while True:
        reply = _call(ctx, state)
        if reply.tool_calls:
            _extend(state, reply, _tools(ctx, state, reply))
            continue
        draft, handoff, why = _verdict(ctx, state, reply.text, customer)
        if not why or state.retried:
            return draft, handoff
        state.retried = True  # una sola vez, con su propia reserva
        _extend(state, reply, (Text(why),))


def _kept(draft: guard.Rendered) -> records.Suggestion:
    """El borrador dentro de los CHECK de `ai_suggestions` (texto y hallazgos): si no cabe, solo la
    marca, nunca un texto cortado ni el run sin registrar."""
    if len(draft.text) > TEXT_MAX or len(draft.findings) > detectors.MAX_FINDINGS:
        draft = replace(draft, text=VERIFY_MARK, findings=(guard.Finding("TOO_LONG", *WHOLE),),
                        expires_at=None)  # fmt: skip
    found = tuple(records.Finding(f.type, f.start, f.end) for f in draft.findings)
    return records.Suggestion(draft.text, found, draft.expires_at)


def _save(
    ctx: TenantContext, state: _State, outcome: str, started: float,
    kept: records.Suggestion | None = None, handoff: str | None = None,
) -> UUID:  # fmt: skip
    """El run, sus tool calls y el borrador, en su propio scope (todo o nada)."""
    ms = int((clock() - started) * 1000)
    counts = Counter(finding.type for finding in kept.findings) if kept else Counter[str]()
    run = records.Run("SUGGEST", state.conversation_id, outcome, ms, counts,
                      tuple(state.reservations), state.retried, handoff)  # fmt: skip
    records.save(ctx, run, state.actions, kept)
    logger.info("ai.copilot_run", ai_run_id=str(run.id), outcome=outcome, ms=ms,
                tool_calls=len(state.actions), retried=state.retried)  # fmt: skip
    return run.id


def suggest(ctx: TenantContext, conversation_id: UUID) -> Draft:
    """Sugiere la respuesta de quien atiende `conversation_id`. Se llama **fuera** de toda
    transacción. `NotFound` sin escribir ni llamar; `CopilotError` con el código del gateway,
    `AI_DEADLINE` o `AI_TURN_INVALID`, con el run ya registrado."""
    if connections["default"].in_atomic_block:
        raise RuntimeError("suggest() abre scopes cortos: ninguna transacción puede estar abierta")
    started = clock()
    tool_calls, seconds = context.caps()
    request, customer = _prepare(ctx, conversation_id)
    state = _State(conversation_id, request, started + seconds, tool_calls,
                   tool_calls + EXTRA_CALLS)  # fmt: skip
    try:
        draft, handoff = _answer(ctx, state, customer)
    except _Stop as stop:
        raise CopilotError(stop.code, _save(ctx, state, stop.code, started)) from None
    kept = _kept(draft)
    run_id = _save(ctx, state, "OK", started, kept, handoff.reason)
    return Draft(run_id, kept.id, kept.text, kept.findings, handoff.reason,
                 kept.evidence_expires_at)  # fmt: skip
