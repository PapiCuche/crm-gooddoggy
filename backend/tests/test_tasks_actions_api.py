"""F5-08b3: `POST …/tasks/{id}/assign|complete|cancel|reopen/`, con sus permisos, su alcance, la
versión, el estado y la auditoría. Middleware, sesión, motor de autorización y PostgreSQL con el
rol `crm_app`."""

from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from tests.factories import make_user
from tests.test_authorization import give
from tests.test_member_branch import DENIED
from tests.test_memberships import join
from tests.test_self_context import NOT_FOUND, reply, signed
from tests.test_tasks_read_api import TASKS
from tests.test_tasks_write_api import DB, audit, board, crm, fail, pipelines, real_stack
from tests.test_team_writes import send

__all__ = ["board", "crm", "pipelines", "real_stack"]  # las fixtures, para pytest
pytestmark = pytest.mark.usefixtures("tenant_db")
SECRET = "Cliente Juan Perez, DNI 12345678"  # noqa: S105 — texto de prueba


def act(client: Any, task: Any, name: str, **body: Any) -> Any:
    return send(client, "post", f"{TASKS}{task}/{name}/", {"version": 1, **body})


def test_complete_closes_a_task_with_its_date_its_author_and_its_audit(
    board: SimpleNamespace, migrator: DB
) -> None:
    ana, task = signed(board.ana), board.tasks["vera"]
    done = act(ana, task, "complete")
    assert done.status_code == 200, done.content
    body = done.json()
    assert (body["status"], body["version"], body["completed_by_user_id"]) == (
        "DONE", 2, str(board.ana.pk),
    )  # fmt: skip
    assert body["completed_at"] is not None and body["assigned_user_id"] == str(board.vera.pk)
    assert [(a, actor) for a, actor, _ in audit(migrator)] == [("task.completed", board.ana.pk)]
    assert ana.get(f"{TASKS}{task}/").json() == body  # y el detalle lo confirma
    assert fail(act(ana, task, "complete", version=2)) == (409, "TASK_CLOSED", [])
    assert fail(act(ana, board.tasks["ana"], "complete", version=7)) == (409, "STALE_VERSION", [])
    assert act(ana, board.tasks["ana"], "complete", extra=1).status_code == 400  # sin claves ajenas
    for bad in ({"version": 0}, {"version": "1"}, {}):
        raw = send(ana, "post", f"{TASKS}{board.tasks['ana']}/complete/", bad)
        assert raw.status_code == 400, bad


def test_cancel_keeps_an_optional_reason_without_auditing_the_text(
    board: SimpleNamespace, migrator: DB
) -> None:
    ana = signed(board.ana)
    gone = act(ana, board.tasks["vera"], "cancel", reason=f"  {SECRET} ")
    assert gone.status_code == 200 and gone.json()["status"] == "CANCELLED"
    assert gone.json()["cancel_reason"] == SECRET and gone.json()["completed_at"] is None
    [(action, _, changes)] = audit(migrator)
    assert action == "task.cancelled" and "12345678" not in changes
    plain = act(ana, board.tasks["sam"], "cancel")  # el motivo es opcional
    assert plain.json()["cancel_reason"] is None
    assert act(ana, board.tasks["ana"], "cancel", reason=None).json()["cancel_reason"] is None
    for bad in ({"reason": "x" * 501}, {"reason": 5}, {"reason": "con\ttab"}):
        status, code, fields = fail(act(ana, board.tasks["nobody"], "cancel", **bad))
        assert (status, code, fields) == (400, "VALIDATION_ERROR", ["reason"]), bad
    assert fail(act(ana, board.tasks["vera"], "cancel", version=2)) == (409, "TASK_CLOSED", [])


def test_reopen_returns_a_closed_task_to_pending_and_refuses_an_open_one(
    board: SimpleNamespace,
) -> None:
    ana, task = signed(board.ana), board.tasks["vera"]
    assert fail(act(ana, task, "reopen")) == (409, "TASK_OPEN", [])  # está abierta
    act(ana, task, "complete")
    back = act(ana, task, "reopen", version=2).json()
    kept = (back["status"], back["completed_at"], back["completed_by_user_id"], back["version"])
    assert kept == ("PENDING", None, None, 3)
    act(ana, task, "cancel", version=3, reason="x")
    again = act(ana, task, "reopen", version=4).json()
    assert (again["status"], again["cancel_reason"]) == ("PENDING", None)
    assert fail(act(ana, task, "reopen", version=2)) == (409, "STALE_VERSION", [])


