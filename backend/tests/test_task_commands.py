"""F5-08a: la tabla `tasks` y sus comandos, con PostgreSQL real y el rol `crm_app`, también con dos
transacciones a la vez. Sin API: llega con F5-08b."""

import json
from datetime import UTC, datetime
from functools import partial
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from django.core.management import call_command
from django.db import DatabaseError, IntegrityError, ProgrammingError, connection, transaction
from django.utils import timezone

from apps.contacts.commands import delete_contact
from apps.contacts.models import Contact
from apps.deals.models import Opportunity
from apps.deals.opportunities import delete_opportunity
from apps.tasks import commands
from apps.tasks.commands import BadReference, StaleVersion, TaskClosed, TaskOpen
from apps.tasks.models import Task
from core.tenancy.scope import tenant_scope
from tests import test_opportunities_tables as tables
from tests.factories import make_user
from tests.test_inventory_stock import race
from tests.test_memberships import ctx, join
from tests.test_opportunity_commands import change
from tests.test_pipelines_tables import as_migrator

world = tables.world
pytestmark = pytest.mark.usefixtures("tenant_db")
type DB = psycopg.Connection[Any]
DUE = datetime(2026, 11, 20, 15, 0, tzinfo=UTC)
NOW = timezone.now()
SECRET = "Llamar a Juan Perez, DNI 12345678"  # noqa: S105 — texto de prueba


def run(world: SimpleNamespace, command: Any, org: str = "a", **fields: Any) -> Any:
    me = ctx(getattr(world, org), world.ana if org == "a" else None)
    with tenant_scope(me) as tenant:
        return command(tenant, **fields)


def new(world: SimpleNamespace, **fields: Any) -> Task:
    made: Task = run(world, commands.create_task, **{"title": "Llamar", **fields})
    return made


def move(world: SimpleNamespace, command: Any, task: Any, version: int = 1, **fields: Any) -> Task:
    done: Task = run(world, command, task_id=task.pk, version=version, **fields)
    return done


def reload(world: SimpleNamespace, pk: Any) -> Task:
    with tenant_scope(ctx(world.a)):
        return Task.objects.get(pk=pk)  # type: ignore[no-any-return]


def audit(migrator: DB) -> list[tuple[str, dict[str, Any]]]:
    found = migrator.execute(
        "SELECT action, changes::text FROM audit_logs WHERE entity_type = 'task'"
        " ORDER BY occurred_at, id"
    ).fetchall()
    return [(action, json.loads(changes)) for action, changes in found]


def test_create_opens_a_pending_task_of_the_actor_and_audits_without_the_text(
    world: SimpleNamespace, migrator: DB
) -> None:
    made = new(world, title=SECRET, description=f"  {SECRET}\n\nOtra línea ", due_at=DUE,
               type="CALL", priority="HIGH", is_all_day=True)  # fmt: skip
    assert (made.status, made.type, made.priority, made.source, made.version) == (
        "PENDING", "CALL", "HIGH", "MANUAL", 1,
    )  # fmt: skip
    assert made.assigned_user_id == made.created_by_user_id == world.ana.pk
    assert (made.title, made.description, made.due_at, made.is_all_day) == (
        SECRET, f"{SECRET}\n\nOtra línea", DUE, True,
    )  # fmt: skip
    [(action, changes)] = audit(migrator)
    assert action == "task.created" and changes["title"] == changes["description"] == [
        None,
        "[texto]",
    ]
    assert changes["status"] == [None, "PENDING"] and "12345678" not in json.dumps(changes)
    minimal = new(world)
    assert (minimal.type, minimal.due_at, minimal.contact_id, minimal.opportunity_id) == (
        "FOLLOW_UP", None, None, None,
    )  # fmt: skip
    pedro = make_user()
    join(world.a, pedro)
    assert new(world, assigned_user_id=pedro.pk).assigned_user_id == pedro.pk


def test_the_links_take_the_contact_of_the_opportunity_and_both_must_agree(
    world: SimpleNamespace,
) -> None:
    opp = tables.make(world.a)
    by_opportunity = new(world, opportunity_id=opp.pk)
    assert (by_opportunity.opportunity_id, by_opportunity.contact_id) == (opp.pk, opp.contact_id)
    both = new(world, opportunity_id=opp.pk, contact_id=opp.contact_id)
    assert both.contact_id == opp.contact_id
    other = tables.contact(world.a).pk
    with pytest.raises(BadReference) as bad:
        new(world, opportunity_id=opp.pk, contact_id=other)  # la oportunidad es de otro contacto
    assert bad.value.field == "opportunity_id"
    assert new(world, contact_id=other).contact_id == other


