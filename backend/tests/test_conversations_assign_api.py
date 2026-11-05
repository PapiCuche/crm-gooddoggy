"""F6-05a: `POST …/conversations/{id}/assign/` (tomar, transferir, soltar). Middleware, sesión,
motor de autorización y PostgreSQL con el rol `crm_app`; el historial, el outbox y la auditoría,
leídos con el rol dueño."""

import json
import threading
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from django.db import connection
from django.test import Client
from django.utils import timezone

from apps.access.selectors import AccessDenied
from apps.accounts.models import User
from apps.conversations import commands
from apps.conversations.models import Conversation
from apps.organizations.models import OrganizationMembership
from core.tenancy.scope import tenant_scope
from tests.factories import TEST_PASSWORD, make_user
from tests.test_authorization import give
from tests.test_conversations_api import BASE, UNAUTHENTICATED, crm, inbox, real_stack
from tests.test_member_branch import DENIED
from tests.test_memberships import ctx, join
from tests.test_self_context import NOT_FOUND, reply, signed
from tests.test_team_writes import send

__all__ = ["crm", "inbox", "real_stack"]  # los fixtures, para pytest
pytestmark = pytest.mark.usefixtures("tenant_db")
DB = psycopg.Connection[Any]


def assign(client: Client, convo: UUID, raw: Any = None, **body: Any) -> Any:
    return send(client, "post", f"{BASE}{convo}/assign/", body if raw is None else raw)


def row(migrator: DB, sql: str, *params: Any) -> list[Any]:
    found: list[Any] = migrator.execute(sql, list(params)).fetchall()
    return found


def history(migrator: DB, convo: UUID) -> list[Any]:
    return row(
        migrator,
        "SELECT reason_code, previous_user_id, new_user_id, new_team_id, assigned_by_user_id, "
        "assigned_by_system FROM conversation_assignments WHERE conversation_id = %s "
        "ORDER BY created_at, id",
        convo,
    )


def current(org: UUID, convo: UUID) -> Conversation:
    with tenant_scope(ctx(org)):
        found: Conversation = Conversation.objects.get(pk=convo)
        return found


def test_the_owner_takes_a_free_conversation_and_everything_is_recorded(
    inbox: SimpleNamespace, migrator: DB
) -> None:
    convo, ana = inbox.convs["nobody"], signed(inbox.ana)
    taken = assign(ana, convo, action="TAKE")
    assert taken.status_code == 200, taken.content
    body = taken.json()
    assert set(body) == {  # sin contacto ni texto: quien transfiere puede dejar de verla
        "id", "number", "status", "assignee_kind", "assigned_user_id", "assigned_team_id",
        "assigned_at", "assignment_version", "version",
    }  # fmt: skip
    assert (body["assignee_kind"], body["assigned_user_id"]) == ("USER", str(inbox.ana.pk))
    assert body["assignment_version"] == 2 and body["status"] == "PENDING"  # D-F6-01: sigue en cola
    assert history(migrator, convo) == [("TAKE", None, inbox.ana.pk, None, inbox.ana.pk, False)]
    audited = row(
        migrator,
        "SELECT changes::text, metadata::text FROM audit_logs "
        "WHERE action = 'conversation.assigned'",
    )
    assert len(audited) == 1 and str(inbox.ana.pk) in audited[0][0] and "TAKE" in audited[0][1]
    event = row(
        migrator,
        "SELECT payload::text FROM outbox_events WHERE event_type = 'conversation.assigned'",
    )
    assert set(json.loads(event[0][0])) == {"previous_user_id", "user_id"}
    mine = {r["id"] for r in ana.get(BASE, {"box": "mine"}).json()["results"]}
    free = {r["id"] for r in ana.get(BASE, {"box": "unassigned"}).json()["results"]}
    assert str(convo) in mine and str(convo) not in free  # `box` ve lo mismo que la asignación


