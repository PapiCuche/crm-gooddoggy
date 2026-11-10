"""Comandos de tareas (F5-08a; E10, modelo de datos §E.6): crear, cambiar, reasignar, completar,
cancelar, reabrir y borrar. No comprueban permisos: los declarará la API (F5-08b), la única que los
importa (contrato de import-linter). Validan los tipos exactos (`ValueError`, nunca `TypeError` ni
`IntegrityError`) y auditan con el antes y el después; el texto libre (título, descripción, motivo)
se audita como `[texto]`. Sin cambios no escriben ni auditan. Bloqueos: contacto y oportunidad
(`FOR SHARE`), la tarea y el miembro (`FOR NO KEY UPDATE`).
"""

import datetime as dt
from typing import Any
from uuid import UUID

from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone

from apps.audit.services import Entity, record
from apps.contacts.models import Contact
from apps.deals.models import Opportunity, Priority
from apps.organizations.selectors import lock_active_member
from apps.tasks.models import Task, TaskSource, TaskStatus, TaskType
from core.tenancy.context import TenantContext
from core.tenancy.scope import require_scope
from core.text import line

TITLE_MAX, REASON_MAX, DESCRIPTION_MAX = 200, 500, 5000
_FIELDS = frozenset(
    {"type", "title", "description", "due_at", "is_all_day", "reminder_at", "priority"}
)
_OPEN = (TaskStatus.PENDING, TaskStatus.IN_PROGRESS)
_REFERENCES = {  # FK compuesta violada → el campo que la causó
    "tasks_contact_fk": "contact_id",
    "tasks_opportunity_fk": "opportunity_id",
    "tasks_assigned_fk": "assigned_user_id",
    "tasks_completed_by_fk": "completed_by_user_id",  # quien actúa no es miembro
    "tasks_created_by_fk": "created_by_user_id",
    "tasks_updated_by_fk": "updated_by_user_id",
    "tasks_deleted_by_fk": "deleted_by_user_id",
}
_EARLIEST, _LATEST = dt.datetime(2000, 1, 1, tzinfo=dt.UTC), dt.datetime(2100, 1, 1, tzinfo=dt.UTC)


class StaleVersion(Exception):
    """Otra escritura cambió la tarea después de la versión que se editaba."""


class BadReference(ValueError):
    """Una referencia que no sirve (contacto, oportunidad, responsable o quien actúa). `field`
    dice cuál."""

    def __init__(self, field: str) -> None:
        super().__init__(field)
        self.field = field


class TaskClosed(Exception):
    """La tarea está completada o cancelada: no se edita ni se reasigna sin reabrirla."""


class TaskOpen(Exception):
    """La tarea sigue abierta: solo se reabre una completada o cancelada."""


def _uuid(value: Any, what: str) -> UUID:
    if isinstance(value, UUID):
        return value
    try:
        return UUID(value)
    except ValueError, TypeError, AttributeError:
        raise ValueError(f"{what}: un identificador") from None


def _moment(value: Any, what: str) -> dt.datetime:
    if not (
        isinstance(value, dt.datetime)
        and value.utcoffset() is not None
        and _EARLIEST <= value <= _LATEST
    ):
        raise ValueError(f"{what}: fecha y hora con zona horaria, entre los años 2000 y 2100")
    return value.astimezone(dt.UTC)


def _text(value: Any, what: str, limit: int, *, lines: bool = False) -> str:
    """Texto sin caracteres de control (una línea, o varias en la descripción), hasta `limit`."""
    if not isinstance(value, str) or len(value) > limit * 4:
        raise ValueError(f"{what}: un texto de hasta {limit} caracteres")
    if lines:
        text = "\n".join(line(part, limit) for part in value.splitlines()).strip()
        if len(text) > limit:
            raise ValueError(f"{what}: hasta {limit} caracteres")
        return text
    return line(value, limit)


def _values(fields: dict[str, Any]) -> dict[str, Any]:
    """Lo que se envía de una tarea, limpio y validado (solo esos campos)."""
    values = dict(fields)
    if "title" in values:
        values["title"] = _text(values["title"], "título", TITLE_MAX)
        if not values["title"]:
            raise ValueError("título: no puede estar vacío")
    if "description" in values:
        values["description"] = _text(
            values["description"], "descripción", DESCRIPTION_MAX, lines=True
        )
    for field, choices in (("type", TaskType.values), ("priority", Priority.values)):
        if field in values and not (isinstance(values[field], str) and values[field] in choices):
            raise ValueError(f"{field}: uno de {', '.join(choices)}")
    if "is_all_day" in values and not isinstance(values["is_all_day"], bool):
        raise ValueError("todo el día: verdadero o falso")
    for field, what in (("due_at", "vencimiento"), ("reminder_at", "recordatorio")):
        if values.get(field) is not None:
            values[field] = _moment(values[field], what)
    return values


