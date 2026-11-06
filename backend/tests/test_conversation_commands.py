"""F6-01c: los comandos del Inbox, con PostgreSQL real y el rol `crm_app`, también con dos
transacciones a la vez. Sin rutas: llegan con F6-02."""

import datetime as dt
import json
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from django.core.exceptions import ObjectDoesNotExist
from django.utils import timezone

from apps.access.selectors import AccessDenied
from apps.contacts.models import Contact
from apps.conversations import commands
from apps.conversations.commands import (
    BadReference,
    ClientMessageConflict,
    StaleVersion,
)
from apps.conversations.models import PREVIEW_MAX, ChannelAccount, Conversation, Message
from apps.conversations.policy import ChannelUnavailable, ConversationClosed
from apps.organizations.models import OrganizationMembership
from core.tenancy.scope import tenant_scope
from tests import test_conversations_tables as tables
from tests.factories import make_user
from tests.test_authorization import give
from tests.test_inventory_stock import race
from tests.test_memberships import ctx, join

world = tables.world
pytestmark = pytest.mark.usefixtures("tenant_db")
type DB = psycopg.Connection[Any]
SECRET = "Mi DNI es 12345678 y mi correo juan@example.com"  # noqa: S105 — texto de prueba


@pytest.fixture(autouse=True)
def writer(world: SimpleNamespace) -> None:
    """`ana` puede responder y anotar en cualquier conversación de A: los comandos lo exigen."""
    with tenant_scope(ctx(world.a)):
        mine = OrganizationMembership.objects.get(user_id=world.ana.pk).pk
    give(world.a, mine, {"messages.send": "ORGANIZATION", "messages.note": "ORGANIZATION"})


def run(
    world: SimpleNamespace,
    command: Callable[..., Any],
    org: str = "a",
    who: Any = None,
    **fields: Any,
) -> Any:
    me = ctx(getattr(world, org), who or (world.ana if org == "a" else world.luis))
    with tenant_scope(me) as tenant:
        return command(tenant, **fields)


def inbound(world: SimpleNamespace, external: str = "wamid.1", **over: Any) -> tuple[Any, bool]:
    wanted = {
        "contact_id": world.contact_a.pk, "channel_account_id": world.account_a.pk,
        "external_message_id": external, "body": SECRET,
    } | over  # fmt: skip
    return run(world, commands.record_inbound, **wanted)  # type: ignore[no-any-return]


def outbound(world: SimpleNamespace, convo: Any, client: UUID | None = None, **over: Any) -> Any:
    wanted = {"conversation_id": convo.pk, "client_message_id": client or uuid4(), "body": "Hola"}
    return run(world, commands.record_outbound, **(wanted | over))


def reload(world: SimpleNamespace, model: Any, pk: UUID) -> Any:
    with tenant_scope(ctx(world.a)):
        return model.objects.get(pk=pk)


def audit(migrator: DB) -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
    found = migrator.execute(
        "SELECT action, changes::text, metadata::text FROM audit_logs "
        "WHERE entity_type IN ('conversation', 'message', 'channel_account') "
        "ORDER BY occurred_at, id"
    ).fetchall()
    return [(a, json.loads(c), json.loads(m)) for a, c, m in found]


def resolve(world: SimpleNamespace, convo: Any, ago: dt.timedelta) -> None:
    with tenant_scope(ctx(world.a)):
        Conversation.objects.filter(pk=convo.pk).update(
            status="RESOLVED", resolved_at=timezone.now() - ago
        )


def test_the_sandbox_account_is_created_once_per_organization(
    world: SimpleNamespace, migrator: DB
) -> None:
    mine = run(world, commands.ensure_sandbox_account)
    assert run(world, commands.ensure_sandbox_account).pk == mine.pk
    other = run(world, commands.ensure_sandbox_account, org="b")
    assert other.pk != mine.pk and mine.external_account_id != other.external_account_id
    assert (mine.platform, mine.status, mine.created_by_user_id) == (
        "SANDBOX",
        "CONNECTED",
        world.ana.pk,
    )
    assert [a for a, *_ in audit(migrator)] == [
        "channel_account.created"
    ] * 2  # una por organización


def test_a_conversation_opens_once_per_contact_and_account_with_its_number(
    world: SimpleNamespace, migrator: DB
) -> None:
    kwargs = {"contact_id": world.contact_a.pk, "channel_account_id": world.account_a.pk}
    convo, created = run(world, commands.open_conversation, **kwargs)
    assert created and (convo.number, convo.status, convo.attention_mode) == (
        "CONV-00001", "PENDING", "HUMAN",
    )  # fmt: skip
    assert reload(world, Conversation, convo.pk).assignee_kind == "UNASSIGNED"
    again, created = run(world, commands.open_conversation, **kwargs)
    assert again.pk == convo.pk and not created
    with tenant_scope(ctx(world.a)):
        second = Contact.objects.create(display_name="Otro")
    other, _ = run(world, commands.open_conversation, **kwargs | {"contact_id": second.pk})
    assert other.number == "CONV-00002"
    assert run(world, commands.open_conversation, org="b", contact_id=world.contact_b.pk,
               channel_account_id=world.account_b.pk)[0].number == "CONV-00001"  # fmt: skip
    created_log = [m for a, _, m in audit(migrator) if a == "conversation.created"]
    assert created_log[0] == {k: str(v) for k, v in kwargs.items()}  # ids, nada de texto


