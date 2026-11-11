"""F5-08b2: `POST …/tasks/`, `PATCH …/tasks/{id}/` y `DELETE …/tasks/{id}/`, con sus permisos, su
alcance, la versión, los vínculos y los datos hostiles. Middleware, sesión, motor de autorización
y PostgreSQL con el rol `crm_app`."""

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from apps.tasks import commands
from apps.tasks.models import Task
from core.tenancy.scope import tenant_scope
from tests.factories import make_user
from tests.test_authorization import give
from tests.test_member_branch import DENIED
from tests.test_memberships import ctx, join
from tests.test_opportunities_tables import make
from tests.test_self_context import NOT_FOUND, reply, signed
from tests.test_tasks_read_api import (
    FIELDS,
    TASKS,
    UNAUTHENTICATED,
    board,
    crm,
    pipelines,
    real_stack,
)
from tests.test_team_writes import send

__all__ = ["board", "crm", "pipelines", "real_stack"]  # las fixtures, para pytest
pytestmark = pytest.mark.usefixtures("tenant_db")
DB = psycopg.Connection[Any]
DUE = "2026-11-20T09:00:00-05:00"
ROWS = "SELECT (SELECT count(*) FROM tasks), (SELECT count(*) FROM audit_logs)"
NOT_OURS = {  # nada de esto se toma del cliente
    "organization_id": str(uuid4()), "assigned_user_id": str(uuid4()), "status": "DONE",
    "source": "AI", "completed_at": DUE, "created_by_user_id": str(uuid4()), "version": 3,
}  # fmt: skip


def create(client: Client, body: Any) -> Any:
    return send(client, "post", TASKS, body)


def patch(client: Client, task: Any, body: Any) -> Any:
    return send(client, "patch", f"{TASKS}{task}/", body)


def fail(response: Any) -> tuple[int, str | None, list[str]]:
    sent = response.json()
    return response.status_code, sent.get("code"), sorted(sent.get("fields", {}))


def audit(migrator: DB) -> list[tuple[str, Any, str]]:
    found = migrator.execute(
        "SELECT action, actor_id, changes::text FROM audit_logs WHERE entity_type = 'task'"
        " ORDER BY occurred_at, id"
    ).fetchall()
    return [(action, actor, changes) for action, actor, changes in found]


def test_the_owner_creates_a_task_and_gets_it_back_like_the_detail(
    board: SimpleNamespace, migrator: DB
) -> None:
    opp = make(board.a, contact_id=board.ids["plain"])
    made = create(
        signed(board.ana),
        {
            "title": "  Llamar   a Juan DNI 12345678 ", "description": "Línea  1\n\nLínea 2",
            "type": "CALL", "priority": "HIGH", "due_at": DUE, "is_all_day": True,
            "reminder_at": "2026-11-19T09:00:00Z", "opportunity_id": str(opp.pk),
        },
    )  # fmt: skip
    assert made.status_code == 201, made.content
    body = made.json()
    assert set(body) == FIELDS
    assert (body["title"], body["description"], body["type"], body["priority"]) == (
        "Llamar a Juan DNI 12345678", "Línea 1\n\nLínea 2", "CALL", "HIGH",
    )  # fmt: skip
    assert (body["status"], body["source"], body["version"], body["is_all_day"]) == (
        "PENDING", "MANUAL", 1, True,
    )  # fmt: skip
    assert body["assigned_user_id"] == str(board.ana.pk)  # quien crea
    assert body["opportunity_id"] == str(opp.pk) and body["contact"]["id"] == str(
        board.ids["plain"]
    )
    assert body["due_at"].startswith("2026-11-20T14:00:00")  # en UTC
    [(action, actor, changes)] = audit(migrator)
    assert (action, actor) == ("task.created", board.ana.pk) and "12345678" not in changes
    assert signed(board.ana).get(f"{TASKS}{body['id']}/").json() == body  # y el detalle lo confirma


