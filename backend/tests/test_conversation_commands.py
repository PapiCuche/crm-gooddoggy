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

from apps.contacts.models import Contact
from apps.conversations import commands
from apps.conversations.commands import (
    BadReference,
    StaleVersion,
)
from apps.conversations.models import ChannelAccount, Conversation, Message
from core.tenancy.scope import tenant_scope
from tests import test_conversations_tables as tables
from tests.factories import make_user
from tests.test_inventory_stock import race
from tests.test_memberships import ctx, join

world = tables.world
pytestmark = pytest.mark.usefixtures("tenant_db")
type DB = psycopg.Connection[Any]
SECRET = "Mi DNI es 12345678 y mi correo juan@example.com"  # noqa: S105 — texto de prueba


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


def outbound_of(org: UUID, convo: Any) -> Any:
    who = make_user()
    join(org, who)
    return tables.message(
        org, convo, direction="OUTBOUND", sender_type="USER", sender_user_id=who.pk,
        delivery_status="QUEUED",
    )  # fmt: skip


def outbound(world: SimpleNamespace, convo: Any) -> Any:
    """Un mensaje saliente en cola, escrito directamente: el comando de salida llega en F6-01d."""
    return tables.message(
        world.a, convo, direction="OUTBOUND", sender_type="USER",
        sender_user_id=world.ana.pk, delivery_status="QUEUED",
    )  # fmt: skip


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


def test_the_delivery_status_advances_and_ignores_what_does_not(
    world: SimpleNamespace, migrator: DB
) -> None:
    convo = reload(world, Conversation, inbound(world)[0].conversation_id)
    sent = outbound(world, convo)
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
    failing = outbound(world, convo)
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
    run(world, commands.advance_delivery, message_id=outbound(world, convo).pk, status="DELIVERED")
    rows = audit(migrator)
    actions = {a for a, *_ in rows}
    assert actions >= {"conversation.created", "message.received", "message.delivery_changed"}
    for word in ("iPhone", "azul", "mañana"):
        assert word not in json.dumps(rows, ensure_ascii=False)
    received = [m for a, _, m in rows if a == "message.received"]
    assert received == [
        {"conversation_id": str(convo.pk), "direction": "INBOUND", "length": len(plain)}
    ]  # solo la longitud, ids y dirección


def test_another_organizations_rows_do_not_exist_for_a_command(world: SimpleNamespace) -> None:
    theirs = tables.conversation(world.b, world.contact_b, world.account_b)
    with pytest.raises(ObjectDoesNotExist):  # su mensaje saliente tampoco existe para A
        run(
            world,
            commands.advance_delivery,
            message_id=outbound_of(world.b, theirs).pk,
            status="SENT",
        )
    with pytest.raises(BadReference):
        inbound(world, contact_id=world.contact_b.pk)


@pytest.mark.parametrize("bad", [None, 5, 1.5, True, "x", [], {}, b"x", "", " "])
def test_hostile_arguments_are_a_value_error_never_a_type_or_integrity_error(
    world: SimpleNamespace, bad: Any
) -> None:
    ok = {"contact_id": world.contact_a.pk, "channel_account_id": world.account_a.pk}
    calls: list[tuple[Callable[..., Any], dict[str, Any]]] = [
        (commands.open_conversation, ok | {"contact_id": bad}),
        (commands.open_conversation, ok | {"channel_account_id": bad}),
        (commands.record_inbound, ok | {"external_message_id": "a", "body": bad}),
        (commands.record_inbound, ok | {"external_message_id": bad, "body": "x"}),
        (commands.advance_delivery, {"message_id": bad, "status": "SENT"}),
        (commands.advance_delivery, {"message_id": uuid4(), "status": bad}),
    ]
    texty = isinstance(bad, str) and bad.strip() != ""  # un texto vale como cuerpo o id externo
    for command, fields in calls:
        if texty and command is commands.record_inbound and "body" in fields:
            continue
        with pytest.raises(ValueError):  # noqa: PT011
            run(world, command, **fields)


@pytest.mark.parametrize(
    "bad",
    ["a" * 4097, "x\x00y", "\n\n", "a" * 201, " lleva espacios ", "con\nsalto"],
)
def test_texts_and_external_ids_with_limits_are_refused(world: SimpleNamespace, bad: str) -> None:
    ok = {"contact_id": world.contact_a.pk, "channel_account_id": world.account_a.pk}
    refused: list[tuple[Callable[..., Any], dict[str, Any]]] = [
        (commands.record_inbound, ok | {"external_message_id": "a", "body": bad}),
    ]
    if len(bad) < 4097 and bad.strip() and "\x00" not in bad:  # lo que solo vale como id externo
        refused = [(commands.record_inbound, ok | {"external_message_id": bad, "body": "x"})]
    for command, fields in refused:
        with pytest.raises(ValueError):  # noqa: PT011
            run(world, command, **fields)
    with tenant_scope(ctx(world.a)):
        assert Message.objects.count() == 0


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
    sent = outbound(world, convo)

    def advance(pk: UUID, status: str) -> str:
        return str(
            run(world, commands.advance_delivery, message_id=pk, status=status).delivery_status
        )

    assert advance(sent.pk, "READ") == "READ"
    before = len(audit(migrator))
    for late in ("DELIVERED", "SENT", "QUEUED", "FAILED", "READ"):  # tardíos y repetidos
        assert advance(sent.pk, late) == "READ"
    assert len(audit(migrator)) == before
    other = outbound(world, convo)
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
    message = outbound(world, convo)

    def update(status: str) -> Callable[[Any], Any]:
        return lambda tenant: commands.advance_delivery(
            tenant, message_id=message.pk, status=status
        )

    first, second = race(world.a, world.ana, [update(order[0]), update(order[1])])
    assert not isinstance(first, str) and not isinstance(second, str)  # ninguna lanzó
    assert reload(world, Message, message.pk).delivery_status == "READ"
