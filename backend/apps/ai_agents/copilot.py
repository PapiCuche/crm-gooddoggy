"""El runtime del copiloto (F8-08g; ADR-034 §2, §5-§7, §9; ADR-005 C-E; tenancy-context §6):
«sugerir» de punta a punta, **fuera** de toda transacción. Un scope corto de preparación (la
persona con `ai.use` y la conversación en el alcance de su `conversations.view`; si no, el mismo
`NotFound`, sin llamar ni reservar); cada llamada al modelo por `gateway.complete`, solo si cabe en
el plazo, los topes y con `ai.use` aún; cada tool call en su scope de solo lectura; el guard con el
reloj de la base; **un** reintento con el motivo estructurado (códigos, ids, tipos y posiciones,
nunca texto del modelo ni del cliente); `records.save` al final. Un error a mitad deja el run con
su código y sin borrador; cualquier otro fallo (la base, su `statement_timeout`, una tool o un
error nuestro), como `INTERNAL_ERROR` con sus reservas (F8-08g2): ninguna llamada pagada sin su
run. Tools fijadas por el código; organización y persona de `ctx`; el log lleva solo el
`ai_run_id`, códigos y duraciones. «Resumir» (`summarize`, F8-08i) es lo mismo sin tools.
Una conversación excluida de la IA (F8-13) se rechaza con `AI_EXCLUDED` antes de leer su contexto
y otra vez antes de cada llamada pagada; si la marca no se puede leer, no se llama (falla cerrado).
F8-13c: cada llamada tiene el candado compartido de la conversación (`attention.ai_call`) hasta el
fin de su envío; excluirla lo pide exclusivo. La comprobación (que exige el candado) corre en la
transacción de la reserva (`admit`) y antes de cada reintento."""

import json
import re
import time
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from uuid import UUID

from django.conf import settings
from django.db import connections

from apps.access.selectors import AccessDenied, execution_context, has_permission, scoped
from apps.ai_agents import context, detectors, guard, records, registry, replies
from apps.ai_agents.context import Purpose
from apps.ai_agents.registry import Evidence
from apps.ai_gateway import gateway
from apps.ai_gateway.gateway import GatewayError
from apps.ai_gateway.llm import Block, LLMRequest, LLMResponse, Outcome, Text, ToolResult, Turn
from apps.conversations import attention
from apps.conversations.models import Conversation
from apps.conversations.policy import ConversationClosed
from core.observability.logging import get_logger
from core.tenancy.context import TenantContext
from core.tenancy.scope import require_scope, tenant_scope
from core.text import VERIFY_MARK

logger = get_logger(__name__)
CALL_SECONDS = 10  # una llamada al modelo no empieza con menos tiempo que esto por delante
MARGIN = 1  # el `SEND_SECONDS` del gateway: con menos por delante, libera la reserva sin enviar
EXTRA_CALLS = 3  # tras las tool calls: la ronda con el error fijo, la final y el reintento
TEXT_MAX, FINDINGS_MAX = 10_000, 100  # los CHECK de `ai_suggestions`
LIMITED = '{"error":{"code":"TOOL_LIMIT"},"ok":false}'  # el sobre de error de `registry`
FINISH = "Ya no quedan llamadas a herramientas: responde ahora con la salida estructurada."
HITS = "Tramos sin evidencia del texto renderizado (tipo y posición); quítalos o usa claims: {}"
WHOLE = (0, len(VERIFY_MARK))
SUMMARY_CALLS = 2  # el resumen, sin tools: una llamada y su reintento
SHORTER = (f"El resumen llegó vacío, con caracteres de control o con más de {TEXT_MAX} caracteres: "
           "responde solo con un resumen más corto, en texto plano.")  # fmt: skip
TOOL_USE_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")  # el CHECK de `ai_actions.tool_use_id`
EXCLUDED = "AI_EXCLUDED"  # F8-13: la conversación está excluida de la IA


class NotFound(Exception):  # noqa: N818 — ajena, inexistente, fuera de alcance o sin `ai.use`
    pass


class CopilotError(Exception):  # noqa: N818 — solo el código; sale con el run ya registrado
    def __init__(self, code: str, run_id: UUID | None = None) -> None:
        super().__init__(code)
        self.code, self.run_id = code, run_id


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
    refused: str | None = None  # F8-13c: el código de la última comprobación que lo negó


