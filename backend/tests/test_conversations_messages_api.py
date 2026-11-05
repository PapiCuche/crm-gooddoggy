"""F6-02c: `…/conversations/{id}/messages/` y el simulador `…/channels/sandbox/inbound/`.
Middleware, sesión, motor de autorización y PostgreSQL con el rol `crm_app`."""

from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from django.test import Client
from django.utils import timezone

from apps.access.selectors import can, execution_context, scoped
from apps.contacts.models import Contact
from apps.conversations.api.messages import MessageSerializer
from apps.conversations.models import Conversation, Message
from core.tenancy.scope import tenant_scope
from tests.factories import make_user
from tests.test_authorization import give
from tests.test_conversations_api import BASE, UNAUTHENTICATED, crm, inbox, real_stack
from tests.test_member_branch import DENIED
from tests.test_memberships import ctx, join
from tests.test_self_context import NOT_FOUND, reply, signed
from tests.test_team_writes import send

__all__ = ["crm", "inbox", "real_stack"]  # los fixtures, para pytest
pytestmark = pytest.mark.usefixtures("tenant_db")
INBOUND = "/api/v1/o/org-a/channels/sandbox/inbound/"
MESSAGE = {
    "id", "direction", "sender_type", "sender_user_id", "message_type", "body",
    "client_message_id", "delivery_status", "created_at", "sent_at", "delivered_at", "read_at",
    "failed_at",
}  # fmt: skip


def say(org: UUID, pk: UUID, body: str, **over: Any) -> Message:
    with tenant_scope(ctx(org)):
        convo = Conversation.objects.get(pk=pk)
        fields = {
            "conversation_id": pk, "channel_account_id": convo.channel_account_id,
            "direction": "INBOUND", "sender_type": "CONTACT", "body": body,
        } | over  # fmt: skip
        return Message.objects.create(**fields)  # type: ignore[no-any-return]


def test_messages_come_newest_first_in_pages_of_fifty_and_the_same_404_applies(
    inbox: SimpleNamespace,
) -> None:
    convo = inbox.convs["vera"]
    for index in range(52):
        say(inbox.a, convo, f"m{index}")
    ana = signed(inbox.ana)
    page = ana.get(f"{BASE}{convo}/messages/").json()
    assert [r["body"] for r in page["results"]] == [f"m{i}" for i in range(51, 1, -1)]  # 50
    assert page["next"] is not None and set(page["results"][0]) == MESSAGE
    older = ana.get(f"{BASE}{convo}/messages/", {"cursor": page["next"]}).json()
    assert [r["body"] for r in older["results"]] == ["m1", "m0"] and older["next"] is None
    assert ana.get(f"{BASE}{convo}/messages/", {"limit": 0}).status_code == 400
    assert ana.get(f"{BASE}{convo}/messages/", {"cursor": "x"}).status_code == 400
    vera, sam = signed(inbox.vera), signed(inbox.sam)
    missing = reply(vera.get(f"{BASE}{uuid4()}/messages/"))
    assert missing == NOT_FOUND
    for key in ("sam", "ana", "b", "team"):
        assert reply(vera.get(f"{BASE}{inbox.convs[key]}/messages/")) == missing, key
    assert reply(signed(inbox.bea).get(f"{BASE}{convo}/messages/")) == DENIED
    assert reply(Client().get(f"{BASE}{convo}/messages/")) == UNAUTHENTICATED
    assert reply(sam.get(f"{BASE}{convo}/messages/")) == missing  # `TEAM` no alcanza lo de vera


def test_a_message_is_in_scope_exactly_when_its_conversation_is(inbox: SimpleNamespace) -> None:
    for key, pk in inbox.convs.items():
        if key != "b":
            say(inbox.a, pk, key)
    for user in (inbox.ana, inbox.vera, inbox.sam):  # la lista y `can()`, fila a fila
        with tenant_scope(ctx(inbox.a, user)) as tenant:
            ectx, rows = execution_context(tenant), Message.objects.all()
            listed = set(scoped(ectx, "conversations.view", rows).values_list("pk", flat=True))
            assert listed == {m.pk for m in rows if can(ectx, "conversations.view", m)}, user.email
            assert (len(listed) < len(rows)) == (user is not inbox.ana)  # el Owner ve todas


