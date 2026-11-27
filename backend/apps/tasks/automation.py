"""Entrada del sistema a las tareas (F11-03; ADR-049 §4, §6): crear y cancelar las tareas de una
automatización. Solo la importa `apps.automations` (contrato `protected`) y no llega a nada que
envíe mensajes o cambie estados (contrato `forbidden` de ADR-049 §1).

- Actúa el sistema: solo un contexto `SYSTEM` sin persona (`user_id` y `actor_id` nulos). No
  comprueba permisos de persona porque no actúa ninguna; la organización sale del contexto y las
  filas, de RLS.
- Texto fijo por regla y paso (`TEMPLATES`): solo se rellena el número del sujeto, leído de la
  base. El contacto (y la oportunidad de la cotización) salen del sujeto, nunca de quien llama.
- Como mucho `MAX_RECIPIENTS` responsables distintos, cada uno miembro activo
  (`lock_active_member`); si uno no lo es, no se crea ninguna.
- Sin idempotencia propia: la da la clave única de `automation_runs`, que quien llama inserta
  (`ON CONFLICT DO NOTHING`) en la misma transacción antes de crear. En `SIMULATE` no se llama.
- Auditoría con ids y códigos: `task.created` y `task.cancelled`, con la regla, el paso y la
  ejecución en `metadata`.
"""

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from django.db import IntegrityError, transaction

from apps.audit.services import Entity, record
from apps.conversations.models import Conversation
from apps.organizations.selectors import lock_active_member
from apps.quotes.models import Quote
from apps.tasks.commands import BadReference, _audited, _links, _moment, _stamp, _uuid
from apps.tasks.models import Task, TaskSource, TaskStatus, TaskType
from core.tenancy.context import ActorType, TenantContext
from core.tenancy.scope import require_scope

MAX_RECIPIENTS = 3  # D-F11-04: ningún aviso pasa de 3 personas
_OPEN = (TaskStatus.PENDING, TaskStatus.IN_PROGRESS)
_LINKS = {"tasks_conversation_fk": "conversation_id", "tasks_quote_fk": "quote_id",
          "tasks_contact_fk": "contact_id", "tasks_opportunity_fk": "opportunity_id",
          "tasks_assigned_fk": "assigned_user_id"}  # fmt: skip


@dataclass(frozen=True)
class Template:
    """Lo fijo de una tarea automática; `{number}` es el número del sujeto. `closed`: el motivo
    de cancelación por código."""

    subject: str  # `conversation` o `quote`
    type: str
    priority: str
    title: str
    description: str
    closed: dict[str, str]


_CONVERSATION_CLOSED = {"RESOLVED": "La conversación ya se respondió o se cerró",
                        "RULE_CHANGED": "La automatización cambió"}  # fmt: skip
_QUOTE_CLOSED = {"RESOLVED": "La cotización ya no espera respuesta",
                 "RULE_CHANGED": "La automatización cambió"}  # fmt: skip
TEMPLATES: dict[tuple[str, int], Template] = {
    ("conversation.unanswered", 1): Template(
        "conversation", TaskType.MESSAGE, Task.Priority.HIGH,
        "Responder a la conversación {number}",
        "El cliente espera respuesta. Tarea creada por una automatización.", _CONVERSATION_CLOSED,
    ),
    ("conversation.unanswered", 2): Template(
        "conversation", TaskType.MESSAGE, Task.Priority.URGENT,
        "La conversación {number} sigue sin respuesta",
        "Escalado: nadie respondió a tiempo. Tarea creada por una automatización.",
        _CONVERSATION_CLOSED,
    ),
    ("quote.no_response", 1): Template(
        "quote", TaskType.QUOTE, Task.Priority.MEDIUM, "Dar seguimiento a la cotización {number}",
        "El cliente no respondió a la cotización. Tarea creada por una automatización.",
        _QUOTE_CLOSED,
    ),
    ("quote.no_response", 2): Template(
        "quote", TaskType.QUOTE, Task.Priority.MEDIUM, "La cotización {number} vence pronto",
        "La cotización vence pronto sin respuesta. Tarea creada por una automatización.",
        _QUOTE_CLOSED,
    ),
}  # fmt: skip


def _system(ctx: TenantContext) -> str:
    """El alias de la organización, solo si actúa el sistema (nadie se hace pasar por él)."""
    if ctx.actor_type != ActorType.SYSTEM or ctx.user_id is not None or ctx.actor_id is not None:
        raise ValueError("solo el sistema crea o cancela tareas automáticas")
    return require_scope(ctx)


def _template(rule_key: Any, step: Any) -> Template:
    found = TEMPLATES.get((rule_key, step)) if type(step) is int else None
    if found is None:
        raise ValueError("regla y paso: uno registrado")
    return found