def test_the_references_must_be_live_and_of_the_organization(world: SimpleNamespace) -> None:
    with tenant_scope(ctx(world.a)):
        gone = Contact.objects.create(display_name="Borrado", deleted_at=timezone.now())
        dead = ChannelAccount.objects.create(
            platform="SANDBOX",
            name="Muerta",
            external_account_id="muerta",
            deleted_at=timezone.now(),
        )
    good = {"contact_id": world.contact_a.pk, "channel_account_id": world.account_a.pk}
    cases = {
        "contact_id": [world.contact_b.pk, gone.pk, uuid4()],
        "channel_account_id": [world.account_b.pk, dead.pk, uuid4()],
    }
    for field, bad in cases.items():
        for value in bad:
            with pytest.raises(BadReference) as error:
                run(world, commands.open_conversation, **good | {field: value})
            assert error.value.field == field


def test_two_opens_at_once_leave_one_conversation(world: SimpleNamespace) -> None:
    kwargs = {"contact_id": world.contact_a.pk, "channel_account_id": world.account_a.pk}

    def open_it(tenant: Any) -> Any:
        return commands.open_conversation(tenant, **kwargs)

    first, second = race(world.a, world.ana, [open_it, open_it])  # el segundo espera al primero
    assert first[0].pk == second[0].pk and (first[1], second[1]) == (True, False)
    with tenant_scope(ctx(world.a)):
        assert Conversation.objects.count() == 1


def test_an_inbound_message_opens_the_conversation_and_is_idempotent(
    world: SimpleNamespace, migrator: DB
) -> None:
    message, created = inbound(world)
    assert created and (message.direction, message.sender_type, message.delivery_status) == (
        "INBOUND", "CONTACT", None,
    )  # fmt: skip
    convo = reload(world, Conversation, message.conversation_id)
    assert (convo.status, convo.number) == ("PENDING", "CONV-00001")  # D-INB-1: en cola
    assert convo.last_inbound_at is not None and convo.last_message_at == convo.last_inbound_at
    again, created = inbound(world)  # el webhook repetido
    assert again.pk == message.pk and not created
    assert (
        reload(world, Conversation, convo.pk).last_inbound_at == convo.last_inbound_at
    )  # no cuenta
    with tenant_scope(ctx(world.a)):  # y aunque la conversación se cierre: no abre otra vacía
        Conversation.objects.update(status="CLOSED", closed_at=timezone.now())
    assert inbound(world)[0].pk == message.pk
    with tenant_scope(ctx(world.a)):
        assert Conversation.objects.count() == 1
        Conversation.objects.update(status="PENDING", closed_at=None)
    second, created = inbound(world, "wamid.2", body="Segundo")
    assert created and second.conversation_id == message.conversation_id
    with tenant_scope(ctx(world.a)):
        assert Message.objects.count() == 2 and Conversation.objects.count() == 1
    assert [a for a, *_ in audit(migrator)] == [
        "conversation.created", "message.received", "message.received",
    ]  # fmt: skip


def test_the_same_inbound_message_twice_at_once_is_stored_once(world: SimpleNamespace) -> None:
    def deliver(tenant: Any) -> Any:
        return commands.record_inbound(
            tenant, contact_id=world.contact_a.pk, channel_account_id=world.account_a.pk,
            external_message_id="wamid.race", body="Hola",
        )  # fmt: skip

    first, second = race(world.a, world.ana, [deliver, deliver])
    assert first[0].pk == second[0].pk and (first[1], second[1]) == (True, False)
    with tenant_scope(ctx(world.a)):
        assert Message.objects.count() == 1 and Conversation.objects.count() == 1