SEES: dict[str, str | None] = {"contacts.view": "ORGANIZATION"}  # sin él el texto no sale (D-F6-10)


def test_the_text_needs_the_contact_to_be_visible_and_the_rest_of_the_message_does_not(
    inbox: SimpleNamespace,
) -> None:
    convo = inbox.convs["vera"]  # de vera, que no ve a ese cliente
    say(inbox.a, convo, "Mi DNI es 12345678")
    say(inbox.a, convo, "NOTA", direction="INTERNAL", message_type="NOTE",
        sender_type="USER", sender_user_id=inbox.ana.pk)  # fmt: skip
    url, vera = f"{BASE}{convo}/messages/", signed(inbox.vera)
    give(inbox.a, inbox.m["vera"], {"messages.note": "OWN"})  # ve las notas, no al cliente
    rows = vera.get(url).json()["results"]
    assert [r["body"] for r in rows] == [None, None] and len(rows) == 2  # nota incluida
    assert {r["direction"] for r in rows} == {"INBOUND", "INTERNAL"} and all(r["id"] for r in rows)
    assert "12345678" not in str(rows) and "NOTA" not in str(rows)
    give(inbox.a, inbox.m["vera"], SEES)
    assert [r["body"] for r in vera.get(url).json()["results"]] == ["NOTA", "Mi DNI es 12345678"]
    with tenant_scope(ctx(inbox.a, inbox.ana)):  # sin el contexto de la vista, cerrado
        sent = Message.objects.filter(body="NOTA").get()
        assert MessageSerializer(sent).data["body"] is None
        assert MessageSerializer(sent, context={"show_body": True}).data["body"] == "NOTA"


def test_notes_reach_only_who_holds_messages_note_over_that_conversation(
    inbox: SimpleNamespace,
) -> None:
    mine, other = inbox.convs["vera"], inbox.convs["sam"]
    for pk in (mine, other):
        say(inbox.a, pk, "visible")
        say(inbox.a, pk, "NOTA", direction="INTERNAL", message_type="NOTE",
            sender_type="USER", sender_user_id=inbox.ana.pk)  # fmt: skip
    viewer = make_user(email="soloveo@example.com")
    give(inbox.a, join(inbox.a, viewer).pk, SEES | {"conversations.view": "ORGANIZATION"})
    assert [r["body"] for r in signed(viewer).get(f"{BASE}{mine}/messages/").json()["results"]] == [
        "visible"
    ]  # `conversations.view` solo: sin notas
    sender = make_user(email="soloenvia@example.com")  # enviar no es poder ver las notas
    give(inbox.a, join(inbox.a, sender).pk, SEES | {"conversations.view": "ORGANIZATION",
                                                    "messages.send": "ORGANIZATION"})  # fmt: skip
    assert len(signed(sender).get(f"{BASE}{mine}/messages/").json()["results"]) == 1
    owner = {r["body"] for r in signed(inbox.ana).get(f"{BASE}{mine}/messages/").json()["results"]}
    assert owner == {"visible", "NOTA"}
    narrow = make_user(email="notaspropias@example.com")  # ve todo, anota solo lo suyo
    give(inbox.a, join(inbox.a, narrow).pk, SEES | {"conversations.view": "ORGANIZATION",
                                                    "messages.note": "OWN"})  # fmt: skip
    with tenant_scope(ctx(inbox.a)):
        Conversation.objects.filter(pk=mine).update(assigned_user_id=narrow.pk)
    seen = {r["body"] for r in signed(narrow).get(f"{BASE}{mine}/messages/").json()["results"]}
    assert seen == {"visible", "NOTA"}  # suya: con notas
    seen = {r["body"] for r in signed(narrow).get(f"{BASE}{other}/messages/").json()["results"]}
    assert seen == {"visible"}  # ajena: sin notas