def test_a_taken_conversation_a_stale_version_and_a_closed_one_answer_409(
    inbox: SimpleNamespace, migrator: DB
) -> None:
    convo, ana, sam = inbox.convs["nobody"], signed(inbox.ana), signed(inbox.sam)
    stale = assign(ana, convo, action="TAKE", assignment_version=5)
    assert (stale.status_code, stale.json()["code"]) == (409, "ASSIGNMENT_CONFLICT")
    assert assign(ana, convo, action="TAKE", assignment_version=1).status_code == 200
    assert reply(assign(sam, convo, action="TAKE")) == NOT_FOUND  # ya no la ve: no es suya
    again = assign(ana, convo, action="TAKE")  # quien sí la ve, pero ya la tiene
    assert (again.status_code, again.json()["code"]) == (409, "ASSIGNMENT_CONFLICT")
    assert "Actualiza" in again.json()["message"] and len(history(migrator, convo)) == 1
    free = inbox.convs["team"]  # sin persona, con equipo
    assert assign(ana, free, action="RELEASE").json()["code"] == "ASSIGNMENT_CONFLICT"  # no tiene
    with tenant_scope(ctx(inbox.a)):
        Conversation.objects.filter(pk=free).update(status="CLOSED", closed_at=timezone.now())
    closed = assign(ana, free, action="TAKE")
    assert (closed.status_code, closed.json()["code"]) == (409, "CONVERSATION_CLOSED")


def test_transfer_gives_it_to_a_member_who_can_see_conversations_and_no_one_else(
    inbox: SimpleNamespace, migrator: DB
) -> None:
    convo, ana = inbox.convs["ana"], signed(inbox.ana)
    nobody_can = make_user(email="sinvista@example.com")  # miembro, pero sin `conversations.view`
    give(inbox.a, join(inbox.a, nobody_can).pk, {"messages.send": "OWN"})
    gone = make_user(email="baja@example.com")
    gone_membership = join(inbox.a, gone).pk
    give(inbox.a, gone_membership, {"conversations.view": "OWN"})
    with tenant_scope(ctx(inbox.a)):
        OrganizationMembership.objects.filter(pk=gone_membership).update(status="SUSPENDED")
    wrong = [inbox.bea.pk, inbox.luis.pk, nobody_can.pk, gone.pk, uuid4()]
    answers = {assign(ana, convo, action="TRANSFER", user_id=str(w)).content for w in wrong}
    assert len(answers) == 1  # sin rol, de otra organización, sin permiso, suspendido, inexistente
    bad = assign(ana, convo, action="TRANSFER", user_id=str(inbox.bea.pk))
    assert bad.status_code == 400 and set(bad.json()["fields"]) == {"user_id"}
    assert (
        current(inbox.a, convo).assigned_user_id == inbox.ana.pk and history(migrator, convo) == []
    )
    moved = assign(ana, convo, action="TRANSFER", user_id=str(inbox.vera.pk), assignment_version=1)
    assert moved.status_code == 200 and moved.json()["assigned_user_id"] == str(inbox.vera.pk)
    assert history(migrator, convo) == [
        ("TRANSFER", inbox.ana.pk, inbox.vera.pk, None, inbox.ana.pk, False)
    ]
    same = assign(ana, convo, action="TRANSFER", user_id=str(inbox.vera.pk))
    assert same.json()["code"] == "ASSIGNMENT_CONFLICT"  # ya la tiene
    team = inbox.convs["team"]  # sin persona: asignar es MANUAL y el equipo se conserva
    assert assign(ana, team, action="TRANSFER", user_id=str(inbox.sam.pk)).status_code == 200
    only = history(migrator, team)
    assert only[0][0] == "MANUAL" and only[0][3] is not None  # `new_team_id` sigue siendo el equipo


def test_release_returns_it_to_the_queue_and_keeps_the_team(
    inbox: SimpleNamespace, migrator: DB
) -> None:
    convo, sam = inbox.convs["both"], signed(inbox.sam)  # de vera y del equipo, que sam ve
    with tenant_scope(ctx(inbox.a)):
        Conversation.objects.filter(pk=convo).update(status="OPEN")
    freed = assign(sam, convo, action="RELEASE")
    assert freed.status_code == 200
    assert (freed.json()["assignee_kind"], freed.json()["status"]) == ("TEAM", "PENDING")
    stored = current(inbox.a, convo)
    assert stored.assigned_user_id is None and stored.assigned_team_id is not None
    assert stored.assigned_at is None and stored.assignment_version == 2
    assert history(migrator, convo)[0][:3] == ("MANUAL", inbox.vera.pk, None)
    changes = row(
        migrator, "SELECT changes::text FROM audit_logs WHERE action = 'conversation.assigned'"
    )
    assert "PENDING" in changes[0][0]  # el cambio de estado también queda


