"""El registro de una invocación del copiloto (F8-08b; ADR-034 §5, §7; ADR-011).

`save` escribe el run, sus tool calls con la evidencia y el borrador en **una** transacción (su
`tenant_scope`), y audita el borrador solo con ids. La organización y quien pide salen de la
sesión que fija el contexto (`app_current_tenant()`, `app_current_user()`), nunca de un argumento.
Nada va al outbox. Ni el prompt ni la respuesta del modelo: de los argumentos de una tool se
guarda un UUID tal cual y, de otro texto (la búsqueda), solo su longitud; el texto del borrador
solo vive en `ai_suggestions`, 30 días (D-F8-05, la purga de F8-08j).

F8-08j: `inserted` audita la inserción en la transacción de quien llama (la de la petición) y
`purge_text` pone a NULL el texto de los borradores viejos de una organización (lo único que
`ai_suggestions_guard` deja cambiar)."""

import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from django.db import connections, transaction

from apps.ai_agents import registry
from apps.ai_agents.fields import CANONICAL
from apps.audit.services import Entity, record
from core.ids import new_id
from core.tenancy.context import ActorType, TenantContext
from core.tenancy.scope import require_scope, tenant_scope

_RUN = (
    "INSERT INTO ai_runs (id, organization_id, purpose, requested_by_user_id, conversation_id,"
    " outcome, tool_calls, findings, reservation_ids, retried, handoff_reason, duration_ms)"
    " VALUES (%s, app_current_tenant(), %s, app_current_user(), %s, %s, %s, %s::jsonb,"
    " %s::uuid[], %s, %s, %s)"
)
_ACTION = (
    "INSERT INTO ai_actions (organization_id, run_id, tool_use_id, tool, code, arguments,"
    " evidence_id, kind, as_of, subjects, facts) VALUES (app_current_tenant(), %s, %s, %s, %s,"
    " %s::jsonb, %s, %s, %s, %s::uuid[], %s::jsonb)"
)
_SUGGESTION = (
    "INSERT INTO ai_suggestions (id, organization_id, run_id, conversation_id,"
    " requested_by_user_id, kind, text, findings, handoff_reason, evidence_expires_at)"
    " VALUES (%s, app_current_tenant(), %s, %s, app_current_user(), %s, %s, %s::jsonb, %s, %s)"
)

# Un lote de la organización del scope (RLS); `SKIP LOCKED`: nunca espera a otra pasada. El lote va
# en un CTE `MATERIALIZED`: en `id IN (… LIMIT n)` el planificador puede volver a recorrer la
# subconsulta por cada fila (un nested loop bajo RLS) y cambiar más de `n`.
PURGE = (
    "WITH due AS MATERIALIZED (SELECT id FROM ai_suggestions WHERE text IS NOT NULL"
    " AND created_at < now() - make_interval(days => %s) ORDER BY created_at LIMIT %s"
    " FOR UPDATE SKIP LOCKED) UPDATE ai_suggestions s SET text = NULL FROM due WHERE s.id = due.id"
)


@dataclass(frozen=True)
class Run:
    purpose: Literal["SUGGEST", "SUMMARY"]
    conversation_id: UUID
    outcome: str  # un código (`OK`, `AI_BUDGET_EXHAUSTED`…)
    duration_ms: int
    findings: Mapping[str, int] = field(default_factory=dict)  # tipo del guard → cuántos
    reservation_ids: tuple[UUID, ...] = ()  # las del gateway: enlazan con `ai_llm_calls`
    retried: bool = False
    handoff_reason: str | None = None  # con traspaso, su motivo (un código)
    id: UUID = field(default_factory=new_id)  # antes de guardar: el log lleva `ai_run_id`


@dataclass(frozen=True)
class Action:
    tool_use_id: str
    tool: str
    code: str  # el de `registry.Invocation`
    arguments: object  # lo que pidió el modelo, sin limpiar: se guarda redactado
    evidence: registry.Evidence | None = None


@dataclass(frozen=True)
class Finding:
    type: str
    start: int  # la posición de su marca `[verificar]` en el texto, nunca el tramo original
    end: int