@pytest.mark.parametrize(
    ("field", "make_bad"),
    [
        ("contact_id", lambda w: tables.contact(w.b).pk),  # de otra organización
        ("contact_id", lambda w: change(w, Contact, tables.contact(w.a).pk, deleted_at=NOW)),
        ("contact_id", lambda w: uuid4()),
        ("opportunity_id", lambda w: tables.make(w.b).pk),
        ("opportunity_id", lambda w: change(w, Opportunity, tables.make(w.a).pk, deleted_at=NOW)),
        ("assigned_user_id", lambda w: w.luis.pk),  # no es miembro de A
        ("assigned_user_id", lambda w: join(w.a, make_user(), "SUSPENDED").user_id),
    ],
)  # fmt: skip
def test_a_reference_that_does_not_fit_writes_nothing(
    world: SimpleNamespace, migrator: DB, field: str, make_bad: Any
) -> None:
    bad = make_bad(world)
    with pytest.raises(BadReference) as refused:
        new(world, **{field: bad})
    assert refused.value.field == field
    assert (
        migrator.execute("SELECT count(*) FROM tasks").fetchone() == (0,) and audit(migrator) == []
    )


@pytest.mark.parametrize(
    "bad",
    [
        {"title": ""}, {"title": "  \n "}, {"title": 5}, {"title": "x" * 20_000},
        {"description": 5}, {"description": "x" * 5001}, {"type": "NOPE"}, {"type": None},
        {"priority": "ALTA"}, {"is_all_day": "no"}, {"due_at": datetime(2026, 11, 1)},
        {"due_at": "2026-11-01"}, {"reminder_at": datetime(3000, 1, 1, tzinfo=UTC)},
        {"assigned_user_id": "x"}, {"contact_id": 5}, {"opportunity_id": "x"},
        {"status": "DONE"}, {"source": "AI"}, {"completed_at": DUE}, {"organization_id": uuid4()},
    ],
)  # fmt: skip
def test_hostile_values_are_a_value_error_and_nothing_is_written(
    world: SimpleNamespace, migrator: DB, bad: dict[str, Any]
) -> None:
    with pytest.raises(ValueError):  # noqa: PT011 — el motivo lo dice el comando
        new(world, **bad)
    assert migrator.execute("SELECT count(*) FROM tasks").fetchone() == (0,)


def test_update_changes_what_is_sent_audits_it_and_a_noop_writes_nothing(
    world: SimpleNamespace, migrator: DB
) -> None:
    made = new(world, due_at=DUE)
    changed = move(world, commands.update_task, made, title=SECRET, priority="URGENT",
                   status="IN_PROGRESS", due_at=None, is_all_day=True)  # fmt: skip
    assert (changed.version, changed.status, changed.priority, changed.due_at) == (
        2, "IN_PROGRESS", "URGENT", None,
    )  # fmt: skip
    assert changed.updated_by_user_id == world.ana.pk and changed.is_all_day
    [_, (action, changes)] = audit(migrator)
    assert action == "task.updated" and changes["title"] == ["[texto]", "[texto]"]
    assert changes["status"] == ["PENDING", "IN_PROGRESS"] and "12345678" not in json.dumps(changes)
    assert reload(world, made.pk).title == SECRET
    again = move(world, commands.update_task, made, version=2, priority="URGENT")
    assert again.version == 2 and len(audit(migrator)) == 2  # sin cambios: ni escribe ni audita
    for bad in (
        {"type": "X"},
        {"status": "DONE"},
        {"assigned_user_id": world.ana.pk},
        {"completed_at": DUE},
    ):
        with pytest.raises(ValueError):  # noqa: PT011
            move(world, commands.update_task, made, version=2, **bad)


def test_a_stale_version_a_closed_task_and_a_missing_one_are_refused(
    world: SimpleNamespace,
) -> None:
    made = new(world)
    with pytest.raises(StaleVersion):
        move(world, commands.update_task, made, version=7, title="x")
    with pytest.raises(ValueError):  # noqa: PT011
        move(world, commands.update_task, made, version=0, title="x")
    move(world, commands.complete_task, made)
    for command, extra in (
        (commands.update_task, {"title": "x"}),
        (commands.assign_task, {"assigned_user_id": world.ana.pk}),
        (commands.complete_task, {}),
        (commands.cancel_task, {}),
    ):
        with pytest.raises(TaskClosed):
            move(world, command, made, version=2, **extra)
    with pytest.raises(TaskOpen):
        move(world, commands.reopen_task, new(world))
    with pytest.raises(Task.DoesNotExist):
        run(world, commands.complete_task, task_id=uuid4(), version=1)