def test_an_inbound_message_reopens_per_d_inb_2(world: SimpleNamespace, migrator: DB) -> None:
    first, _ = inbound(world)
    convo = reload(world, Conversation, first.conversation_id)
    with tenant_scope(ctx(world.a)):  # esperando al cliente: contesta y vuelve a OPEN
        Conversation.objects.filter(pk=convo.pk).update(status="WAITING_CUSTOMER")
    inbound(world, "wamid.2")
    woke = reload(world, Conversation, convo.pk)
    assert woke.status == "OPEN" and woke.last_inbound_at > convo.last_inbound_at  # ventana ADR-010
    assert woke.last_message_at == woke.last_inbound_at
    resolve(world, convo, dt.timedelta(hours=71))  # dentro del plazo: la misma, otra vez abierta
    reopened, _ = inbound(world, "wamid.3")
    after = reload(world, Conversation, convo.pk)
    assert reopened.conversation_id == convo.pk and (after.status, after.resolved_at) == (
        "OPEN",
        None,
    )
    resolve(world, convo, dt.timedelta(hours=73))  # pasado el plazo: se cierra y se abre otra
    fresh, _ = inbound(world, "wamid.4")
    assert fresh.conversation_id != convo.pk
    closed, new = (reload(world, Conversation, p) for p in (convo.pk, fresh.conversation_id))
    assert (closed.status, new.status, new.number) == ("CLOSED", "PENDING", "CONV-00002")
    assert closed.closed_at is not None
    with tenant_scope(ctx(world.a)):  # una cerrada no recibe: la siguiente abre otra
        Conversation.objects.filter(pk=new.pk).update(status="CLOSED", closed_at=timezone.now())
    assert inbound(world, "wamid.5")[0].conversation_id not in (convo.pk, new.pk)
    pending, _ = inbound(world, "wamid.6")  # en cola sigue en cola con más mensajes
    assert reload(world, Conversation, pending.conversation_id).status == "PENDING"
    actions = [a for a, *_ in audit(migrator)]
    assert actions.count("conversation.reopened") == 2 and actions.count("conversation.closed") == 1


def test_an_outbound_message_is_queued_idempotently(world: SimpleNamespace, migrator: DB) -> None:
    convo = reload(world, Conversation, inbound(world)[0].conversation_id)
    client = uuid4()
    queued, created = outbound(world, convo, client)
    assert created and (queued.direction, queued.delivery_status) == ("OUTBOUND", "QUEUED")
    assert (queued.sender_type, queued.sender_user_id) == ("USER", world.ana.pk)
    assert reload(world, Conversation, convo.pk).status == "OPEN"  # la primera respuesta humana
    again, created = outbound(world, convo, client)
    assert again.pk == queued.pk and not created
    for clash in ({"body": "Otro texto"}, {"conversation_id": uuid4()}):  # mismo id, otra cosa
        with pytest.raises((ClientMessageConflict, ObjectDoesNotExist)):
            outbound(world, convo, client, **clash)
    with tenant_scope(ctx(world.a)):
        who = Contact.objects.create(display_name="Otro cliente")
    other, _ = inbound(world, "wamid.9", contact_id=who.pk)
    with pytest.raises(ClientMessageConflict):  # el id ya es de un mensaje de otra conversación
        outbound(world, reload(world, Conversation, other.conversation_id), client)
    with tenant_scope(ctx(world.a)):
        assert Message.objects.filter(direction="OUTBOUND").count() == 1
    assert reload(world, Message, queued.pk).body == "Hola"  # el conflicto no sobrescribe
    assert "message.queued" in [a for a, *_ in audit(migrator)]


def test_the_first_reply_stores_the_status_the_version_and_the_thread_clock(
    world: SimpleNamespace,
) -> None:
    """Lo guardado, leído de la base de datos (no el objeto en memoria): pasar de PENDING a OPEN
    persiste también `last_message_at`, la versión y `updated_at`."""
    convo = reload(world, Conversation, inbound(world)[0].conversation_id)
    assert convo.status == "PENDING"
    outbound(world, convo)
    opened = reload(world, Conversation, convo.pk)
    assert (opened.status, opened.version) == ("OPEN", convo.version + 1)
    assert opened.last_message_at > convo.last_message_at and opened.updated_at > convo.updated_at
    outbound(world, opened)  # ya abierta: la marca avanza, sin cambio de estado ni de versión
    again = reload(world, Conversation, convo.pk)
    assert (again.status, again.version) == ("OPEN", opened.version)
    assert again.last_message_at > opened.last_message_at and again.updated_at > opened.updated_at
    run(world, commands.add_note, conversation_id=convo.pk, body="Nota")  # y una nota, nada
    noted = reload(world, Conversation, convo.pk)

    def clock(c: Conversation) -> tuple[Any, ...]:
        return (c.status, c.version, c.last_message_at, c.last_inbound_at, c.updated_at)

    assert clock(noted) == clock(again)


def test_a_closed_conversation_takes_no_reply_but_replays_what_was_stored(
    world: SimpleNamespace,
) -> None:
    convo = reload(world, Conversation, inbound(world)[0].conversation_id)
    client = uuid4()
    queued, _ = outbound(world, convo, client)
    with tenant_scope(ctx(world.a)):
        Conversation.objects.filter(pk=convo.pk).update(status="CLOSED", closed_at=timezone.now())
    with pytest.raises(ConversationClosed):
        outbound(world, convo, uuid4())
    assert outbound(world, convo, client) == (queued, False)  # el reenvío no falla