def _admitted(ctx: TenantContext, conversation_id: UUID) -> None:
    """En el scope activo: la persona activa con `ai.use`, la conversación en el alcance de su
    `conversations.view` (si no, el mismo `NotFound`) y no excluida de la IA (F8-13; si no,
    `CopilotError(AI_EXCLUDED)`). Un error al leer la marca sale tal cual: nada se llama."""
    try:
        ectx = execution_context(ctx)  # solo una persona con membresía activa
    except AccessDenied:
        raise NotFound from None
    mine = scoped(ectx, "conversations.view", Conversation.objects.filter(pk=conversation_id))
    if not has_permission(ectx, "ai.use"):
        raise NotFound
    marks = list(mine.values_list("ai_excluded_at", flat=True))
    if not marks:
        raise NotFound
    if marks != [None]:
        raise CopilotError(EXCLUDED)


def admit(ctx: TenantContext, conversation_id: UUID) -> None:
    """`_admitted` en su propio scope corto: la ruta lo mira antes de contar los topes (F8-13)."""
    with tenant_scope(ctx):
        _admitted(ctx, conversation_id)


def _prepare(
    ctx: TenantContext, conversation_id: UUID, purpose: Purpose
) -> tuple[LLMRequest, tuple[str, ...]]:
    """El scope corto de preparación: la persona, sus permisos, el alcance, la exclusión y, solo
    después, el contexto."""
    with tenant_scope(ctx):
        _admitted(ctx, conversation_id)
        request = context.build(ctx, conversation_id, purpose)
        return request, context.customer_since_reply(ctx, conversation_id)


def _refused(ctx: TenantContext, conversation_id: UUID) -> str | None:
    """En el scope del gateway, antes de reservar y de cada envío: `ACCESS_DENIED` si la persona
    perdió su membresía o `ai.use`; `AI_EXCLUDED` si la conversación está excluida o ya no se puede
    leer (falla cerrado). Un error de la base, o el candado perdido (`CallLockLost`), sale tal cual:
    el run queda `INTERNAL_ERROR`, no excluido."""
    try:
        if not has_permission(execution_context(ctx), "ai.use"):
            return "ACCESS_DENIED"
    except AccessDenied:
        return "ACCESS_DENIED"
    return None if attention.ai_included(ctx, conversation_id) else EXCLUDED


def _admits(ctx: TenantContext, state: _State) -> bool:
    """El `admit` del gateway: guarda el código para el run (sin código, si falló: el gateway no
    reenvía y el run queda `INTERNAL_ERROR`)."""
    state.refused = _refused(ctx, state.conversation_id)
    return state.refused is None


def _call(ctx: TenantContext, state: _State) -> LLMResponse:
    """Una llamada al modelo, si cabe en el plazo y en los topes; su reserva queda en el run."""
    if state.deadline - time.monotonic() < CALL_SECONDS:
        raise CopilotError("AI_DEADLINE")
    if not state.calls_left or len(state.request.turns) + 2 > settings.AI_TURN_LIMITS["turns"]:
        raise CopilotError(Outcome.AI_TURN_INVALID)  # su respuesta ya no se podría contestar
    state.calls_left -= 1
    try:  # F8-13c: con el candado de la llamada, de antes de la reserva al fin del envío
        with attention.ai_call(ctx, state.conversation_id, state.deadline - time.monotonic()):
            reply = gateway.complete(ctx, state.request, deadline=state.deadline,
                                     admit=lambda: _admits(ctx, state))  # fmt: skip
    except GatewayError as error:  # sin tiempo ya ni para enviar, o cortado en el plazo
        state.reservations += [error.reservation_id] if error.reservation_id else []
        if error.outcome == Outcome.AI_WITHHELD:  # F8-13c: excluida, sin `ai.use` o sin leerse
            raise CopilotError(state.refused or "INTERNAL_ERROR") from None
        late = time.monotonic() >= state.deadline - MARGIN  # o liberada por falta de tiempo
        raise CopilotError("AI_DEADLINE" if late else error.outcome) from None
    except attention.AICallInProgress:  # F8-13c: una exclusión retuvo el candado todo el plazo
        raise CopilotError("AI_DEADLINE") from None
    except AccessDenied:  # la membresía cayó entre la comprobación y la reserva: no reservó
        raise CopilotError("ACCESS_DENIED") from None
    state.reservations += [reply.reservation_id] if reply.reservation_id else []
    if time.monotonic() >= state.deadline:  # ni tools, ni guard, ni borrador fuera de plazo
        raise CopilotError("AI_DEADLINE")
    if not all(TOOL_USE_ID.fullmatch(call.id) for call in reply.tool_calls):
        raise CopilotError(Outcome.PROVIDER_UNAVAILABLE)  # un id que no se podría registrar
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
    """El borrador ya sustituido, el traspaso y el motivo del reintento (vacío si no hace falta).
    Una respuesta que no cumple el esquema deja `MALFORMED` como único hallazgo."""
    why = ""
    try:
        rendered = guard.render(json.loads(text), state.evidence, _now(ctx))
    except ValueError, RecursionError:  # `Malformed` o un JSON ilegible: no se renderiza nada
        rendered = guard.Rendered(VERIFY_MARK, (guard.Finding("MALFORMED", *WHOLE),), (), False,
                                  None, (WHOLE,))  # fmt: skip
        why = guard.Malformed.FEEDBACK
    # con traspaso, el texto de la organización (F8-08l), leído solo entonces en su scope corto
    draft, handoff = detectors.suggestion(rendered, customer, lambda: replies.current(ctx))
    if handoff.requested:  # el texto de traspaso: nada que reintentar
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
        if state.retried and why == guard.Malformed.FEEDBACK:  # dos veces sin una respuesta usable
            raise CopilotError("AI_MALFORMED")
        if not why or state.retried:
            return draft, handoff
        state.retried = True  # una sola vez, con su propia reserva
        _extend(state, reply, (Text(why),))


