"""F6-04a: `POST …/conversations/{id}/messages/`, el mensaje saliente en cola. Middleware, sesión,
motor de autorización y PostgreSQL con el rol `crm_app`; el outbox y la auditoría, leídos con el
rol dueño."""

import json
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from django.test import Client
from django.utils import timezone

from apps.accounts.models import User
from apps.conversations.models import ChannelAccount, Conversation, Message
from core.tenancy.scope import tenant_scope
from tests import test_conversations_tables as tables
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


def post(client: Client, convo: UUID, raw: Any = None, base: str = BASE, **over: Any) -> Any:
    sent = {"client_message_id": str(uuid4()), "body": "Hola"} | over if raw is None else raw
    return send(client, "post", f"{base}{convo}/messages/", sent)


def counts(migrator: DB) -> tuple[int, int, int]:
    """(mensajes, eventos del outbox, auditorías de `message.queued`)."""

    def count(table: str, where: str = "") -> int:
        row = migrator.execute(f"SELECT count(*) FROM {table} {where}").fetchone()  # noqa: S608
        assert row is not None
        return int(row[0])

    return (
        count("messages"),
        count("outbox_events", "WHERE event_type = 'message.queued'"),
        count("audit_logs", "WHERE action = 'message.queued'"),
    )


def account_of(org: UUID, convo: UUID) -> ChannelAccount:
    with tenant_scope(ctx(org)):
        found: ChannelAccount = ChannelAccount.objects.get(
            pk=Conversation.objects.get(pk=convo).channel_account_id
        )
        return found


def set_state(org: UUID, convo: UUID, **fields: Any) -> None:
    with tenant_scope(ctx(org)):
        if status := fields.pop("account", None):
            ChannelAccount.objects.filter(pk=account_of(org, convo).pk).update(status=status)
        if fields:
            Conversation.objects.filter(pk=convo).update(**fields)


def test_the_owner_queues_a_message_once_and_a_replay_is_the_same_message(
    inbox: SimpleNamespace, migrator: DB
) -> None:
    convo, key = inbox.convs["nobody"], str(uuid4())
    ana = signed(inbox.ana)
    first = post(ana, convo, client_message_id=key, body="Hola, ¿en qué te ayudo?")
    assert first.status_code == 202, first.content
    sent = first.json()
    assert (sent["direction"], sent["delivery_status"], sent["body"]) == (
        "OUTBOUND", "QUEUED", "Hola, ¿en qué te ayudo?",
    )  # fmt: skip
    assert sent["client_message_id"] == key and sent["sender_user_id"] == str(inbox.ana.pk)
    again = post(ana, convo, client_message_id=key, body="Hola, ¿en qué te ayudo?")
    assert again.status_code == 200 and again.json() == sent  # el mismo, no otro
    assert counts(migrator) == (1, 1, 1)  # un mensaje, un evento, una auditoría
    event = migrator.execute(
        "SELECT aggregate_type, aggregate_id, payload::text FROM outbox_events "
        "WHERE event_type = 'message.queued'"
    ).fetchone()
    assert event is not None and event[:2] == ("message", UUID(sent["id"]))
    assert set(json.loads(event[2])) == {"conversation_id", "channel_account_id"}
    assert "Hola" not in event[2]  # el evento lleva ids, nunca el texto
    audited = migrator.execute(
        "SELECT changes::text || metadata::text FROM audit_logs WHERE action = 'message.queued'"
    ).fetchone()
    assert audited is not None and "Hola" not in audited[0]
    row = {r["id"]: r for r in ana.get(BASE).json()["results"]}[str(convo)]
    assert (row["status"], row["last_message_preview"], row["last_message_direction"]) == (
        "OPEN",
        "Hola, ¿en qué te ayudo?",
        "OUTBOUND",
    )  # sale de la cola y la lista lo enseña
    listed = ana.get(f"{BASE}{convo}/messages/").json()["results"]
    assert [m["id"] for m in listed] == [sent["id"]]


def test_the_same_id_never_means_another_message(inbox: SimpleNamespace, migrator: DB) -> None:
    convo, other, key = inbox.convs["nobody"], inbox.convs["ana"], str(uuid4())
    ana, sam = signed(inbox.ana), signed(inbox.sam)
    assert post(ana, convo, client_message_id=key).status_code == 202
    for client, where, text in (
        (ana, convo, "Otro texto"),  # otro texto
        (ana, other, "Hola"),  # otra conversación
        (sam, convo, "Hola"),  # otra persona, el mismo texto: no se le devuelve lo ajeno
    ):
        taken = post(client, where, client_message_id=key, body=text)
        assert (taken.status_code, taken.json()["code"]) == (409, "CLIENT_MESSAGE_ID_TAKEN")
        assert "Hola" not in taken.json().get("message", "")
    assert counts(migrator) == (1, 1, 1)