# `PENDING` ya no se alcanza desde una cuenta conectada: la base lo impide (F7-02a).
@pytest.mark.parametrize("state", ["DISCONNECTED", "ERROR"])
def test_an_unconnected_channel_takes_no_new_reply_but_replays_what_was_stored(
    world: SimpleNamespace, state: str
) -> None:
    convo = reload(world, Conversation, inbound(world)[0].conversation_id)
    client = uuid4()
    queued, _ = outbound(world, convo, client)
    with tenant_scope(ctx(world.a)):
        ChannelAccount.objects.filter(pk=convo.channel_account_id).update(status=state)
    with pytest.raises(ChannelUnavailable):
        outbound(world, convo, uuid4())
    assert outbound(world, convo, client) == (queued, False)


def test_two_replies_with_the_same_client_id_at_once_store_one(world: SimpleNamespace) -> None:
    convo = reload(world, Conversation, inbound(world)[0].conversation_id)
    client = uuid4()

    def reply(tenant: Any) -> Any:
        return commands.record_outbound(
            tenant, conversation_id=convo.pk, client_message_id=client, body="Hola"
        )

    first, second = race(world.a, world.ana, [reply, reply])
    assert first[0].pk == second[0].pk and (first[1], second[1]) == (True, False)


def test_a_note_is_internal_never_sendable_and_leaves_the_thread_clock_alone(
    world: SimpleNamespace, migrator: DB
) -> None:
    convo = reload(world, Conversation, inbound(world)[0].conversation_id)
    note, created = run(world, commands.add_note, conversation_id=convo.pk, body=SECRET)
    assert created and (note.direction, note.message_type, note.delivery_status) == (
        "INTERNAL", "NOTE", None,
    )  # fmt: skip
    assert note.external_message_id is None and note.sender_user_id == world.ana.pk
    after = reload(world, Conversation, convo.pk)
    assert (after.last_message_at, after.last_inbound_at, after.status) == (
        convo.last_message_at, convo.last_inbound_at, "PENDING",
    )  # fmt: skip
    client = uuid4()
    first, _ = run(
        world, commands.add_note, conversation_id=convo.pk, body="Nota", client_message_id=client
    )
    again, created = run(
        world, commands.add_note, conversation_id=convo.pk, body="Nota", client_message_id=client
    )
    assert again.pk == first.pk and not created
    with pytest.raises(ClientMessageConflict):  # ese id es de un mensaje que no es esta nota
        run(
            world,
            commands.add_note,
            conversation_id=convo.pk,
            body="Otra",
            client_message_id=client,
        )
    with pytest.raises(
        ClientMessageConflict
    ):  # ni una respuesta con el id de una nota (mismo texto)
        outbound(world, convo, client, body="Nota")
    with tenant_scope(ctx(world.a)):
        Conversation.objects.filter(pk=convo.pk).update(status="CLOSED", closed_at=timezone.now())
    assert run(world, commands.add_note, conversation_id=convo.pk, body="Tras cerrar")[
        1
    ]  # se admite


def test_the_delivery_status_advances_and_ignores_what_does_not(
    world: SimpleNamespace, migrator: DB
) -> None:
    convo = reload(world, Conversation, inbound(world)[0].conversation_id)
    sent, _ = outbound(world, convo)
    before = len(audit(migrator))

    def advance(status: str, **over: Any) -> Any:
        return run(world, commands.advance_delivery, message_id=sent.pk, status=status, **over)

    moment = timezone.now() - dt.timedelta(minutes=5)
    assert advance("SENT", at=moment).sent_at == moment
    with pytest.raises(ValueError, match="zona"):
        advance("SENT", at=dt.datetime(2026, 1, 1))  # noqa: DTZ001 — sin zona horaria
    read = advance("READ")  # saltar pasos vale, y solo marca la suya
    assert (read.delivery_status, read.read_at is None, read.delivered_at) == ("READ", False, None)
    assert advance("DELIVERED").delivery_status == "READ"  # un estado tardío no retrocede
    assert advance("READ").delivery_status == "READ" and advance("FAILED").delivery_status == "READ"
    assert audit(migrator)[-1][0] == "message.delivery_changed"
    assert len(audit(migrator)) == before + 2  # solo SENT y READ dejaron rastro
    failing, _ = outbound(world, convo)
    run(world, commands.advance_delivery, message_id=failing.pk, status="SENT")
    done = run(world, commands.advance_delivery, message_id=failing.pk, status="FAILED")
    assert (done.delivery_status, done.failed_at is not None) == ("FAILED", True)
    ignored = run(world, commands.advance_delivery, message_id=failing.pk, status="DELIVERED")
    assert ignored.delivery_status == "FAILED"  # terminal


def test_only_outbound_messages_have_a_delivery_status(world: SimpleNamespace) -> None:
    message, _ = inbound(world)
    with pytest.raises(ValueError, match="saliente"):
        run(world, commands.advance_delivery, message_id=message.pk, status="SENT")
    with pytest.raises(ObjectDoesNotExist):
        run(world, commands.advance_delivery, message_id=uuid4(), status="SENT")