def test_assign_needs_an_active_member_and_the_same_owner_writes_nothing(
    world: SimpleNamespace, migrator: DB
) -> None:
    made, pedro = new(world), make_user()
    join(world.a, pedro)
    moved = move(world, commands.assign_task, made, assigned_user_id=pedro.pk)
    assert (moved.assigned_user_id, moved.version) == (pedro.pk, 2)
    assert [a for a, _ in audit(migrator)] == ["task.created", "task.assigned"]
    same = move(world, commands.assign_task, made, version=2, assigned_user_id=pedro.pk)
    assert same.version == 2 and len(audit(migrator)) == 2
    for who in (world.luis.pk, join(world.a, make_user(), "SUSPENDED").user_id):
        with pytest.raises(BadReference):
            move(world, commands.assign_task, made, version=2, assigned_user_id=who)
    assert reload(world, made.pk).assigned_user_id == pedro.pk


def test_complete_cancel_and_reopen_keep_their_date_author_and_audit(
    world: SimpleNamespace, migrator: DB
) -> None:
    first = move(world, commands.complete_task, new(world))
    assert (first.status, first.completed_by_user_id, first.version) == ("DONE", world.ana.pk, 2)
    assert first.completed_at is not None
    back = move(world, commands.reopen_task, first, version=2)
    assert (back.status, back.completed_at, back.completed_by_user_id) == ("PENDING", None, None)
    gone = move(world, commands.cancel_task, back, version=3, reason=f"  {SECRET} ")
    assert (gone.status, gone.cancel_reason, gone.completed_at) == ("CANCELLED", SECRET, None)
    reopened = move(world, commands.reopen_task, gone, version=4)
    assert (reopened.status, reopened.cancel_reason) == ("PENDING", None)
    plain = move(world, commands.cancel_task, new(world))  # el motivo es opcional
    assert plain.cancel_reason is None
    assert [a for a, _ in audit(migrator)][:5] == [
        "task.created", "task.completed", "task.reopened", "task.cancelled", "task.reopened",
    ]  # fmt: skip
    cancelled = [c for a, c in audit(migrator) if a == "task.cancelled"][0]
    assert cancelled["cancel_reason"] == [None, "[texto]"] and "12345678" not in json.dumps(
        cancelled
    )


def test_delete_is_logical_audited_and_the_task_stops_existing(
    world: SimpleNamespace, migrator: DB
) -> None:
    made = new(world)
    run(world, commands.delete_task, task_id=made.pk)
    assert migrator.execute(
        "SELECT deleted_at IS NOT NULL, deleted_by_user_id FROM tasks"
    ).fetchone() == (True, world.ana.pk)
    assert [a for a, _ in audit(migrator)] == ["task.created", "task.deleted"]
    again: list[tuple[Any, dict[str, Any]]] = [
        (commands.delete_task, {}), (commands.complete_task, {"version": 2})
    ]  # fmt: skip
    for command, extra in again:
        with pytest.raises(Task.DoesNotExist):
            run(world, command, task_id=made.pk, **extra)


def test_a_task_of_a_never_exists_for_b_and_the_database_keeps_its_rules(
    world: SimpleNamespace,
) -> None:
    mine = new(world, contact_id=tables.contact(world.a).pk)
    with tenant_scope(ctx(world.b)):
        assert Task.objects.count() == 0 and not Task.objects.filter(pk=mine.pk).exists()
        with (
            connection.cursor() as cursor,
            pytest.raises(ProgrammingError, match="permission denied"),
        ):
            cursor.execute("DELETE FROM tasks")  # `crm_app` no borra
    with (
        tenant_scope(ctx(world.b)),
        connection.cursor() as cursor,
        pytest.raises(ProgrammingError, match="permission denied"),
    ):
        cursor.execute("TRUNCATE tasks")  # ni vacía la tabla
    foreign = tables.contact(world.b).pk
    for fields, constraint in (
        ({"contact_id": foreign}, "tasks_contact_fk"),
        ({"opportunity_id": tables.make(world.b).pk}, "tasks_opportunity_fk"),
        ({"assigned_user_id": world.luis.pk}, "tasks_assigned_fk"),
        ({"status": "DONE"}, "tasks_closed_ck"),  # completada sin fecha
        ({"status": "PENDING", "cancel_reason": "x"}, "tasks_closed_ck"),
        ({"status": "CANCELLED", "completed_at": DUE}, "tasks_closed_ck"),
        (
            {"status": "DONE", "completed_at": DUE, "completed_by_user_id": world.luis.pk},
            "tasks_completed_by_fk",
        ),
        ({"created_by_user_id": world.luis.pk}, "tasks_created_by_fk"),  # ni una persona ajena
        ({"updated_by_user_id": world.luis.pk}, "tasks_updated_by_fk"),
        ({"deleted_by_user_id": world.luis.pk}, "tasks_deleted_by_fk"),
        ({"priority": "ASAP"}, "tasks_priority_ck"),
        ({"source": "WEB"}, "tasks_source_ck"),
        ({"status": "SOON"}, "tasks_(status|closed)_ck"),
        ({"type": "X"}, "tasks_type_ck"),
        ({"title": "  "}, "tasks_title_ck"),
        ({"version": 0}, "tasks_version_ck"),
    ):
        with tenant_scope(ctx(world.a)), pytest.raises(IntegrityError, match=constraint):
            with transaction.atomic():
                Task.objects.filter(pk=mine.pk).update(**fields)