def test_assign_needs_an_active_member_and_the_same_owner_writes_nothing(
    board: SimpleNamespace, migrator: DB
) -> None:
    ana, task = signed(board.ana), board.tasks["vera"]
    moved = act(ana, task, "assign", assigned_user_id=str(board.sam.pk))
    assert moved.status_code == 200 and moved.json()["assigned_user_id"] == str(board.sam.pk)
    assert moved.json()["version"] == 2
    same = act(ana, task, "assign", version=2, assigned_user_id=str(board.sam.pk))
    assert same.json()["version"] == 2 and len(audit(migrator)) == 1  # sin cambios: nada
    suspended = join(board.a, make_user(), "SUSPENDED").user_id
    for who in (board.luis.pk, suspended, uuid4()):  # de B, suspendido o inexistente
        answer = act(ana, task, "assign", version=2, assigned_user_id=str(who))
        assert fail(answer) == (400, "VALIDATION_ERROR", ["assigned_user_id"])
        assert "miembro activo" in answer.content.decode()
    assert act(ana, task, "assign", version=2).status_code == 400  # sin responsable
    act(ana, task, "complete", version=2)
    closed = act(ana, task, "assign", version=3, assigned_user_id=str(board.vera.pk))
    assert fail(closed) == (409, "TASK_CLOSED", [])


def test_each_action_needs_its_permissions_before_reading_anything(board: SimpleNamespace) -> None:
    task, other = board.tasks["vera"], make_user()
    join(board.a, other)
    bea, nobody, viewless = signed(board.bea), signed(other), make_user()
    give(board.a, board.m["bea"], {"tasks.view": "ORGANIZATION"})
    give(
        board.a, join(board.a, viewless).pk, {"tasks.update": "ORGANIZATION", "tasks.assign": None}
    )
    sam, blind = {"assigned_user_id": str(board.sam.pk)}, signed(viewless)
    for name, body in (("complete", {}), ("cancel", {}), ("reopen", {}), ("assign", sam)):
        assert reply(act(blind, task, name, **body)) == DENIED, name  # cambiar o repartir sin ver
        assert reply(act(bea, task, name, **body)) == DENIED, name  # ver no es cambiar ni repartir
        assert reply(act(nobody, task, name, **body)) == DENIED, name  # sin rol
        assert reply(act(signed(board.luis), task, name, **body)) == NOT_FOUND, name  # no es de A
    give(board.a, board.m["bea"], {"tasks.update": "ORGANIZATION"})
    assert act(bea, task, "complete").status_code == 200  # ver y cambiar
    assert (
        reply(act(bea, board.tasks["ana"], "assign", **sam)) == DENIED
    )  # repartir es otro permiso
    give(board.a, board.m["bea"], {"tasks.assign": None})
    assert act(bea, board.tasks["ana"], "assign", **sam).status_code == 200


def test_the_scope_decides_each_action_and_every_miss_is_the_same_404(
    board: SimpleNamespace,
) -> None:
    vera, sam = signed(board.vera), signed(board.sam)
    for name in ("complete", "cancel", "reopen"):  # `OWN`: lo suyo; lo demás, un 404 igual
        misses = {
            reply(act(vera, pk, name))
            for pk in (board.tasks["sam"], board.tasks["gone"], board.tasks["b"], uuid4())
        }
        assert misses == {NOT_FOUND}, name
    assert (
        reply(act(vera, board.tasks["sam"], "assign", assigned_user_id=str(board.sam.pk))) == DENIED
    )
    assert act(sam, board.tasks["vera"], "complete").status_code == 200  # `TEAM`: su compañera
    assert act(vera, board.tasks["vera"], "complete", version=2).status_code == 409
    moved = act(sam, board.tasks["sam"], "assign", assigned_user_id=str(board.ana.pk))
    assert moved.status_code == 200 and moved.json()["assigned_user_id"] == str(board.ana.pk)
    assert reply(sam.get(f"{TASKS}{board.tasks['sam']}/")) == NOT_FOUND  # ya no es de su equipo


def test_an_action_decides_the_scope_on_the_locked_row(board: SimpleNamespace) -> None:
    with CaptureQueriesContext(connection) as queries:
        assert act(signed(board.ana), board.tasks["vera"], "complete").status_code == 200
    locks = [q for q in queries if "FOR NO KEY UPDATE" in q["sql"] and "tasks" in q["sql"]]
    assert len(locks) >= 2  # la API, al decidir el alcance, y el comando, al escribir