def test_nothing_of_the_message_text_reaches_the_audit(
    world: SimpleNamespace, migrator: DB
) -> None:
    plain = "Quiero el iPhone azul para mañana"  # sin nada que la redacción reconozca
    message, _ = inbound(world, body=plain)
    convo = reload(world, Conversation, message.conversation_id)
    queued, _ = outbound(world, convo, body=plain)
    run(world, commands.add_note, conversation_id=convo.pk, body=plain)
    run(world, commands.advance_delivery, message_id=queued.pk, status="DELIVERED")
    rows = audit(migrator)
    assert {a for a, *_ in rows} >= {
        "conversation.created", "message.received", "message.queued", "message.noted",
        "message.delivery_changed",
    }  # fmt: skip
    for word in ("iPhone", "azul", "mañana"):
        assert word not in json.dumps(rows, ensure_ascii=False)
    lengths = [m["length"] for a, _, m in rows if a.startswith("message.") and "length" in m]
    assert lengths == [len(plain)] * 3  # solo la longitud, ids y dirección
    keys = {k for a, _, m in rows if a.startswith("message.") for k in m}
    assert keys <= {"conversation_id", "direction", "length"}


def test_another_organizations_rows_do_not_exist_for_a_command(world: SimpleNamespace) -> None:
    theirs = tables.conversation(world.b, world.contact_b, world.account_b)
    with pytest.raises(ObjectDoesNotExist):
        outbound(world, theirs)
    with pytest.raises(ObjectDoesNotExist):
        run(world, commands.add_note, conversation_id=theirs.pk, body="x")
    with pytest.raises(BadReference):
        inbound(world, contact_id=world.contact_b.pk)


@pytest.mark.parametrize("bad", [None, 5, 1.5, True, "x", [], {}, b"x", "", " "])
def test_hostile_arguments_are_a_value_error_never_a_type_or_integrity_error(
    world: SimpleNamespace, bad: Any
) -> None:
    ok = {"contact_id": world.contact_a.pk, "channel_account_id": world.account_a.pk}
    convo = tables.first(world)
    calls: list[tuple[Callable[..., Any], dict[str, Any]]] = [
        (commands.open_conversation, ok | {"contact_id": bad}),
        (commands.open_conversation, ok | {"channel_account_id": bad}),
        (commands.record_inbound, ok | {"external_message_id": "a", "body": bad}),
        (commands.record_inbound, ok | {"external_message_id": bad, "body": "x"}),
        (
            commands.record_outbound,
            {"conversation_id": convo.pk, "client_message_id": bad, "body": "x"},
        ),
        (
            commands.record_outbound,
            {"conversation_id": bad, "client_message_id": uuid4(), "body": "x"},
        ),
        (commands.add_note, {"conversation_id": convo.pk, "body": bad}),
        (commands.advance_delivery, {"message_id": bad, "status": "SENT"}),
        (commands.advance_delivery, {"message_id": uuid4(), "status": bad}),
    ]
    texty = isinstance(bad, str) and bad.strip() != ""  # un texto vale como cuerpo o id externo
    for command, fields in calls:
        if texty and command in (commands.record_inbound, commands.add_note) and "body" in fields:
            continue
        with pytest.raises(ValueError):  # noqa: PT011
            run(world, command, **fields)


@pytest.mark.parametrize(
    "bad",
    ["a" * 4097, "x\x00y", "\n\n", "a" * 201, " lleva espacios ", "con\nsalto"],
)
def test_texts_and_external_ids_with_limits_are_refused(world: SimpleNamespace, bad: str) -> None:
    ok = {"contact_id": world.contact_a.pk, "channel_account_id": world.account_a.pk}
    convo = tables.first(world)
    refused: list[tuple[Callable[..., Any], dict[str, Any]]] = [
        (commands.record_inbound, ok | {"external_message_id": "a", "body": bad}),
        (commands.add_note, {"conversation_id": convo.pk, "body": bad}),
    ]
    if len(bad) < 4097 and bad.strip() and "\x00" not in bad:  # lo que solo vale como id externo
        refused = [(commands.record_inbound, ok | {"external_message_id": bad, "body": "x"})]
    for command, fields in refused:
        with pytest.raises(ValueError):  # noqa: PT011
            run(world, command, **fields)
    with tenant_scope(ctx(world.a)):
        assert Message.objects.count() == 0


def seller(world: SimpleNamespace, email: str, grants: dict[str, str | None]) -> Any:
    user = make_user(email=email)
    give(world.a, join(world.a, user).pk, grants)
    return user