@dataclass(frozen=True)
class Suggestion:
    text: str  # ya renderizado, con sus marcas
    findings: tuple[Finding, ...] = ()
    evidence_expires_at: datetime | None = None
    id: UUID = field(default_factory=new_id)


def redacted(tool: str, raw: object) -> dict[str, str | int]:
    """Solo los campos que la tool declara (una clave que inventa el modelo no se guarda): un
    UUID tal cual; cualquier otro texto, su longitud; lo demás, nada."""
    spec = registry.TOOLS.get(tool)
    if spec is None or type(raw) is not dict:
        return {}
    values = {name: raw.get(name) for name in spec.fields}
    return {
        name: str(UUID(value)) if CANONICAL.fullmatch(value) else len(value)
        for name, value in values.items()
        if isinstance(value, str)
    }


def _action(run: UUID, action: Action) -> list[Any]:
    tool = action.tool if action.tool in registry.TOOLS else "unknown"  # un nombre, nunca texto
    arguments = json.dumps(redacted(action.tool, action.arguments))
    row = [run, action.tool_use_id, tool, action.code, arguments]
    if (found := action.evidence) is None:
        return [*row, None, None, None, [], "{}"]
    facts = json.dumps(dict(found.facts))  # lo exacto para el vendedor; nunca va al modelo
    return [*row, found.evidence_id, found.kind, found.as_of, list(found.subjects), facts]


def save(
    ctx: TenantContext, run: Run, actions: Sequence[Action], suggestion: Suggestion | None
) -> UUID:
    """Todo o nada. Audita `ai.suggestion.created` o `ai.summary.created` si hay borrador."""
    if ctx.actor_type != ActorType.USER or ctx.user_id is None:
        raise ValueError("records.save: el copiloto lo pide una persona")
    # Un punto de guardado propio: dentro del `tenant_scope` de quien llama, un fallo aquí no
    # deja nada aunque esa transacción siga y se confirme.
    with tenant_scope(ctx), transaction.atomic(using=require_scope(ctx)):
        _write(ctx, run, actions, suggestion)
    return run.id


def _write(
    ctx: TenantContext, run: Run, actions: Sequence[Action], suggestion: Suggestion | None
) -> None:
    with connections[require_scope(ctx)].cursor() as cursor:
        cursor.execute(_RUN, [
            run.id, run.purpose, run.conversation_id, run.outcome, len(actions),
            json.dumps(dict(run.findings)), list(run.reservation_ids), run.retried,
            run.handoff_reason, run.duration_ms,
        ])  # fmt: skip
        if actions:
            cursor.executemany(_ACTION, [_action(run.id, action) for action in actions])
        if suggestion is not None:
            findings = [asdict(finding) for finding in suggestion.findings]
            cursor.execute(_SUGGESTION, [
                suggestion.id, run.id, run.conversation_id, run.purpose, suggestion.text,
                json.dumps(findings), run.handoff_reason, suggestion.evidence_expires_at,
            ])  # fmt: skip
            ids = {"conversation_id": str(run.conversation_id), "run_id": str(run.id)}
            action = "ai.suggestion.created" if run.purpose == "SUGGEST" else "ai.summary.created"
            record(ctx, action, Entity("ai_suggestion", suggestion.id), metadata=ids)


def inserted(
    ctx: TenantContext, suggestion_id: UUID, conversation_id: UUID, findings: Mapping[str, int]
) -> None:
    """Audita `ai.suggestion.inserted` en la transacción **de quien llama**: si no se confirma, el
    texto no sale. Ids y cuántos hallazgos por tipo; nunca el texto."""
    ids = {"conversation_id": str(conversation_id), "findings": dict(findings)}
    record(ctx, "ai.suggestion.inserted", Entity("ai_suggestion", suggestion_id), metadata=ids)


def purge_text(ctx: TenantContext, days: int, batch: int) -> int:
    """Pone a NULL el texto de hasta `batch` borradores de más de `days` días de la organización
    de `ctx`, en su propio scope; los hallazgos y los ids quedan. Devuelve cuántos."""
    with tenant_scope(ctx), connections[require_scope(ctx)].cursor() as cursor:
        cursor.execute(PURGE, [days, batch])
        return int(cursor.rowcount)