def test_a_creator_needs_to_see_the_links_and_every_failure_looks_the_same(
    board: SimpleNamespace, migrator: DB
) -> None:
    vera, before = signed(board.vera), migrator.execute(ROWS).fetchone()
    with tenant_scope(ctx(board.a)):
        other = make(board.a, contact_id=board.ids["plain"])  # de un contacto que vera no ve
        mine = make(board.a, contact_id=board.ids["owned"], assigned_user_id=board.vera.pk)
        gone = make(board.a, contact_id=board.ids["owned"], deleted_at=datetime.now(UTC))
    bad_contacts = [board.ids["plain"], uuid4(), board.ids["b"], board.ids["sam"]]
    answers = {create(vera, {"title": "x", "contact_id": str(c)}).content for c in bad_contacts}
    assert len(answers) == 1  # el mismo 400 con el mismo cuerpo: no sirve para sondear
    assert fail(create(vera, {"title": "x", "contact_id": str(bad_contacts[0])})) == (
        400, "VALIDATION_ERROR", ["contact_id"],
    )  # fmt: skip
    for opp in (other.pk, uuid4(), gone.pk, make(board.b).pk):  # fuera de alcance, ajena o borrada
        assert fail(create(vera, {"title": "x", "opportunity_id": str(opp)}))[2] == [
            "opportunity_id"
        ]
    both = {"title": "x", "opportunity_id": str(mine.pk), "contact_id": str(board.ids["plain"])}
    assert fail(create(signed(board.ana), both))[2] == ["opportunity_id"]  # de otro contacto
    assert migrator.execute(ROWS).fetchone() == before
    ok = create(
        vera, {"title": "x", "contact_id": str(board.ids["owned"]), "opportunity_id": str(mine.pk)}
    )
    assert ok.status_code == 201 and ok.json()["contact"]["display_name"] == "De vera"


def test_creating_needs_its_permission_before_reading_anything(
    board: SimpleNamespace, migrator: DB
) -> None:
    body = {"title": "x"}
    assert reply(create(signed(board.bea), body)) == DENIED  # sin rol
    assert reply(create(Client(), body)) == UNAUTHENTICATED
    assert reply(create(signed(board.luis), body)) == NOT_FOUND  # no es miembro de A
    give(board.a, board.m["bea"], {"tasks.view": "OWN", "tasks.update": "OWN"})
    assert reply(create(signed(board.bea), body)) == DENIED  # ver y cambiar no es crear
    give(board.a, board.m["bea"], {"tasks.create": None})
    assert create(signed(board.bea), body).status_code == 201  # y puede crear sin poder ver
    assert migrator.execute("SELECT count(*) FROM tasks WHERE title = 'x'").fetchone() == (1,)


def test_the_client_never_chooses_the_owner_the_status_or_the_organization(
    board: SimpleNamespace,
) -> None:
    for key, value in NOT_OURS.items():
        assert fail(create(signed(board.ana), {"title": "x", key: value}))[0] == 400, key
    assert fail(create(signed(board.ana), {"title": "x", "contact_id": 5}))[0] == 400


@pytest.mark.parametrize(
    "bad",
    [
        {"title": ""}, {"title": "  \n "}, {"title": 5}, {"title": "x" * 201}, {"title": None},
        {"description": 5}, {"description": "x" * 5001}, {"description": "con\ttabulador"},
        {"type": "NOPE"}, {"priority": "ALTA"}, {"is_all_day": "no"}, {"due_at": "2026-11-20"},
        {"due_at": "2026-11-20T09:00:00"}, {"reminder_at": "3000-01-01T00:00:00Z"},
        {"due_at": 5}, {"opportunity_id": "x"},
    ],
)  # fmt: skip
def test_a_hostile_create_is_a_400_on_its_field_and_writes_nothing(
    board: SimpleNamespace, migrator: DB, bad: dict[str, Any]
) -> None:
    before = migrator.execute(ROWS).fetchone()
    status, code, fields = fail(create(signed(board.ana), {"title": "Llamar", **bad}))
    assert (status, code) == (400, "VALIDATION_ERROR") and fields == [next(iter(bad))]
    assert migrator.execute(ROWS).fetchone() == before


@pytest.mark.parametrize("raw", ['"texto"', "[]", "5", "null", "{"])
def test_a_body_that_is_not_an_object_is_a_400(board: SimpleNamespace, raw: str) -> None:
    client = signed(board.ana)
    client.cookies["csrftoken"] = "t" * 32
    answer = client.post(TASKS, raw, "application/json", headers={"X-CSRFToken": "t" * 32})
    assert answer.status_code == 400