def test_writing_on_behalf_of_a_person_checks_the_permission_and_the_scope(
    world: SimpleNamespace, migrator: DB
) -> None:
    mine = tables.fresh(world.a, world.account_a, "CONV-00070", assigned_user_id=world.ana.pk)
    free = tables.fresh(world.a, world.account_a, "CONV-00071")
    own: dict[str, str | None] = {"messages.send": "OWN", "messages.note": "OWN"}
    vera = seller(world, "vera@example.com", own)
    mute = seller(world, "mute@example.com", {"organization.view": None})
    before = len(audit(migrator))
    for command, fields in (
        (commands.record_outbound, {"client_message_id": uuid4(), "body": "Hola"}),
        (commands.add_note, {"body": "Nota"}),
    ):
        with pytest.raises(ObjectDoesNotExist) as hidden:  # de ana: fuera del alcance `OWN`
            run(world, command, who=vera, conversation_id=mine.pk, **fields)
        with pytest.raises(ObjectDoesNotExist) as unknown:
            run(world, command, who=vera, conversation_id=uuid4(), **fields)
        assert type(hidden.value) is type(unknown.value) and str(hidden.value) == str(unknown.value)
        for known in (free.pk, uuid4()):  # sin el permiso: igual exista o no (nada que enumerar)
            with pytest.raises(AccessDenied):
                run(world, command, who=mute, conversation_id=known, **fields)
        with pytest.raises(AccessDenied):  # la persona de otra organización no es miembro
            run(world, command, who=world.luis, conversation_id=free.pk, **fields)
    assert len(audit(migrator)) == before  # nada se escribió
    with tenant_scope(ctx(world.a)):
        assert Message.objects.count() == 0
    only_note = seller(world, "nota@example.com", {"messages.note": "ORGANIZATION"})
    only_send = seller(world, "envia@example.com", {"messages.send": "ORGANIZATION"})
    with pytest.raises(AccessDenied):  # cada comando pide su permiso, no el del otro
        run(world, commands.record_outbound, who=only_note, conversation_id=free.pk,
            client_message_id=uuid4(), body="Hola")  # fmt: skip
    with pytest.raises(AccessDenied):
        run(world, commands.add_note, who=only_send, conversation_id=free.pk, body="Nota")
    assert run(world, commands.add_note, who=only_note, conversation_id=free.pk, body="Nota")[1]
    sent = {"conversation_id": free.pk, "client_message_id": uuid4(), "body": "Hola"}
    assert run(world, commands.record_outbound, who=vera, **sent)[1]  # sin asignar: a la vista
    assert run(world, commands.add_note, who=vera, conversation_id=free.pk, body="Nota")[1]


def test_a_channel_account_changes_only_its_names_and_never_its_identity(
    world: SimpleNamespace, migrator: DB
) -> None:
    account = world.account_a
    done = run(world, commands.update_channel_account, channel_account_id=account.pk, version=1,
               name=" Ventas ", display_identifier="+51 999")  # fmt: skip
    assert (done.name, done.display_identifier, done.version) == (" Ventas ", "+51 999", 2)
    assert audit(migrator)[-1][0] == "channel_account.updated"
    same = run(world, commands.update_channel_account, channel_account_id=account.pk, version=2,
               name=" Ventas ")  # fmt: skip
    assert same.version == 2  # sin cambios: ni versión ni auditoría
    with pytest.raises(StaleVersion):
        run(
            world,
            commands.update_channel_account,
            channel_account_id=account.pk,
            version=1,
            name="x",
        )
    for field, value in (("platform", "SANDBOX"), ("external_account_id", "otro"),
                         ("status", "ERROR"), ("deleted_at", None)):  # fmt: skip
        with pytest.raises(ValueError, match="no admitidos"):
            changes: dict[str, Any] = {field: value}
            run(world, commands.update_channel_account, channel_account_id=account.pk, version=2,
                **changes)  # fmt: skip
    for bad in ("", "  ", 5, None, "a" * 101, "con\nsalto"):
        with pytest.raises(ValueError):  # noqa: PT011
            run(world, commands.update_channel_account, channel_account_id=account.pk, version=2,
                name=bad)  # fmt: skip
    with pytest.raises(ObjectDoesNotExist):
        run(
            world, commands.update_channel_account, channel_account_id=world.account_b.pk, version=1
        )
    stored = reload(world, ChannelAccount, account.pk)
    assert (stored.platform, stored.external_account_id, stored.status) == (
        "SANDBOX", account.external_account_id, "CONNECTED",
    )  # fmt: skip


@pytest.mark.parametrize("mode", ["AI_AUTONOMOUS", "AI_ASSISTED", "", None, 5])
def test_no_command_opens_a_conversation_in_an_ai_mode_before_phase_8(
    world: SimpleNamespace, mode: Any
) -> None:
    kwargs = {"contact_id": world.contact_a.pk, "channel_account_id": world.account_a.pk}
    with pytest.raises(ValueError, match="HUMAN"):
        run(world, commands.open_conversation, attention_mode=mode, **kwargs)
    with tenant_scope(ctx(world.a)):
        assert Conversation.objects.count() == 0
    assert run(world, commands.open_conversation, attention_mode="HUMAN", **kwargs)[1]