def _audited(field: str, value: Any) -> Any:
    return "[texto]" if field in ("title", "description", "cancel_reason") and value else value


def _entity(task: Task) -> Entity:
    return Entity("task", task.pk)


def _reference(error: IntegrityError) -> Exception:
    for constraint, field in _REFERENCES.items():
        if constraint in str(error):
            return BadReference(field)
    raise error


def _locked(alias: str, task_id: UUID) -> Task:
    live = Task.objects.using(alias).filter(deleted_at__isnull=True)
    found: Task = live.select_for_update(no_key=True).get(pk=_uuid(task_id, "tarea"))
    return found


def _stamp(ctx: TenantContext, alias: str, task: Task, changes: dict[str, Any]) -> None:
    """Guarda `changes` (`{campo: [antes, después]}`) y sube la versión."""
    for field, (_, value) in changes.items():
        setattr(task, field, value)
    task.updated_by_user_id = ctx.user_id
    task.version = F("version") + 1
    task.save(using=alias, update_fields=[*changes, "updated_by_user_id", "version", "updated_at"])
    task.refresh_from_db(using=alias, fields=["version"])


def _change(
    ctx: TenantContext, task_id: UUID, version: int, action: str, change: Any, *, closed: bool
) -> Task:
    """Bloquea la tarea, comprueba la versión y el estado (`closed`: se exige cerrada; si no,
    abierta), calcula `{campo: nuevo}` con `change(alias, task)` y, si algo cambia, lo guarda y
    audita."""
    alias = require_scope(ctx)
    if isinstance(version, bool) or not isinstance(version, int) or not 1 <= version < 2**31:
        raise ValueError("versión: un entero de 1 a 2147483647")
    try:
        with transaction.atomic(using=alias):
            task = _locked(alias, task_id)
            if task.version != version:
                raise StaleVersion(task.pk)
            if (task.status in _OPEN) == closed:
                raise TaskOpen(task.pk) if closed else TaskClosed(task.pk)
            wanted = change(alias, task)
            if diff := {
                f: [getattr(task, f), v] for f, v in wanted.items() if getattr(task, f) != v
            }:
                _stamp(ctx, alias, task, diff)
                record(
                    ctx,
                    action,
                    _entity(task),
                    {k: [_audited(k, a), _audited(k, b)] for k, (a, b) in diff.items()},
                )
    except IntegrityError as error:
        raise _reference(error) from None
    return task


def _links(alias: str, contact_id: UUID | None, opportunity_id: UUID | None) -> UUID | None:
    """Bloquea el contacto y la oportunidad vivos (`FOR SHARE`) y devuelve el contacto de la tarea:
    el dado o, sin él, el de la oportunidad, que ha de ser de ese contacto."""
    if opportunity_id is not None:
        live = Opportunity.objects.using(alias).filter(pk=opportunity_id, deleted_at__isnull=True)
        found = live.select_for_update(no_key=True).values_list("contact_id", flat=True)[:1]
        if not found or contact_id not in (None, found[0]):
            raise BadReference("opportunity_id")
        contact_id = found[0]
    if contact_id is not None:
        live_contact = Contact.objects.using(alias).filter(pk=contact_id, deleted_at__isnull=True)
        if not live_contact.select_for_update(no_key=True).values_list("pk", flat=True)[:1]:
            raise BadReference("contact_id")
    return contact_id