def _kept(draft: guard.Rendered) -> records.Suggestion:
    """El borrador dentro de los CHECK de `ai_suggestions` (texto y hallazgos): si no cabe o queda
    vacío, solo la marca, nunca un texto cortado ni el run sin registrar."""
    if not draft.text.strip() or len(draft.text) > TEXT_MAX or len(draft.findings) > FINDINGS_MAX:
        kind = "TOO_LONG" if draft.text.strip() else "MALFORMED"
        draft = replace(draft, text=VERIFY_MARK, findings=(guard.Finding(kind, *WHOLE),),
                        expires_at=None)  # fmt: skip
    found = tuple(records.Finding(f.type, f.start, f.end) for f in draft.findings)
    return records.Suggestion(draft.text, found, draft.expires_at)


def _reconnect() -> None:
    """Una conexión que la base cortó (mientras el proveedor respondía o tras la última consulta)
    se cierra, como hace Django entre peticiones: el registro abre otra y el borrador no se pierde.
    Nunca dentro de una transacción."""
    for db in connections.all(initialized_only=True):
        if not db.in_atomic_block and not db.is_usable():
            db.close()


def _save(
    ctx: TenantContext, state: _State, outcome: str, started: float,
    kept: records.Suggestion | None = None, handoff: str | None = None,
) -> UUID:  # fmt: skip
    """El run, sus tool calls y el borrador, en su propio scope (todo o nada). Si no se guarda, el
    log lleva solo códigos e ids (las reservas, que enlazan cada llamada) y el error sigue."""
    _reconnect()
    ms = int((time.monotonic() - started) * 1000)
    counts = Counter(finding.type for finding in kept.findings) if kept else Counter[str]()
    summary = state.request.purpose == Purpose.SUMMARY
    run = records.Run("SUMMARY" if summary else "SUGGEST", state.conversation_id, outcome, ms,
                      counts, tuple(state.reservations), state.retried, handoff)  # fmt: skip
    try:
        records.save(ctx, run, state.actions, kept)
    except Exception as error:  # nunca su mensaje: puede llevar valores de la fila
        logger.error("ai.copilot_run_unsaved", ai_run_id=str(run.id), outcome=outcome,
                     error=type(error).__name__,
                     reservation_ids=[str(held) for held in state.reservations])  # fmt: skip
        raise
    logger.info("ai.copilot_run", ai_run_id=str(run.id), outcome=outcome, ms=ms,
                tool_calls=len(state.actions), retried=state.retried)  # fmt: skip
    return run.id