def test_out_of_order_delivery_statuses_are_no_ops_that_do_not_abort_the_transaction(
    world: SimpleNamespace, migrator: DB
) -> None:
    convo = reload(world, Conversation, inbound(world)[0].conversation_id)
    sent, _ = outbound(world, convo)
    assert sent.delivery_status == "QUEUED"  # siempre nace en cola

    def advance(pk: UUID, status: str) -> str:
        return str(
            run(world, commands.advance_delivery, message_id=pk, status=status).delivery_status
        )

    assert advance(sent.pk, "READ") == "READ"
    before = len(audit(migrator))
    for late in ("DELIVERED", "SENT", "QUEUED", "FAILED", "READ"):  # tardíos y repetidos
        assert advance(sent.pk, late) == "READ"
    assert len(audit(migrator)) == before
    other, _ = outbound(world, convo)
    assert (advance(other.pk, "DELIVERED"), advance(other.pk, "FAILED")) == (
        "DELIVERED",
        "DELIVERED",
    )
    with tenant_scope(ctx(world.a, world.ana)) as tenant:  # una sola transacción
        commands.advance_delivery(tenant, message_id=other.pk, status="READ")
        after = commands.advance_delivery(tenant, message_id=other.pk, status="SENT")
        assert after.delivery_status == "READ" and Message.objects.count() == 3  # sigue viva


@pytest.mark.parametrize("order", [("READ", "DELIVERED"), ("DELIVERED", "READ")])
def test_two_delivery_updates_at_once_end_on_the_later_state_without_errors(
    world: SimpleNamespace, order: tuple[str, str]
) -> None:
    convo = reload(world, Conversation, inbound(world)[0].conversation_id)
    message, _ = outbound(world, convo)

    def update(status: str) -> Callable[[Any], Any]:
        return lambda tenant: commands.advance_delivery(
            tenant, message_id=message.pk, status=status
        )

    first, second = race(world.a, world.ana, [update(order[0]), update(order[1])])
    assert not isinstance(first, str) and not isinstance(second, str)  # ninguna lanzó
    assert reload(world, Message, message.pk).delivery_status == "READ"


def test_each_delivery_status_stores_its_own_timestamp_and_a_naive_clock_is_refused(
    world: SimpleNamespace,
) -> None:
    convo = reload(world, Conversation, inbound(world)[0].conversation_id)
    message, _ = outbound(world, convo)
    first, second, third = (timezone.now() - dt.timedelta(minutes=m) for m in (30, 20, 10))

    def stamps(pk: UUID) -> tuple[Any, ...]:
        found = reload(world, Message, pk)
        return (found.sent_at, found.delivered_at, found.read_at, found.failed_at)

    def advance(status: str, at: dt.datetime) -> None:
        run(world, commands.advance_delivery, message_id=message.pk, status=status, at=at)

    advance("SENT", first)
    assert stamps(message.pk) == (first, None, None, None)
    advance("DELIVERED", second)
    assert stamps(message.pk) == (first, second, None, None)
    advance("READ", third)
    assert stamps(message.pk) == (first, second, third, None)
    failing, _ = outbound(world, convo)
    run(world, commands.advance_delivery, message_id=failing.pk, status="FAILED", at=third)
    assert stamps(failing.pk) == (None, None, None, third)
    for bad in (dt.datetime(2026, 11, 1, 9, 0), "2026-11-01T09:00:00Z", 5):  # sin zona horaria
        with pytest.raises(ValueError, match="zona horaria"):
            run(world, commands.advance_delivery, message_id=failing.pk, status="SENT", at=bad)


def test_the_same_client_id_in_two_conversations_at_once_is_a_conflict_not_a_crash(
    world: SimpleNamespace,
) -> None:
    here = reload(world, Conversation, inbound(world)[0].conversation_id)
    with tenant_scope(ctx(world.a)):
        who = Contact.objects.create(display_name="Otro cliente")
    there = reload(
        world, Conversation, inbound(world, "wamid.9", contact_id=who.pk)[0].conversation_id
    )
    client = uuid4()

    def reply(convo: Conversation) -> Callable[[Any], Any]:
        return lambda tenant: commands.record_outbound(
            tenant, conversation_id=convo.pk, client_message_id=client, body="Hola"
        )

    first, second = race(world.a, world.ana, [reply(here), reply(there)])
    assert first[1] is True and second == "ClientMessageConflict"  # sin IntegrityError