def test_the_permissions_decide_before_the_body_and_the_scope_before_the_conversation(
    inbox: SimpleNamespace,
) -> None:
    convo = inbox.convs["vera"]
    assert reply(post(signed(inbox.bea), convo, "basura")) == DENIED  # sin rol: no se lee
    assert reply(post(Client(), convo)) == UNAUTHENTICATED
    assert reply(post(signed(inbox.luis), convo)) == NOT_FOUND  # de B, en la ruta de A
    staff = User.objects.create_superuser("staff@example.com", TEST_PASSWORD)
    assert reply(post(signed(staff), convo)) == NOT_FOUND
    ana = signed(inbox.ana)
    ana.cookies["csrftoken"] = "t" * 32
    no_token = ana.post(f"{BASE}{convo}/messages/", {"body": "x"}, "application/json")
    assert reply(no_token) == (403, b'{"code":"CSRF_FAILED"}')
    narrow: tuple[dict[str, str | None], ...] = (
        {"messages.send": "ORGANIZATION"},
        {"conversations.view": "ORGANIZATION"},
    )
    for index, grants in enumerate(narrow):  # cada permiso solo no basta
        user = make_user(email=f"parcial{index}@example.com")
        give(inbox.a, join(inbox.a, user).pk, grants)
        assert reply(post(signed(user), convo)) == DENIED
        assert reply(post(signed(user), convo, "basura")) == DENIED  # el permiso, antes del cuerpo
    missing = reply(post(ana, uuid4()))
    assert missing == NOT_FOUND
    assert post(ana, uuid4(), {"body": ""}).status_code == 400  # el cuerpo antes del 404


def test_a_seller_sends_only_in_the_conversations_the_scope_reaches(
    inbox: SimpleNamespace, migrator: DB
) -> None:
    vera = signed(inbox.vera)
    missing = reply(post(vera, uuid4()))
    assert missing == NOT_FOUND
    for key in ("sam", "ana", "team", "b"):  # ajenas, del equipo (a ella no) o de B: la misma
        assert reply(post(vera, inbox.convs[key])) == missing, key
    assert counts(migrator) == (0, 0, 0)
    mine = post(vera, inbox.convs["vera"], body="Con gusto")
    assert mine.status_code == 202 and mine.json()["body"] is None  # no ve a ese cliente
    assert mine.json()["delivery_status"] == "QUEUED" and mine.json()["sender_user_id"]
    give(inbox.a, inbox.m["vera"], {"contacts.view": "ORGANIZATION"})
    key = mine.json()["client_message_id"]
    shown = post(vera, inbox.convs["vera"], client_message_id=key, body="Con gusto")
    assert shown.status_code == 200 and shown.json()["body"] == "Con gusto"  # D-F6-10
    assert counts(migrator) == (1, 1, 1)
    assert post(signed(inbox.sam), inbox.convs["both"]).status_code == 202  # del equipo, `TEAM`
    wide = make_user(email="ancho@example.com")  # envía en toda la organización, ve solo lo suyo
    give(
        inbox.a,
        join(inbox.a, wide).pk,
        {"messages.send": "ORGANIZATION", "conversations.view": "OWN"},
    )
    assert reply(post(signed(wide), inbox.convs["sam"])) == missing  # lo que no ve, no existe


@pytest.mark.parametrize("blocked", [{"status": "CLOSED"}, {"account": "DISCONNECTED"},
                                     {"account": "ERROR"}])  # fmt: skip
def test_a_closed_conversation_or_an_unconnected_channel_blocks_the_send_with_a_clear_409(
    inbox: SimpleNamespace, migrator: DB, blocked: dict[str, str]
) -> None:
    convo = inbox.convs["nobody"]
    fields: dict[str, Any] = dict(blocked)
    if blocked.get("status") == "CLOSED":
        fields["closed_at"] = timezone.now()
    ana, key = signed(inbox.ana), str(uuid4())
    sent = post(ana, convo, client_message_id=key)
    assert sent.status_code == 202
    set_state(inbox.a, convo, **fields)
    refused = post(ana, convo)
    code = "CONVERSATION_CLOSED" if "status" in blocked else "CHANNEL_UNAVAILABLE"
    assert (refused.status_code, refused.json()["code"]) == (409, code)
    assert "cerrada" in refused.json()["message"] or "canal" in refused.json()["message"]
    assert counts(migrator) == (1, 1, 1)  # nada se escribió
    assert post(ana, convo, client_message_id=key).status_code == 200  # lo ya guardado se devuelve