def test_the_permissions_decide_before_the_body_and_the_scope_before_the_conversation(
    inbox: SimpleNamespace,
) -> None:
    convo = inbox.convs["nobody"]
    for who in (inbox.bea, inbox.vera):  # sin rol, y un Vendedor: no reparte
        assert reply(assign(signed(who), convo, "basura")) == DENIED
    assert reply(assign(Client(), convo, action="TAKE")) == UNAUTHENTICATED
    assert reply(assign(signed(inbox.luis), convo, action="TAKE")) == NOT_FOUND
    staff = User.objects.create_superuser("staff@example.com", TEST_PASSWORD)
    assert reply(assign(signed(staff), convo, action="TAKE")) == NOT_FOUND
    ana = signed(inbox.ana)
    ana.cookies["csrftoken"] = "t" * 32
    bare = ana.post(f"{BASE}{convo}/assign/", {"action": "TAKE"}, "application/json")
    assert reply(bare) == (403, b'{"code":"CSRF_FAILED"}')
    parts: tuple[dict[str, str | None], ...] = (
        {"conversations.assign": None},
        {"conversations.view": "ORGANIZATION"},
    )
    for index, grants in enumerate(parts):  # cada permiso solo no basta
        user = make_user(email=f"parcial{index}@example.com")
        give(inbox.a, join(inbox.a, user).pk, grants)
        assert reply(assign(signed(user), convo, "basura")) == DENIED
    missing = reply(assign(ana, uuid4(), action="TAKE"))
    assert missing == NOT_FOUND
    assert assign(ana, uuid4(), action="NOPE").status_code == 400  # el cuerpo, antes del 404
    sam = signed(inbox.sam)  # `TEAM`: no ve la de ana
    assert reply(assign(sam, inbox.convs["ana"], action="RELEASE")) == missing
    assert reply(assign(sam, inbox.convs["b"], action="TAKE")) == missing
    wide = make_user(email="ancho@example.com")  # reparte en toda la organización, ve lo suyo
    give(
        inbox.a, join(inbox.a, wide).pk, {"conversations.assign": None, "conversations.view": "OWN"}
    )
    assert reply(assign(signed(wide), inbox.convs["sam"], action="RELEASE")) == missing


BAD_IDS: list[Any] = [None, 5, "x", "", []]
BAD_V: list[Any] = [0, -1, "x", True, 1.5, None, 2**31]
HOSTILE: list[tuple[Any, list[str]]] = [
    ({}, ["action"]),
    ({"action": "take"}, ["action"]),
    *[({"action": v}, ["action"]) for v in (None, 5, "", ["TAKE"], "RELEASE ")],
    ({"action": "TRANSFER"}, ["user_id"]),
    ({"action": "TAKE", "user_id": str(uuid4())}, ["user_id"]),
    ({"action": "RELEASE", "user_id": str(uuid4())}, ["user_id"]),
    *[({"action": "TRANSFER", "user_id": v}, ["user_id"]) for v in BAD_IDS],
    *[({"action": "TAKE", "assignment_version": v}, ["assignment_version"]) for v in BAD_V],
    ({"action": "TAKE", "organization_id": str(uuid4())}, ["organization_id"]),
    ({"action": "TAKE", "assigned_team_id": str(uuid4())}, ["assigned_team_id"]),
    ({"action": "TAKE", "assigned_user_id": str(uuid4())}, ["assigned_user_id"]),
]


@pytest.mark.parametrize(("body", "named"), HOSTILE)
def test_a_hostile_assignment_is_a_400_on_its_own_field_and_changes_nothing(
    inbox: SimpleNamespace, migrator: DB, body: dict[str, Any], named: list[str]
) -> None:
    convo = inbox.convs["nobody"]
    response = assign(signed(inbox.ana), convo, **body)
    assert response.status_code == 400 and response.json()["code"] == "VALIDATION_ERROR"
    assert sorted(response.json()["fields"]) == sorted(named), response.content
    assert history(migrator, convo) == [] and current(inbox.a, convo).assigned_user_id is None