def _request_human(
    ctx: TenantContext, conversation_id: UUID, reason: str, run_id: UUID, after: datetime
) -> None:
    """F8-08n: el traspaso queda en la conversación, en su scope y tras guardar el run. Cerrada o
    respondida desde `after`: nada; sin acceso, `NotFound`; otro fallo, al log con códigos."""
    try:
        with tenant_scope(ctx):
            attention.request_human(ctx, conversation_id=conversation_id, reason=reason,
                                    ai_run_id=run_id, after=after)  # fmt: skip
    except ConversationClosed:
        pass
    except Conversation.DoesNotExist, AccessDenied:
        raise NotFound from None
    except Exception as error:  # nunca su mensaje: puede llevar valores de la fila
        logger.error("ai.copilot_handoff_unsaved", ai_run_id=str(run_id),
                     error=type(error).__name__)  # fmt: skip


def _suggested(
    ctx: TenantContext, state: _State, customer: tuple[str, ...]
) -> tuple[records.Suggestion, str | None]:
    draft, handoff = _answer(ctx, state, customer)
    return _kept(draft), handoff.reason


def _summarized(
    ctx: TenantContext, state: _State, customer: tuple[str, ...]
) -> tuple[records.Suggestion, str | None]:
    """El resumen (§7): sin tools ni esquema. Un texto vacío, con lo que la base no guarda o más
    largo que `TEXT_MAX` se pide **una** vez más (la misma petición con el motivo fijo, nunca su
    texto); dos veces, `AI_MALFORMED`. Se marca con `detectors.mark`, sin sustituir nada, y el
    traspaso sale solo de lo que escribió el cliente: el resumen no tiene `wants_handoff`."""
    [asked] = state.request.turns
    while True:
        text = _call(ctx, state).text.strip()
        if text and len(text) <= TEXT_MAX and not guard.UNSTORABLE.search(text):
            break
        if state.retried:
            raise CopilotError("AI_MALFORMED")
        state.retried = True  # una sola vez, con su propia reserva
        state.request = replace(
            state.request, turns=(Turn("user", f"{asked.content}\n\n{SHORTER}"),)
        )
    marks = tuple(records.Finding(f.type, f.start, f.end) for f in detectors.mark(text))
    return records.Suggestion(text, marks), detectors.handoff(customer, False).reason


def _invoke(ctx: TenantContext, conversation_id: UUID, purpose: Purpose) -> Draft:
    if connections["default"].in_atomic_block:
        raise RuntimeError(f"{purpose.lower()}() abre scopes cortos: ninguna transacción abierta")
    started, opened = time.monotonic(), datetime.now(UTC)
    tool_calls, seconds = context.caps()
    request, customer = _prepare(ctx, conversation_id, purpose)
    summary = purpose == Purpose.SUMMARY
    tools = 0 if summary else tool_calls
    state = _State(conversation_id, request, started + seconds, tools,
                   SUMMARY_CALLS if summary else tools + EXTRA_CALLS)  # fmt: skip
    try:
        answer = _summarized if summary else _suggested
        kept, handoff = answer(ctx, state, customer)
        run_id = _save(ctx, state, "OK", started, kept, handoff)
    except CopilotError as stop:
        raise CopilotError(stop.code, _save(ctx, state, stop.code, started)) from None
    except Exception:  # la base, una tool o un error nuestro: el run sin borrador, con sus reservas
        raise CopilotError("INTERNAL_ERROR", _save(ctx, state, "INTERNAL_ERROR", started)) from None
    if handoff is not None:
        _request_human(ctx, conversation_id, handoff, run_id, opened)
    return Draft(run_id, kept.id, kept.text, kept.findings, handoff,
                 kept.evidence_expires_at)  # fmt: skip


def suggest(ctx: TenantContext, conversation_id: UUID) -> Draft:
    """Sugiere la respuesta de quien atiende `conversation_id`. Se llama **fuera** de toda
    transacción. `NotFound` sin escribir ni llamar; `CopilotError` con el código del gateway,
    `AI_DEADLINE`, `AI_TURN_INVALID` o `INTERNAL_ERROR`, con el run ya registrado; si ni ese
    registro se puede guardar, el error de la base."""
    return _invoke(ctx, conversation_id, Purpose.SUGGEST)


def summarize(ctx: TenantContext, conversation_id: UUID) -> Draft:
    """Resume `conversation_id` para quien atiende (F8-08i), con los mismos scopes, permisos,
    plazo, errores y registro que `suggest`; sin tools. El texto es el del modelo con sus marcas
    (`findings`: tipo y tramo), nunca para insertar; `expires_at` siempre es `None`."""
    return _invoke(ctx, conversation_id, Purpose.SUMMARY)