def test_a_pending_channel_blocks_the_send_with_a_clear_409(
    inbox: SimpleNamespace, migrator: DB
) -> None:
    """`PENDING` ya no se alcanza desde una cuenta conectada (F7-02a): la cuenta nace así."""
    convo = tables.fresh(inbox.a, tables.pending_account(inbox.a), "CONV-09901")
    refused = post(signed(inbox.ana), convo.pk)
    assert (refused.status_code, refused.json()["code"]) == (409, "CHANNEL_UNAVAILABLE")
    assert counts(migrator) == (0, 0, 0)  # nada se escribió


def test_a_conversation_the_sender_can_read_but_not_write_is_an_unknown_one(
    inbox: SimpleNamespace, migrator: DB
) -> None:
    """`conversations.view` abarca más que `messages.send` (`OWN`): el comando lo rechaza bajo el
    bloqueo y la ruta lo cuenta como desconocido, no como un 500."""
    reader = make_user(email="lee@example.com")
    grants: dict[str, str | None] = {"conversations.view": "ORGANIZATION", "messages.send": "OWN"}
    give(inbox.a, join(inbox.a, reader).pk, grants)
    client, theirs = signed(reader), inbox.convs["sam"]
    assert client.get(f"{BASE}{theirs}/").status_code == 200  # la ve
    assert reply(post(client, theirs)) == reply(post(client, uuid4())) == NOT_FOUND
    assert counts(migrator) == (0, 0, 0)


def test_a_deleted_channel_account_takes_no_replies(inbox: SimpleNamespace) -> None:
    convo = inbox.convs["nobody"]
    with tenant_scope(ctx(inbox.a)):
        ChannelAccount.objects.filter(pk=account_of(inbox.a, convo).pk).update(
            deleted_at=timezone.now()
        )
    refused = post(signed(inbox.ana), convo)
    assert (refused.status_code, refused.json()["code"]) == (409, "CHANNEL_UNAVAILABLE")


def test_a_resolved_conversation_and_a_degraded_channel_still_take_replies(
    inbox: SimpleNamespace,
) -> None:
    convo = inbox.convs["nobody"]
    set_state(inbox.a, convo, status="RESOLVED", resolved_at=timezone.now(), account="DEGRADED")
    ana = signed(inbox.ana)
    assert post(ana, convo).status_code == 202
    assert ana.get(f"{BASE}{convo}/").json()["status"] == "RESOLVED"  # D-F6-11: sigue resuelta


BAD_IDS: list[Any] = [None, 5, "x", "", []]
HOSTILE: list[tuple[Any, list[str]]] = [
    ({}, ["body", "client_message_id"]),
    ({"organization_id": str(uuid4())}, ["organization_id"]),
    ({"sender_user_id": str(uuid4())}, ["sender_user_id"]),
    ({"delivery_status": "SENT"}, ["delivery_status"]),
    ({"direction": "INBOUND"}, ["direction"]),
    *[({"client_message_id": v}, ["client_message_id"]) for v in BAD_IDS],
    *[({"body": v}, ["body"]) for v in [None, 5, "", "   ", "x\x00y", "x\x07y", "a" * 4097, ["x"]]],
    *[({"body": v}, ["body"]) for v in ["​​", "x‮y"]],
]  # fmt: skip


@pytest.mark.parametrize(("change", "named"), HOSTILE)
def test_a_hostile_send_is_a_400_on_its_own_field_and_writes_nothing(
    inbox: SimpleNamespace, migrator: DB, change: dict[str, Any], named: list[str]
) -> None:
    sent = {"client_message_id": str(uuid4()), "body": "Hola"} | change if change else {}
    response = post(signed(inbox.ana), inbox.convs["nobody"], sent)
    assert response.status_code == 400 and response.json()["code"] == "VALIDATION_ERROR"
    assert sorted(response.json()["fields"]) == sorted(named), response.content
    assert counts(migrator) == (0, 0, 0)


@pytest.mark.parametrize("raw", ["null", "[]", '"x"', "5", "{", "", "true"])
def test_a_send_body_that_is_not_an_object_is_a_400(inbox: SimpleNamespace, raw: str) -> None:
    assert post(signed(inbox.ana), inbox.convs["nobody"], raw).status_code == 400


def test_a_conversation_of_another_organization_is_an_unknown_one(inbox: SimpleNamespace) -> None:
    other = BASE.replace("org-a", "org-b")
    give(
        inbox.b,
        inbox.m_luis,
        {"messages.send": "ORGANIZATION", "conversations.view": "ORGANIZATION"},
    )
    luis = signed(inbox.luis)
    unknown = reply(post(luis, uuid4(), base=other))
    assert unknown == NOT_FOUND
    assert reply(post(luis, inbox.convs["vera"], base=other)) == unknown  # la de A, desde B
    assert post(luis, inbox.convs["b"], base=other).status_code == 202  # la suya, sí
    with tenant_scope(ctx(inbox.a)):
        assert Message.objects.count() == 0