ROW = (  # una fila completa, de la organización que se diga
    "INSERT INTO tasks (id, organization_id, type, title, description, is_all_day, status,"
    " priority, source, version, created_at, updated_at) VALUES (%s, %s, 'CALL', 'x', '', false,"
    " 'PENDING', 'LOW', 'MANUAL', 1, now(), now())"
)


def sql(org: Any, statement: str, params: list[Any] | None = None) -> list[Any]:
    with tenant_scope(ctx(org)), connection.cursor() as cursor:
        cursor.execute(statement, params)
        return cursor.fetchall() if cursor.description else [(cursor.rowcount,)]


def test_row_level_security_itself_isolates_the_tasks_and_the_organization_is_a_real_reference(
    world: SimpleNamespace, migrator: DB
) -> None:
    new(world)
    assert sql(world.a, "SELECT count(*) FROM tasks") == [(1,)]
    assert sql(world.b, "SELECT count(*) FROM tasks") == [(0,)]  # la base, no solo el manager
    assert sql(world.b, "UPDATE tasks SET title = 'otra'") == [(0,)]  # ni las cambia
    with pytest.raises(DatabaseError, match="row-level security"):  # ni las escribe en A
        sql(world.b, ROW, [uuid4(), world.a])
    with pytest.raises(DatabaseError, match="row-level security"):  # ni las mueve de organización
        sql(world.a, "UPDATE tasks SET organization_id = %s", [world.b])
    with pytest.raises(psycopg.errors.ForeignKeyViolation, match="tasks_organization_fk"):
        migrator.execute(ROW, [uuid4(), uuid4()])  # una organización que no existe


def test_two_completes_at_once_leave_one_and_a_stale_version(world: SimpleNamespace) -> None:
    made = new(world)
    complete = partial(commands.complete_task, task_id=made.pk, version=1)
    first, second = race(world.a, world.ana, [complete, complete])
    assert first.status == "DONE" and second == "StaleVersion"
    assert reload(world, made.pk).version == 2


def test_deleting_the_contact_or_the_opportunity_at_once_never_leaves_a_task_linked(
    world: SimpleNamespace,
) -> None:
    for gone, field in (
        (partial(delete_contact, contact_id=tables.contact(world.a).pk), "contact_id"),
        (partial(delete_opportunity, opportunity_id=tables.make(world.a).pk), "opportunity_id"),
    ):
        target = gone.keywords.get("contact_id") or gone.keywords["opportunity_id"]
        create = partial(commands.create_task, title="x", **{field: target})
        assert (
            race(world.a, world.ana, [gone, create])[1] == "BadReference"
        )  # el alta espera y falla


def test_the_layers_contract_allows_only_the_two_edges_tasks_needs() -> None:
    import tomllib
    from pathlib import Path

    toml = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())
    layers = next(c for c in toml["tool"]["importlinter"]["contracts"] if c["type"] == "layers")
    assert "apps.tasks" in layers["layers"][0].split(" | ")  # en la fila de deals, no encima
    edges = {"apps.tasks.models -> apps.deals.models", "apps.tasks.commands -> apps.deals.models"}
    assert edges <= set(layers["ignore_imports"])  # y ninguna otra de tasks
    assert not [
        e for e in layers["ignore_imports"] if e.startswith("apps.tasks") and e not in edges
    ]


def test_the_migration_goes_back_and_forward(world: SimpleNamespace, migrator: DB) -> None:
    exists = "SELECT to_regclass('public.tasks') IS NOT NULL"
    assert migrator.execute(exists).fetchone() == (True,)
    with as_migrator():
        call_command("migrate", "tasks", "zero", verbosity=0)
    assert migrator.execute(exists).fetchone() == (False,)
    with as_migrator():
        call_command("migrate", verbosity=0)
    assert migrator.execute(exists).fetchone() == (True,)
    assert migrator.execute(
        "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = 'tasks'"
    ).fetchone() == (True, True)
    assert migrator.execute(
        "SELECT count(*) FROM pg_constraint WHERE conrelid = 'tasks'::regclass AND contype = 'f'"
    ).fetchone() == (8,)