def create_task(
    ctx: TenantContext,
    *,
    title: str,
    assigned_user_id: UUID | None = None,
    contact_id: UUID | None = None,
    opportunity_id: UUID | None = None,
    **fields: Any,
) -> Task:
    """Crea una tarea abierta (`PENDING`, de origen manual) de quien actúa, o del responsable que
    se diga (miembro activo), con vínculo opcional a un contacto vivo y a una oportunidad viva (que
    ha de ser de ese contacto; sin contacto, se toma el de la oportunidad). Audita `task.created`.
    `BadReference` con el campo; `ValueError`."""
    alias = require_scope(ctx)
    if unknown := set(fields) - _FIELDS:
        raise ValueError(f"campos no admitidos: {sorted(unknown)}")
    values = _values({"title": title, **fields})
    owner = ctx.user_id if assigned_user_id is None else _uuid(assigned_user_id, "responsable")
    contact_id = None if contact_id is None else _uuid(contact_id, "contacto")
    opportunity_id = None if opportunity_id is None else _uuid(opportunity_id, "oportunidad")
    try:
        with transaction.atomic(using=alias):
            contact_id = _links(alias, contact_id, opportunity_id)
            if owner is not None and not lock_active_member(owner):
                raise BadReference("assigned_user_id")
            task: Task = Task.objects.using(alias).create(
                assigned_user_id=owner, contact_id=contact_id, opportunity_id=opportunity_id,
                status=TaskStatus.PENDING, source=TaskSource.MANUAL,
                created_by_user_id=ctx.user_id, updated_by_user_id=ctx.user_id, **values,
            )  # fmt: skip
            shown = {**values, "assigned_user_id": owner, "contact_id": contact_id,
                     "opportunity_id": opportunity_id, "status": TaskStatus.PENDING}  # fmt: skip
            record(
                ctx,
                "task.created",
                _entity(task),
                {k: [None, _audited(k, v)] for k, v in shown.items()},
            )
    except IntegrityError as error:
        raise _reference(error) from None
    return task


def update_task(ctx: TenantContext, *, task_id: UUID, version: int, **fields: Any) -> Task:
    """Cambia lo que se envía de una tarea abierta (`status`: `PENDING` o `IN_PROGRESS`) si nadie
    la cambió desde `version`, y audita `task.updated`. `DoesNotExist`; `StaleVersion`;
    `TaskClosed`; `ValueError`."""
    if unknown := set(fields) - _FIELDS - {"status"}:
        raise ValueError(f"campos no admitidos: {sorted(unknown)}")
    status = fields.pop("status", None)
    if status is not None and status not in _OPEN:
        raise ValueError("estado: PENDING o IN_PROGRESS (completar y cancelar tienen su comando)")
    wanted = _values(fields) | ({} if status is None else {"status": status})
    return _change(ctx, task_id, version, "task.updated", lambda alias, task: wanted, closed=False)


def assign_task(ctx: TenantContext, *, task_id: UUID, version: int, assigned_user_id: UUID) -> Task:
    """Deja la tarea abierta con ese responsable, miembro activo, y audita `task.assigned`."""
    owner = _uuid(assigned_user_id, "responsable")

    def change(alias: str, task: Task) -> dict[str, Any]:
        if owner != task.assigned_user_id and not lock_active_member(owner):
            raise BadReference("assigned_user_id")
        return {"assigned_user_id": owner}

    return _change(ctx, task_id, version, "task.assigned", change, closed=False)


def complete_task(ctx: TenantContext, *, task_id: UUID, version: int) -> Task:
    """Completa una tarea abierta: `DONE`, con su fecha y quien la completó (`task.completed`)."""
    done = {
        "status": TaskStatus.DONE,
        "completed_at": timezone.now(),
        "completed_by_user_id": ctx.user_id,
    }
    return _change(ctx, task_id, version, "task.completed", lambda alias, task: done, closed=False)


def cancel_task(
    ctx: TenantContext, *, task_id: UUID, version: int, reason: str | None = None
) -> Task:
    """Cancela una tarea abierta, con un motivo opcional (`task.cancelled`)."""
    why = None if reason is None else _text(reason, "motivo", REASON_MAX) or None
    gone = {"status": TaskStatus.CANCELLED, "cancel_reason": why}
    return _change(ctx, task_id, version, "task.cancelled", lambda alias, task: gone, closed=False)


def reopen_task(ctx: TenantContext, *, task_id: UUID, version: int) -> Task:
    """Reabre una tarea completada o cancelada (`PENDING`, sin fecha ni motivo: `task.reopened`)."""
    back = {"status": TaskStatus.PENDING, "completed_at": None, "completed_by_user_id": None,
            "cancel_reason": None}  # fmt: skip
    return _change(ctx, task_id, version, "task.reopened", lambda alias, task: back, closed=True)


def delete_task(ctx: TenantContext, *, task_id: UUID) -> None:
    """Borra una tarea (borrado lógico, abierta o cerrada) y lo audita (`task.deleted`)."""
    alias = require_scope(ctx)
    try:
        with transaction.atomic(using=alias):
            task = _locked(alias, task_id)
            gone = {"deleted_at": [None, timezone.now()], "deleted_by_user_id": [None, ctx.user_id]}
            _stamp(ctx, alias, task, gone)
            record(ctx, "task.deleted", _entity(task))
    except IntegrityError as error:
        raise _reference(error) from None