def _subject(alias: str, template: Template, subject_id: UUID) -> dict[str, Any]:
    """Número, contacto y oportunidad del sujeto visible bajo RLS; si no, `BadReference`."""
    field = f"{template.subject}_id"
    if template.subject == "conversation":
        rows = Conversation.objects.using(alias).filter(pk=subject_id)
        found = rows.values("number", "contact_id").first()
    else:
        quotes = Quote.objects.using(alias).filter(pk=subject_id)
        found = quotes.values("number", "contact_id", "opportunity_id").first()
    if found is None:
        raise BadReference(field)
    return {field: subject_id, **found}


def _reference(error: IntegrityError) -> Exception:
    diag = getattr(error.__cause__, "diag", None)
    if (field := _LINKS.get(getattr(diag, "constraint_name", None) or "")) is not None:
        return BadReference(field)
    raise error


def create_automation_task(
    ctx: TenantContext,
    *,
    rule_key: str,
    step: int,
    run_id: UUID,
    subject_id: UUID,
    recipients: Sequence[UUID],
    due_at: dt.datetime,
) -> list[Task]:
    """Crea, en una transacción, la tarea fija de `(rule_key, step)` para cada responsable (de 1 a
    `MAX_RECIPIENTS`, distintos, en orden de id), enlazada al sujeto y a su contacto: `PENDING`,
    `source = AUTOMATION`, sin autor. `ValueError`; `BadReference` (sujeto o responsable)."""
    alias = _system(ctx)
    template = _template(rule_key, step)
    run, subject = _uuid(run_id, "ejecución"), _uuid(subject_id, template.subject)
    if not isinstance(recipients, list | tuple) or not 1 <= len(recipients) <= MAX_RECIPIENTS:
        raise ValueError(f"responsables: una lista de 1 a {MAX_RECIPIENTS}, distintos")
    owners = sorted({_uuid(who, "responsable") for who in recipients})
    if len(owners) != len(recipients):
        raise ValueError(f"responsables: una lista de 1 a {MAX_RECIPIENTS}, distintos")
    due = _moment(due_at, "vencimiento")
    made: list[Task] = []
    try:
        with transaction.atomic(using=alias):
            found = _subject(alias, template, subject)
            number = found.pop("number")
            found["contact_id"] = _links(alias, found["contact_id"], found.get("opportunity_id"))
            values = {"type": template.type, "priority": template.priority,
                      "title": template.title.format(number=number),
                      "description": template.description, "due_at": due, **found}  # fmt: skip
            for owner in owners:
                if not lock_active_member(owner):
                    raise BadReference("assigned_user_id")
                task: Task = Task.objects.using(alias).create(
                    assigned_user_id=owner, status=TaskStatus.PENDING,
                    source=TaskSource.AUTOMATION, **values,
                )  # fmt: skip
                shown = {**values, "assigned_user_id": owner, "status": TaskStatus.PENDING,
                         "source": TaskSource.AUTOMATION}  # fmt: skip
                created = {k: [None, _audited(k, v)] for k, v in shown.items()}
                metadata = {"rule": rule_key, "step": step, "run_id": run}
                record(ctx, "task.created", Entity("task", task.pk), created, metadata,
                       actor=(ActorType.SYSTEM, None))  # fmt: skip
                made.append(task)
    except IntegrityError as error:
        raise _reference(error) from None
    return made


def cancel_automation_task(
    ctx: TenantContext, *, task_id: UUID, version: int, rule_key: str, reason: str, run_id: UUID
) -> bool:
    """Cancela una tarea automática con el motivo fijo de `reason` (`RESOLVED`, `RULE_CHANGED`)
    solo si sigue abierta y en `version` (nadie la cambió). Si no, la deja y devuelve `False`.
    `Task.DoesNotExist` si no existe, está borrada, es de otra organización o no es automática."""
    alias = _system(ctx)
    template = _template(rule_key, 1)
    if reason not in template.closed:
        raise ValueError(f"motivo: uno de {', '.join(template.closed)}")
    if isinstance(version, bool) or not isinstance(version, int) or not 1 <= version < 2**31:
        raise ValueError("versión: un entero de 1 a 2147483647")
    task_pk, run = _uuid(task_id, "tarea"), _uuid(run_id, "ejecución")
    with transaction.atomic(using=alias):
        live = Task.objects.using(alias).filter(deleted_at__isnull=True,
                                                source=TaskSource.AUTOMATION)  # fmt: skip
        task: Task = live.select_for_update(no_key=True).get(pk=task_pk)
        if task.version != version or task.status not in _OPEN:
            return False
        before = task.status
        _stamp(ctx, alias, task, {"status": [before, TaskStatus.CANCELLED],
                                  "cancel_reason": [None, template.closed[reason]]})  # fmt: skip
        changes = {"status": [before, TaskStatus.CANCELLED], "cancel_reason": [None, "[texto]"]}
        record(ctx, "task.cancelled", Entity("task", task.pk), changes,
               {"rule": rule_key, "run_id": run, "reason": reason},
               actor=(ActorType.SYSTEM, None))  # fmt: skip
    return True