def test_the_preview_is_the_plain_start_of_the_last_external_message_and_never_a_note(
    world: SimpleNamespace,
) -> None:
    first, _ = inbound(world, body="Hola\n\n  quiero\t el   iPhone " + "x" * 200)
    convo = reload(world, Conversation, first.conversation_id)
    assert convo.last_message_direction == "INBOUND"
    assert convo.last_message_preview == ("Hola quiero el iPhone " + "x" * 200)[:80]
    assert len(convo.last_message_preview) == 80
    run(world, commands.add_note, conversation_id=convo.pk, body="Nota con el costo 700")
    after = reload(world, Conversation, convo.pk)  # una nota no toca ni el texto ni el sentido
    assert (after.last_message_preview, after.last_message_direction) == (
        convo.last_message_preview, "INBOUND",
    )  # fmt: skip
    outbound(world, convo, body="  Con   gusto ")
    sent = reload(world, Conversation, convo.pk)
    assert (sent.last_message_preview, sent.last_message_direction) == ("Con gusto", "OUTBOUND")
    outbound(world, convo, body="Segunda respuesta")  # ya abierta: otra rama del comando
    again = reload(world, Conversation, convo.pk)
    assert (again.last_message_preview, again.last_message_direction) == (
        "Segunda respuesta", "OUTBOUND",
    )  # fmt: skip
    inbound(world, "wamid.2", body="Gracias")
    last = reload(world, Conversation, convo.pk)
    assert (last.last_message_preview, last.last_message_direction) == ("Gracias", "INBOUND")
    inbound(world, "wamid.2", body="Otro texto")  # la repetición no cambia nada, ni la vista previa
    assert reload(world, Conversation, convo.pk).last_message_preview == "Gracias"


def test_a_closed_and_reopened_conversation_also_gets_its_preview(world: SimpleNamespace) -> None:
    convo = reload(world, Conversation, inbound(world)[0].conversation_id)
    resolve(world, convo, dt.timedelta(minutes=5))
    inbound(world, "wamid.2", body="Sigo aquí")
    again = reload(world, Conversation, convo.pk)
    assert again.status == "OPEN" and again.last_message_preview == "Sigo aquí"


@pytest.mark.parametrize(
    "bad",
    [
        "\ud800",
        "ab\udfffcd",
        "​",
        "​ ⁠﻿",
        "  ",
        "x\x07y",
        "x\x1by",
        "\x7f",
        "x\u202ey",
        "a\u2066b\u2069",
    ],
)
def test_texts_with_lone_surrogates_control_characters_or_nothing_visible_are_refused(
    world: SimpleNamespace, bad: str
) -> None:
    convo = tables.first(world)
    attempts: list[tuple[Callable[..., Any], dict[str, Any]]] = [
        (
            commands.record_inbound,
            {
                "contact_id": world.contact_a.pk,
                "channel_account_id": world.account_a.pk,
                "external_message_id": "w",
                "body": bad,
            },
        ),
        (
            commands.record_outbound,
            {"conversation_id": convo.pk, "client_message_id": uuid4(), "body": bad},
        ),
        (commands.add_note, {"conversation_id": convo.pk, "body": bad}),
    ]
    for command, fields in attempts:
        with pytest.raises(ValueError, match="^texto: ") as refused:
            run(world, command, **fields)
        assert "codec" not in str(refused.value)  # lo dice el comando, no la base ni Python
    with tenant_scope(ctx(world.a)):
        assert Message.objects.count() == 0


def test_text_with_accents_emoji_zero_width_joiners_and_line_breaks_is_kept(
    world: SimpleNamespace,
) -> None:
    text = "Hola ñandú 👨‍👩‍👧 ¿precio?\nlínea\ttab"
    message, _ = inbound(world, body=text)
    assert reload(world, Message, message.pk).body == text


FAMILY = "\U0001f468\u200d\U0001f469\u200d\U0001f467"  # tres emoji unidos por ZWJ


def test_the_preview_drops_invisible_format_characters_and_never_splits_a_joined_sequence() -> None:
    clean = commands._preview("a\u200bb\u2060c\ufeffd\u200ee\u202af\n\n  g")
    assert clean == "abcdef g"  # sin ancho cero, BOM ni marcas de dirección; una sola línea
    assert commands._preview("می\u200cخواهم") == "می\u200cخواهم"  # el ZWNJ del persa vale
    whole = "x" * (PREVIEW_MAX - len(FAMILY)) + FAMILY
    assert commands._preview(whole) == whole  # cabe entera
    for pad in (1, 2, 3, 4):  # el corte cae dentro de la secuencia
        cut = commands._preview("x" * (PREVIEW_MAX - len(FAMILY) + pad) + FAMILY)
        assert cut == "x" * (PREVIEW_MAX - len(FAMILY) + pad), (
            pad
        )  # sin media familia ni ZWJ suelto
    assert not commands._preview("x" * 79 + "\u200d" + "y").endswith("\u200d")
    emoji = "\U0001f600" * 76  # solo se quita la secuencia partida, no los emoji anteriores
    assert commands._preview(emoji + FAMILY) == emoji


def test_direction_marks_for_right_to_left_text_are_kept_in_the_body(
    world: SimpleNamespace,
) -> None:
    text = "שלום\u200f 123\u200e"  # LRM y RLM, no reordenan nada
    message, _ = inbound(world, body=text)
    assert reload(world, Message, message.pk).body == text