def test_the_client_message_id_is_shown_only_on_the_viewers_own_outbound_messages(
    inbox: SimpleNamespace,
) -> None:
    convo, key = inbox.convs["nobody"], uuid4()
    own = {"direction": "OUTBOUND", "sender_type": "USER", "delivery_status": "QUEUED"}
    say(inbox.a, convo, "mío", sender_user_id=inbox.ana.pk, client_message_id=key, **own)
    say(inbox.a, convo, "de sam", sender_user_id=inbox.sam.pk, client_message_id=uuid4(), **own)
    say(inbox.a, convo, "del cliente")
    rows = {
        r["body"]: r for r in signed(inbox.ana).get(f"{BASE}{convo}/messages/").json()["results"]
    }
    assert rows["mío"]["client_message_id"] == str(key)
    assert (
        rows["de sam"]["client_message_id"] is None
        and rows["del cliente"]["client_message_id"] is None
    )
    assert (
        rows["mío"]["delivery_status"] == "QUEUED"
        and rows["del cliente"]["delivery_status"] is None
    )


def post(client: Client, body: Any) -> Any:
    return send(client, "post", INBOUND, body)


def simulated(contact: Any, **over: Any) -> dict[str, Any]:
    return {"contact_id": str(contact), "external_message_id": "wamid.sim1", "text": "Hola"} | over