@pytest.mark.parametrize("raw", ["null", "[]", '"x"', "5", "{", "", "true"])
def test_an_assignment_body_that_is_not_an_object_is_a_400(
    inbox: SimpleNamespace, raw: str
) -> None:
    assert assign(signed(inbox.ana), inbox.convs["nobody"], raw).status_code == 400


def duel(
    org: UUID, first: tuple[Any, Callable[[Any], Any]], second: tuple[Any, Callable[[Any], Any]]
) -> list[Any]:
    """Dos personas a la vez: la primera hace su cambio y retiene la transacción; la segunda tiene
    que esperar su bloqueo, y sigue cuando la primera confirma."""
    wrote, release, outcome = threading.Event(), threading.Event(), list[Any]([None, None])

    def run(index: int, who: Any, command: Callable[[Any], Any]) -> None:
        try:
            with tenant_scope(ctx(org, who)) as tenant:
                outcome[index] = command(tenant)
                if index == 0:
                    wrote.set()
                    release.wait(timeout=10)
        except Exception as error:  # noqa: BLE001 — el resultado lo comprueba el test
            outcome[index] = type(error).__name__
        finally:
            wrote.set()
            connection.close()

    one, two = (threading.Thread(target=run, args=(i, *p)) for i, p in enumerate((first, second)))
    one.start()
    assert wrote.wait(timeout=10)
    two.start()
    two.join(timeout=1)
    waited = two.is_alive()
    release.set()
    for thread in (one, two):
        thread.join(timeout=10)
    assert waited, "la segunda no esperó a la primera"
    return outcome


def test_the_commands_check_the_assign_permission_themselves(inbox: SimpleNamespace) -> None:
    with tenant_scope(ctx(inbox.a, inbox.vera)) as tenant, pytest.raises(AccessDenied):
        commands.take_conversation(tenant, conversation_id=inbox.convs["nobody"])  # no reparte


def test_two_people_taking_the_same_conversation_at_once_only_one_gets_it(
    inbox: SimpleNamespace, migrator: DB
) -> None:
    convo = inbox.convs["nobody"]

    def take(tenant: Any) -> Any:
        return commands.take_conversation(tenant, conversation_id=convo)

    won, lost = duel(inbox.a, (inbox.ana, take), (inbox.sam, take))
    assert won.assigned_user_id == inbox.ana.pk and lost == "AssignmentConflict"
    assert current(inbox.a, convo).assigned_user_id == inbox.ana.pk
    assert len(history(migrator, convo)) == 1  # una sola asignación, un solo registro


def test_someone_who_loses_sight_of_a_conversation_at_that_moment_cannot_release_it(
    inbox: SimpleNamespace, migrator: DB
) -> None:
    convo = inbox.convs["nobody"]  # libre: la ve quien solo ve lo suyo y lo libre
    wide = make_user(email="ancho@example.com")
    grants = {"conversations.assign": None, "conversations.view": "OWN"}
    give(inbox.a, join(inbox.a, wide).pk, grants)

    def take(tenant: Any) -> Any:
        return commands.take_conversation(tenant, conversation_id=convo)

    def release(tenant: Any) -> Any:
        return commands.release_conversation(tenant, conversation_id=convo)

    won, lost = duel(inbox.a, (inbox.ana, take), (wide, release))
    assert won.assigned_user_id == inbox.ana.pk and lost == "DoesNotExist"  # ya no es suya de ver
    assert current(inbox.a, convo).assigned_user_id == inbox.ana.pk
    assert len(history(migrator, convo)) == 1


def test_a_transfer_and_a_release_at_once_do_not_overwrite_each_other(
    inbox: SimpleNamespace, migrator: DB
) -> None:
    convo = inbox.convs["both"]  # de vera; sam y ana pueden repartirla

    def moved(tenant: Any) -> Any:
        return commands.transfer_conversation(
            tenant, conversation_id=convo, user_id=inbox.sam.pk, expected_version=1
        )

    def freed(tenant: Any) -> Any:
        return commands.release_conversation(tenant, conversation_id=convo, expected_version=1)

    first, second = duel(inbox.a, (inbox.ana, moved), (inbox.sam, freed))
    assert first.assigned_user_id == inbox.sam.pk and second == "AssignmentConflict"
    assert len(history(migrator, convo)) == 1