def test_patch_changes_what_is_sent_with_the_version_and_audits_it(
    board: SimpleNamespace, migrator: DB
) -> None:
    task = board.tasks["vera"]
    ana = signed(board.ana)
    sent = {"version": 1, "title": " Nuevo  título ", "priority": "URGENT", "status": "IN_PROGRESS",
            "due_at": DUE, "is_all_day": True}  # fmt: skip
    done = patch(ana, task, sent)
    assert done.status_code == 200, done.content
    body = done.json()
    assert (body["title"], body["priority"], body["status"], body["version"]) == (
        "Nuevo título", "URGENT", "IN_PROGRESS", 2,
    )  # fmt: skip
    assert body["assigned_user_id"] == str(board.vera.pk)  # no cambia el responsable
    [(action, actor, changes)] = audit(migrator)
    assert (action, actor) == ("task.updated", board.ana.pk) and "Nuevo" not in changes
    again = patch(ana, task, {"version": 2, "priority": "URGENT"})  # sin cambios: nada
    assert again.status_code == 200 and again.json()["version"] == 2 and len(audit(migrator)) == 1
    assert fail(patch(ana, task, {"version": 1, "title": "x"})) == (409, "STALE_VERSION", [])
    assert (
        patch(ana, task, {"version": 2, "due_at": None, "reminder_at": None}).json()["due_at"]
        is None
    )
    wrong: list[dict[str, Any]] = [
        {"version": "2"}, {"version": 0}, {"status": "DONE"}, {"title": ""},
        {"assigned_user_id": str(uuid4())}, {"contact_id": str(uuid4())}, {"description": "a\tb"},
    ]  # fmt: skip
    assert patch(ana, task, {"title": "x"}).status_code == 400  # sin versión
    for bad in wrong:
        assert patch(ana, task, {"version": 2, **bad}).status_code == 400, bad


def test_a_closed_task_is_a_409_and_a_missing_or_out_of_scope_one_a_404(
    board: SimpleNamespace,
) -> None:
    with tenant_scope(ctx(board.a, board.ana)) as tenant:
        commands.complete_task(tenant, task_id=board.tasks["ana"], version=1)
    ana = signed(board.ana)
    assert fail(patch(ana, board.tasks["ana"], {"version": 2, "title": "x"})) == (
        409, "TASK_CLOSED", [],
    )  # fmt: skip
    vera = signed(board.vera)
    misses = {
        reply(patch(vera, pk, {"version": 1, "title": "x"}))
        for pk in (board.tasks["sam"], board.tasks["gone"], board.tasks["b"], uuid4())
    }
    assert misses == {NOT_FOUND}  # fuera de alcance, borrada, de B o inexistente: lo mismo
    assert (
        patch(signed(board.sam), board.tasks["vera"], {"version": 1, "title": "x"}).status_code
        == 200
    )
    assert reply(patch(signed(board.bea), board.tasks["vera"], {"version": 1})) == DENIED


def test_updating_needs_view_and_update_together(board: SimpleNamespace) -> None:
    task, viewer = board.tasks["vera"], make_user()
    give(board.a, board.m["bea"], {"tasks.update": "ORGANIZATION"})
    give(board.a, join(board.a, viewer).pk, {"tasks.view": "ORGANIZATION"})
    for who in (signed(board.bea), signed(viewer)):  # cambiar sin ver, o ver sin cambiar
        assert reply(patch(who, task, {"version": 1, "title": "x"})) == DENIED
        assert reply(send(who, "delete", f"{TASKS}{task}/", {})) == DENIED
    give(board.a, board.m["bea"], {"tasks.view": "ORGANIZATION"})
    assert patch(signed(board.bea), task, {"version": 1, "title": "x"}).status_code == 200


def test_the_scope_is_decided_on_the_locked_row(board: SimpleNamespace) -> None:
    with CaptureQueriesContext(connection) as queries:
        assert (
            patch(signed(board.ana), board.tasks["vera"], {"version": 1, "title": "x"}).status_code
            == 200
        )
    locks = [q for q in queries if "FOR NO KEY UPDATE" in q["sql"] and "tasks" in q["sql"]]
    assert len(locks) >= 2  # la API, al decidir el alcance, y el comando, al escribir


def test_delete_is_logical_audited_without_a_version_and_not_idempotent(
    board: SimpleNamespace, migrator: DB
) -> None:
    ana, task = signed(board.ana), board.tasks["vera"]
    done = send(ana, "delete", f"{TASKS}{task}/", {})
    assert done.status_code == 204 and done.content == b""
    assert migrator.execute(
        "SELECT deleted_at IS NOT NULL FROM tasks WHERE id = %s", [task]
    ).fetchone() == (True,)
    assert [a for a, _, _ in audit(migrator)] == ["task.deleted"]
    assert reply(ana.get(f"{TASKS}{task}/")) == NOT_FOUND
    assert reply(send(ana, "delete", f"{TASKS}{task}/", {})) == NOT_FOUND  # repetirlo: 404
    vera = signed(board.vera)
    for other in (board.tasks["sam"], board.tasks["b"], uuid4()):  # ajena: el mismo 404
        assert reply(send(vera, "delete", f"{TASKS}{other}/", {})) == NOT_FOUND
    assert (
        send(vera, "delete", f"{TASKS}{board.tasks['vera']}/", {}).status_code == 404
    )  # ya borrada
    with tenant_scope(ctx(board.a)):
        assert Task.objects.filter(pk=board.tasks["sam"]).exists()