def test_the_owner_simulates_an_inbound_message_and_a_replay_is_the_same_message(
    inbox: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    with tenant_scope(ctx(inbox.a)):
        who = Contact.objects.create(display_name="Cliente nuevo").pk
    ana = signed(inbox.ana)
    before = migrator.execute("SELECT count(*) FROM channel_accounts").fetchone()
    assert before is not None
    first = post(ana, simulated(who))
    assert first.status_code == 201, first.content
    body = first.json()
    assert body["created"] is True and set(body["message"]) == MESSAGE
    assert (body["message"]["direction"], body["message"]["body"]) == ("INBOUND", "Hola")
    again = post(ana, simulated(who))
    assert again.status_code == 200 and again.json() == {**body, "created": False}
    listed = ana.get(BASE).json()["results"]
    mine = next(r for r in listed if r["contact"]["id"] == str(who))
    assert (mine["status"], mine["last_message_preview"]) == ("PENDING", "Hola")
    assert mine["number"].startswith("CONV-")
    assert (
        ana.get(f"{BASE}{body['conversation_id']}/messages/").json()["results"][0]["body"] == "Hola"
    )
    before = migrator.execute("SELECT count(*) FROM channel_accounts").fetchone()
    assert before is not None
    second = post(ana, simulated(who, external_message_id="wamid.sim2", text="Otro"))
    assert second.status_code == 201 and second.json()["conversation_id"] == body["conversation_id"]
    rows = migrator.execute(
        "SELECT action FROM audit_logs WHERE entity_type = 'message'"
    ).fetchall()
    assert [r[0] for r in rows] == ["message.received", "message.received"]  # la repetición, nada
    accounts = migrator.execute("SELECT count(*) FROM channel_accounts WHERE platform = 'SANDBOX'")
    assert accounts.fetchone() == (before[0],)  # la cuenta Sandbox se crea una vez, no por mensaje


def test_an_external_id_cannot_be_reused_for_other_text_or_another_contact(
    inbox: SimpleNamespace,
) -> None:
    with tenant_scope(ctx(inbox.a)):
        one, two = (Contact.objects.create(display_name=n).pk for n in ("uno", "dos"))
    ana = signed(inbox.ana)
    assert post(ana, simulated(one)).status_code == 201
    for other in (simulated(one, text="Distinto"), simulated(two)):
        response = post(ana, other)
        assert (response.status_code, response.json()["code"]) == (409, "EXTERNAL_ID_TAKEN")
        assert "Hola" not in response.text
    with tenant_scope(ctx(inbox.a)):
        assert Message.objects.filter(external_message_id="wamid.sim1").count() == 1


def test_the_contact_must_be_one_the_caller_can_see_with_one_answer_for_all(
    inbox: SimpleNamespace,
) -> None:
    with tenant_scope(ctx(inbox.a)):
        gone = Contact.objects.create(display_name="b", deleted_at=timezone.now()).pk
        theirs = Contact.objects.create(display_name="ajeno", owner_user_id=inbox.ana.pk).pk
    with tenant_scope(ctx(inbox.b)):
        foreign = Contact.objects.create(display_name="de B").pk
    ops = make_user(email="ops@example.com")  # `contacts.view` solo de lo suyo
    give(inbox.a, join(inbox.a, ops).pk, {"channels.manage": None, "contacts.view": "OWN"})
    client = signed(ops)
    answers = {post(client, simulated(c)).content for c in (gone, theirs, foreign, uuid4())}
    assert len(answers) == 1  # desconocido, borrado, de otro responsable y de otra organización
    bad = post(client, simulated(theirs))
    assert bad.status_code == 400 and set(bad.json()["fields"]) == {"contact_id"}
    with tenant_scope(ctx(inbox.a)):
        assert (
            Message.objects.count() == 0
            and Conversation.objects.filter(contact_id=theirs).count() == 0
        )


def test_simulating_needs_channels_manage_and_contacts_view_before_reading_the_body(
    inbox: SimpleNamespace,
) -> None:
    with tenant_scope(ctx(inbox.a)):
        who = Contact.objects.create(display_name="x").pk
    body = simulated(who)
    for user in (
        inbox.vera,
        inbox.sam,
        inbox.bea,
    ):  # el Vendedor y el Supervisor no configuran canales
        assert reply(post(signed(user), body)) == DENIED, user.email
        assert reply(post(signed(user), "basura")) == DENIED  # y el cuerpo no se lee antes
    only_channels = make_user(email="canales@example.com")
    give(inbox.a, join(inbox.a, only_channels).pk, {"channels.manage": None})
    assert reply(post(signed(only_channels), body)) == DENIED  # sin `contacts.view`
    assert reply(post(Client(), body)) == UNAUTHENTICATED
    assert reply(post(signed(inbox.luis), body)) == NOT_FOUND
    assert reply(signed(inbox.ana).post(INBOUND, body, "application/json")) == (
        403, b'{"code":"CSRF_FAILED"}',
    )  # fmt: skip
    with tenant_scope(ctx(inbox.a)):
        assert Message.objects.count() == 0


HOSTILE = [
    ({}, ["contact_id", "external_message_id", "text"]),
    ({"at": "2026-01-01T00:00:00Z"}, ["at"]),
    ({"organization_id": "x"}, ["organization_id"]),
    *[({"contact_id": v}, ["contact_id"]) for v in (None, 5, "x")],
    *[({"external_message_id": v}, ["external_message_id"]) for v in (None, 5, "", "a\nb")],
    ({"external_message_id": "a" * 201}, ["external_message_id"]),
    *[({"text": v}, ["text"]) for v in (None, 5, "", "   ", "x\x00y", "a" * 4097, ["x"])],
]  # fmt: skip


@pytest.mark.parametrize(("change", "named"), HOSTILE)
def test_a_hostile_simulation_is_a_400_on_its_own_field_and_writes_nothing(
    inbox: SimpleNamespace, change: dict[str, Any], named: list[str]
) -> None:
    with tenant_scope(ctx(inbox.a)):
        who = Contact.objects.create(display_name="x").pk
    sent = {**simulated(who), **change} if change else {}
    response = post(signed(inbox.ana), sent)
    assert response.status_code == 400 and response.json()["code"] == "VALIDATION_ERROR"
    assert sorted(response.json()["fields"]) == sorted(named), response.content
    with tenant_scope(ctx(inbox.a)):
        assert (
            Message.objects.count() == 0
            and Conversation.objects.filter(contact_id=who).count() == 0
        )


@pytest.mark.parametrize("raw", ["null", "[]", '"x"', "5", "{", "", "true"])
def test_a_simulation_body_that_is_not_an_object_is_a_400(inbox: SimpleNamespace, raw: str) -> None:
    assert send(signed(inbox.ana), "post", INBOUND, raw).status_code == 400
