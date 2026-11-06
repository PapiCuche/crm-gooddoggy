"""F6-05a2: `POST …/conversations/{id}/notes/`, la nota interna. Middleware, sesión, motor de
autorización y PostgreSQL con el rol `crm_app`; el outbox y la auditoría, con el rol dueño."""

from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from django.test import Client
from django.utils import timezone

from apps.access.selectors import AccessDenied
from apps.conversations import commands
from apps.conversations.models import Conversation, Message
from core.tenancy.scope import tenant_scope
from tests.factories import make_user
from tests.test_authorization import give
from tests.test_conversations_api import BASE, UNAUTHENTICATED, crm, inbox, real_stack
from tests.test_conversations_assign_api import current
from tests.test_member_branch import DENIED
from tests.test_memberships import ctx, join
from tests.test_self_context import NOT_FOUND, reply, signed
from tests.test_team_writes import send

__all__ = ["crm", "inbox", "real_stack"]  # los fixtures, para pytest
pytestmark = pytest.mark.usefixtures("tenant_db")
DB = psycopg.Connection[Any]


def note(client: Client, convo: UUID, raw: Any = None, **over: Any) -> Any:
    sent = {"client_message_id": str(uuid4()), "body": "Ojo con el precio"} | over
    return send(client, "post", f"{BASE}{convo}/notes/", sent if raw is None else raw)


def row(migrator: DB, sql: str, *params: Any) -> list[Any]:
    found: list[Any] = migrator.execute(sql, list(params)).fetchall()
    return found


def test_a_note_is_internal_never_sent_and_idempotent(inbox: SimpleNamespace, migrator: DB) -> None:
    convo, ana, key = inbox.convs["nobody"], signed(inbox.ana), str(uuid4())
    made = note(ana, convo, client_message_id=key)
    assert made.status_code == 201, made.content
    saved = made.json()
    assert (saved["direction"], saved["message_type"], saved["delivery_status"]) == (
        "INTERNAL", "NOTE", None,
    )  # fmt: skip
    assert saved["body"] == "Ojo con el precio" and saved["sender_user_id"] == str(inbox.ana.pk)
    again = note(ana, convo, client_message_id=key)
    assert again.status_code == 200 and again.json() == saved
    for client, text in ((ana, "Otro texto"), (signed(inbox.sam), "Ojo con el precio")):
        taken = note(client, convo, client_message_id=key, body=text)
        assert (taken.status_code, taken.json()["code"]) == (409, "CLIENT_MESSAGE_ID_TAKEN")
    assert row(
        migrator, "SELECT count(*) FROM outbox_events WHERE event_type = 'message.queued'"
    ) == [(0,)]
    audited = row(
        migrator,
        "SELECT changes::text || metadata::text FROM audit_logs WHERE action = 'message.noted'",
    )
    assert len(audited) == 1 and "Ojo con el precio" not in audited[0][0]  # ids y longitud
    with tenant_scope(ctx(inbox.a)):
        stored = Message.objects.get(pk=saved["id"])
    assert run_dispatch(inbox, stored) is None  # el despachador ignora lo que no es saliente
    assert current(inbox.a, convo).last_message_preview is None  # y la lista no la enseña
    listed = {r["id"]: r for r in ana.get(BASE).json()["results"]}[str(convo)]
    assert listed["last_message_preview"] is None


def run_dispatch(inbox: SimpleNamespace, message: Message) -> Any:
    with tenant_scope(ctx(inbox.a, inbox.ana)) as tenant:
        return commands.dispatch_outbound(tenant, message_id=message.pk)


def test_the_note_scope_and_the_view_scope_both_have_to_reach_the_conversation(
    inbox: SimpleNamespace,
) -> None:
    theirs, missing = inbox.convs["sam"], reply(note(signed(inbox.vera), uuid4()))
    sees_little = make_user(
        email="ve-poco@example.com"
    )  # anota en toda la organización, ve lo suyo
    give(
        inbox.a,
        join(inbox.a, sees_little).pk,
        {"messages.note": "ORGANIZATION", "conversations.view": "OWN"},
    )
    assert reply(note(signed(sees_little), theirs)) == missing
    writes_little = make_user(
        email="anota-poco@example.com"
    )  # ve toda la organización, anota lo suyo
    give(
        inbox.a,
        join(inbox.a, writes_little).pk,
        {"messages.note": "OWN", "conversations.view": "ORGANIZATION"},
    )
    client = signed(writes_little)
    assert client.get(f"{BASE}{theirs}/").status_code == 200  # la lee
    assert reply(note(client, theirs)) == missing  # pero no es suya para anotar
    sender = make_user(email="solo-envia@example.com")
    give(inbox.a, join(inbox.a, sender).pk, {"messages.send": "ORGANIZATION"})
    with tenant_scope(ctx(inbox.a, sender)) as tenant, pytest.raises(AccessDenied):
        commands.add_note(tenant, conversation_id=theirs, body="x")  # el comando exige el permiso


def test_who_may_write_a_note_and_where(inbox: SimpleNamespace, migrator: DB) -> None:
    vera, convo = signed(inbox.vera), inbox.convs["vera"]
    missing = reply(note(vera, uuid4()))
    assert missing == NOT_FOUND
    for key in ("sam", "ana", "team", "b"):  # ajenas o de B: la misma respuesta
        assert reply(note(vera, inbox.convs[key])) == missing, key
    mine = note(vera, convo)
    assert mine.status_code == 201 and mine.json()["body"] is None  # no ve a ese cliente (D-F6-10)
    assert reply(note(signed(inbox.bea), convo, "basura")) == DENIED
    assert reply(note(Client(), convo)) == UNAUTHENTICATED
    assert reply(note(signed(inbox.luis), convo)) == NOT_FOUND
    assert note(signed(inbox.ana), uuid4(), {"body": ""}).status_code == 400  # el cuerpo, antes
    only_view = make_user(email="lee@example.com")
    give(inbox.a, join(inbox.a, only_view).pk, {"conversations.view": "ORGANIZATION"})
    assert reply(note(signed(only_view), convo, "basura")) == DENIED  # ver no es anotar
    only_note = make_user(email="anota@example.com")
    give(inbox.a, join(inbox.a, only_note).pk, {"messages.note": "ORGANIZATION"})
    assert reply(note(signed(only_note), convo, "basura")) == DENIED  # ni anotar es poder ver
    with tenant_scope(ctx(inbox.a)):
        Conversation.objects.filter(pk=inbox.convs["nobody"]).update(
            status="CLOSED", closed_at=timezone.now()
        )
    assert note(signed(inbox.ana), inbox.convs["nobody"]).status_code == 201  # cerrada, vale
    assert row(migrator, "SELECT count(*) FROM messages WHERE direction = 'INTERNAL'") == [(2,)]


def good(**over: Any) -> dict[str, Any]:
    return {"client_message_id": str(uuid4()), "body": "x"} | over


NOTES: list[dict[str, Any]] = [
    {}, {"body": "x"}, {"client_message_id": None}, good(client_message_id="x"),
    good(body=""), good(body="  "), good(body="x\x00"), good(body="a" * 4097),
    good(direction="OUTBOUND"), good(delivery_status="QUEUED"),
]  # fmt: skip


@pytest.mark.parametrize("body", NOTES)
def test_a_hostile_note_is_a_400_and_writes_nothing(
    inbox: SimpleNamespace, migrator: DB, body: dict[str, Any]
) -> None:
    assert note(signed(inbox.ana), inbox.convs["nobody"], body).status_code == 400
    assert row(migrator, "SELECT count(*) FROM messages") == [(0,)]
