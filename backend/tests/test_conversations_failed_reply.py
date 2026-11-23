"""F7b-14 (#1093): una respuesta que falla no da la conversación por contestada. «Sin responder»
mira la última respuesta que no falló (`PROVIDER_UNKNOWN` cuenta: pudo salir) y la solicitud de
persona que una respuesta atendió se reabre, como estaba, si esa respuesta falla y ninguna otra la
sustituye. Del envío por el adaptador al `failed` de Meta por el webhook, con PostgreSQL y
`crm_app`, hilos reales y las migraciones. Datos ficticios."""

import threading
import time
from collections.abc import Callable
from importlib import import_module
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from django.conf import settings
from django.core.management import call_command
from django.db import IntegrityError, connection

from apps.contacts.models import Contact
from apps.conversations import attention, commands, ingress, selectors
from apps.conversations.models import Conversation, Message
from apps.organizations.models import OrganizationMembership
from core.tenancy.scope import require_scope, tenant_scope
from tests import test_conversations_tables as tables
from tests.meta_sim import webhooks
from tests.test_authorization import give
from tests.test_conversation_commands import audit, outbound, run, world, writer
from tests.test_delivery_status import run as status
from tests.test_inventory_stock import race
from tests.test_memberships import ctx
from tests.test_meta_webhook import channel
from tests.test_pipelines_tables import as_migrator
from tests.test_whatsapp_outbound import graph, redis, send, talk

__all__ = ["channel", "graph", "redis", "world", "writer"]  # los fixtures, para pytest
pytestmark = pytest.mark.usefixtures("tenant_db", "channel")
DB = psycopg.Connection[Any]
INDEX = import_module("apps.conversations.migrations.0061_conversations_unreplied_index")
ADD = import_module("apps.conversations.migrations.0059_conversation_failed_reply")
FIRST = "wamid.SIMOUT000001"  # el `wamid` del primer envío del simulador
REOPENED = "conversation.human_request_reopened"


@pytest.fixture(autouse=True)
def fresh_wake(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ingress, "_last_wake", 0.0)


@pytest.fixture
def chat(world: SimpleNamespace, graph: Any) -> Conversation:
    """Una conversación de WhatsApp: el cliente pidió una persona y `ana` lo registró."""
    with tenant_scope(ctx(world.a)):
        mine = OrganizationMembership.objects.get(user_id=world.ana.pk).pk
    give(world.a, mine, {"messages.send": "ORGANIZATION", "conversations.view": "ORGANIZATION"})
    convo = talk(world)
    run(world, commands.record_inbound, contact_id=world.contact_a.pk, body="Una persona",
        channel_account_id=convo.channel_account_id, external_message_id="wamid.IN1")  # fmt: skip
    run(world, attention.request_human, conversation_id=convo.pk, reason="AGENT")
    return convo


def look(world: SimpleNamespace, convo: Conversation) -> tuple[Any, bool, bool]:
    """(la solicitud que ve la API: quién, cuándo y por qué, o `None`; `last_reply_failed`; si
    está en «Sin responder»)."""
    with tenant_scope(ctx(world.a)):
        row = Conversation.objects.get(pk=convo.pk)
        shown = (row.human_request_reason, row.human_requested_by_user_id, row.human_requested_at)
        waits = Conversation.objects.filter(selectors.UNANSWERED, pk=convo.pk).exists()
        return (shown if attention.waiting(row) else None), row.last_reply_failed, waits


def move(world: SimpleNamespace, message: Message, state: str, why: str | None = None) -> Any:
    return run(world, commands.advance_delivery, message_id=message.pk, status=state, reason=why)


def theirs(world: SimpleNamespace) -> Message:
    """Un mensaje del cliente de B, en su conversación."""
    wanted = {"contact_id": world.contact_b.pk, "channel_account_id": world.account_b.pk}
    found = run(world, commands.record_inbound, org="b", external_message_id="wamid.B1",
                body="B", **wanted)  # fmt: skip
    return found[0]  # type: ignore[no-any-return]


def reopened(migrator: DB) -> list[dict[str, Any]]:
    return [meta for action, _, meta in audit(migrator) if action == REOPENED]


def test_a_reply_meta_fails_by_webhook_keeps_the_request_and_the_conversation_unanswered(
    world: SimpleNamespace, chat: Conversation, migrator: DB
) -> None:
    asked = look(world, chat)[0]
    assert asked is not None and look(world, chat)[1:] == (False, True)
    message, saved = send(world, chat)  # Meta lo acepta (`SENT`): atendida y contestada
    assert saved.delivery_status == "SENT" and look(world, chat) == (None, False, False)
    events = len(migrator.execute("SELECT id FROM outbox_events").fetchall())
    failed = webhooks.status(FIRST, "failed", error=(131031, "x"))
    assert status(failed) == {"PROCESSED": 1}  # y luego `failed`: nada le llegó al cliente
    assert look(world, chat) == (asked, True, True)  # quién, cuándo y por qué: los mismos
    assert reopened(migrator) == [{"cause": "REPLY_FAILED", "message_id": str(message.pk)}]
    hints = migrator.execute(
        "SELECT event_type, aggregate_id, payload FROM outbox_events ORDER BY id OFFSET %s",
        [events],
    ).fetchall()  # la pantalla se entera (solo ids)
    assert ("conversation.updated", chat.pk, {}) in hints
    status(webhooks.status(FIRST, "failed", error=(131026, "y")))  # otro: nada más cambia
    assert len(reopened(migrator)) == 1 and look(world, chat) == (asked, True, True)


def test_a_later_reply_that_did_not_fail_answers_and_the_failed_one_never_reopens_it(
    world: SimpleNamespace, chat: Conversation, migrator: DB
) -> None:
    first, _ = outbound(world, chat, body="Te ayudo")
    second, _ = outbound(world, chat, body="¿Qué talla?")  # mientras la primera sigue en cola
    move(world, first, "FAILED", "PROVIDER_REJECTED")
    assert look(world, chat) == (None, False, False)  # la segunda pudo llegar: la sustituye
    move(world, second, "SENT")
    assert look(world, chat) == (None, False, False) and reopened(migrator) == []
    move(world, second, "FAILED", "PROVIDER_REJECTED")  # tampoco llegó: se reabre
    assert look(world, chat)[1:] == (True, True) and len(reopened(migrator)) == 1
    third, _ = outbound(world, chat, body="Perdón, ya estoy aquí")  # otra respuesta: contestada
    assert look(world, chat) == (None, False, False)
    move(world, third, "SENT")
    assert look(world, chat) == (None, False, False)


def test_a_reply_that_got_out_answers_even_if_a_later_one_fails_or_is_the_handoff_text(
    world: SimpleNamespace, chat: Conversation, migrator: DB
) -> None:
    sent, _ = outbound(world, chat, body="Te ayudo")
    move(world, sent, "DELIVERED")
    after, _ = outbound(world, chat, body="Otra cosa")
    move(world, after, "FAILED", "WINDOW_CLOSED")
    assert look(world, chat) == (None, False, False) and reopened(migrator) == []
    run(world, attention.close_request, conversation_id=chat.pk)  # nada abierto: no cambia
    run(world, attention.request_human, conversation_id=chat.pk, reason="AGENT")
    real, _ = outbound(world, chat, body="Ya te atiendo")
    handoff, _ = outbound(world, chat, body=settings.AI_HANDOFF_REPLY)  # el inicial
    move(world, real, "FAILED", "PROVIDER_REJECTED")  # el texto de traspaso no atiende
    assert look(world, chat)[0] is not None and len(reopened(migrator)) == 1
    move(world, handoff, "SENT")
    assert look(world, chat)[1:] == (False, False)  # pero sí le llegó algo: contestada


def test_a_reply_that_may_have_gone_out_counts_as_an_answer(
    world: SimpleNamespace, chat: Conversation, migrator: DB
) -> None:
    message, _ = outbound(world, chat)
    move(world, message, "SENDING")
    assert run(world, commands.fail_sending, message_id=message.pk, reason="PROVIDER_UNKNOWN")
    assert look(world, chat) == (None, False, False) and reopened(migrator) == []
    later, _ = outbound(world, chat, body="¿Me lees?")
    move(world, later, "FAILED", "PROVIDER_REJECTED")  # la que pudo salir sigue contando
    assert look(world, chat) == (None, False, False) and reopened(migrator) == []
    move(world, message, "READ")  # un estado tardío lo reconcilia: sigue contestada
    assert look(world, chat) == (None, False, False)
    with tenant_scope(ctx(world.a)):  # el cliente nunca escribió: no hay nada que responder
        quiet = Contact.objects.create(display_name="Cliente sin mensajes")
    account = SimpleNamespace(pk=chat.channel_account_id)
    alone = tables.conversation(world.a, quiet, account, "CONV-00002",
                                last_inbound_at=chat.last_inbound_at)  # fmt: skip
    first, _ = outbound(world, alone)
    move(world, first, "FAILED", "PROVIDER_REJECTED")
    assert look(world, alone) == (None, False, False)


def test_a_customer_message_or_closing_it_ends_the_failed_mark_and_the_saved_request(
    world: SimpleNamespace, chat: Conversation, migrator: DB
) -> None:
    message, _ = outbound(world, chat)
    move(world, message, "FAILED", "PROVIDER_REJECTED")
    assert look(world, chat)[1:] == (True, True)
    run(world, commands.record_inbound, contact_id=world.contact_a.pk, body="¿Hola?",
        channel_account_id=chat.channel_account_id, external_message_id="wamid.IN2")  # fmt: skip
    assert look(world, chat)[1:] == (False, True)  # lo último es del cliente
    again, _ = outbound(world, chat)
    run(world, attention.close_request, conversation_id=chat.pk)  # atendida: no hay nada que cerrar
    move(world, again, "FAILED", "PROVIDER_REJECTED")
    assert look(world, chat)[0] is not None  # ni a mano se pierde lo atendido por una que falló
    taken, _ = outbound(world, chat)
    with tenant_scope(ctx(world.a)) as tenant:
        row = Conversation.objects.get(pk=chat.pk)
        attention.clear(tenant, require_scope(tenant), row, "ASSIGNED")
    move(world, taken, "FAILED", "PROVIDER_REJECTED")  # tomada por una persona: no vuelve
    assert look(world, chat)[0] is None and len(reopened(migrator)) == 2


def test_another_organization_cannot_touch_the_conversation_and_the_database_keeps_it_whole(
    world: SimpleNamespace, chat: Conversation
) -> None:
    message, _ = outbound(world, chat)
    with pytest.raises(Message.DoesNotExist):  # el mensaje de A no existe para B
        run(world, commands.advance_delivery, org="b", message_id=message.pk, status="FAILED")
    assert look(world, chat) == (None, False, False)
    other = theirs(world)

    def refused(name: str, **fields: Any) -> None:
        with pytest.raises(IntegrityError) as error, tenant_scope(ctx(world.a)):
            Conversation.objects.filter(pk=chat.pk).update(**fields)
        assert error.value.__cause__.diag.constraint_name == name  # type: ignore[union-attr]

    refused("conversations_human_reply_fk", human_request_reply_id=other.pk)  # de B
    refused("conversations_human_reply_fk", human_request_reply_id=uuid4())
    gone = dict.fromkeys(attention.FIELDS)  # la respuesta guardada, sin solicitud
    refused("conversations_human_reply_ck", **gone)
    refused(
        "conversations_reply_failed_ck", last_reply_failed=True, last_message_direction="INBOUND"
    )


@pytest.mark.parametrize("first", ["reply", "failure"])
def test_a_failure_and_a_new_reply_at_once_end_answered_in_either_order(
    world: SimpleNamespace, chat: Conversation, first: str
) -> None:
    message, _ = outbound(world, chat)
    reply: Callable[[Any], Any] = lambda tenant: commands.record_outbound(  # noqa: E731
        tenant, conversation_id=chat.pk, client_message_id=uuid4(), body="Otra",
        purpose="CUSTOMER_REPLY",
    )  # fmt: skip
    fail: Callable[[Any], Any] = lambda tenant: commands.advance_delivery(  # noqa: E731
        tenant, message_id=message.pk, status="FAILED", reason="PROVIDER_REJECTED"
    )
    wants = [reply, fail] if first == "reply" else [fail, reply]
    outcome = race(world.a, world.ana, wants)
    assert not any(isinstance(one, str) for one in outcome), outcome  # ni un bloqueo mutuo
    assert look(world, chat) == (None, False, False)


def test_a_token_failure_takes_the_account_before_the_conversation_like_the_claim(
    world: SimpleNamespace, chat: Conversation, migrator: DB
) -> None:
    send(world, chat)
    account, held, done = chat.channel_account_id, threading.Event(), list[Any]([None])

    def claim() -> None:  # el orden de `claim_outbound`: cuenta → contacto → conversación
        try:
            with tenant_scope(ctx(world.a)), connection.cursor() as cursor:
                cursor.execute("BEGIN")
                cursor.execute("SELECT 1 FROM channel_accounts WHERE id = %s FOR SHARE", [account])
                held.set()
                for _ in range(100):  # hasta que el webhook espera a la cuenta
                    found = migrator.execute(
                        "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock'"
                    ).fetchone()
                    if found and found[0]:
                        break
                    time.sleep(0.05)
                cursor.execute(
                    "SELECT 1 FROM conversations WHERE id = %s FOR NO KEY UPDATE", [chat.pk]
                )
                cursor.execute("COMMIT")
                done[0] = "ok"
        except Exception as error:  # noqa: BLE001 — lo comprueba la prueba
            done[0] = type(error).__name__
        finally:
            connection.close()

    thread = threading.Thread(target=claim)
    thread.start()
    assert held.wait(timeout=10)
    assert status(webhooks.status(FIRST, "failed", error=(190, "x"))) == {"PROCESSED": 1}
    thread.join(timeout=10)
    assert done == ["ok"] and look(world, chat)[1:] == (True, True)
    with tenant_scope(ctx(world.a)):
        assert Conversation.objects.get(pk=chat.pk).channel_account.status_detail == "TOKEN_INVALID"


def test_the_unanswered_filter_uses_its_partial_index(world: SimpleNamespace, migrator: DB) -> None:
    with tenant_scope(ctx(world.a)), connection.cursor() as cursor:
        found = Conversation.objects.filter(selectors.UNANSWERED).order_by(
            "-last_message_at", "-id"
        )
        sql, params = found.values("id").query.sql_with_params()
        cursor.execute("SET LOCAL enable_seqscan = off")
        cursor.execute("EXPLAIN " + sql, params)
        plan = " ".join(row[0] for row in cursor.fetchall())
    assert INDEX.INDEX in plan, plan


def test_the_migrations_mark_old_failures_rebuild_the_index_and_go_back_and_forth(
    world: SimpleNamespace, chat: Conversation, migrator: DB
) -> None:
    theirs(world)  # dos conversaciones: un índice único de una constante falla
    message, _ = outbound(world, chat)
    move(world, message, "FAILED", "PROVIDER_REJECTED")
    answered, _ = outbound(world, chat)  # atendida, pendiente: hacia atrás queda cerrada
    stamp = "UPDATE conversations SET last_reply_failed = false WHERE id = %s"
    migrator.execute(stamp, [chat.pk])
    with as_migrator():
        call_command("migrate", "conversations", "0058", verbosity=0)
    assert migrator.execute(INDEX.STATE, [INDEX.INDEX]).fetchall() == []
    row = "SELECT human_requested_at FROM conversations WHERE id = %s"
    assert migrator.execute(row, [chat.pk]).fetchone() == (None,)  # cerrada, como antes
    migrator.execute(
        f"CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS {INDEX.INDEX} ON conversations (id)"
    )  # un intento anterior, distinto: válido, así que la migración se niega
    with as_migrator(), pytest.raises(RuntimeError, match="otra definición"):
        call_command("migrate", "conversations", verbosity=0)
    migrator.execute(f"DROP INDEX {INDEX.INDEX}")
    with pytest.raises(psycopg.errors.UniqueViolation):  # un `CONCURRENTLY` fallido: `INVALID`
        migrator.execute(f"CREATE UNIQUE INDEX CONCURRENTLY {INDEX.INDEX} ON conversations ((1))")
    with as_migrator():
        call_command("migrate", "conversations", verbosity=0)
    assert migrator.execute(INDEX.STATE, [INDEX.INDEX]).fetchall() == [(True, INDEX.DEFINITION)]
    move(world, answered, "FAILED", "PROVIDER_REJECTED")  # tampoco llegó
    migrator.execute(stamp, [chat.pk])
    with as_migrator():
        call_command("migrate", "conversations", "0059", verbosity=0)
        call_command("migrate", "conversations", verbosity=0)  # la `0060` la marca otra vez
    assert look(world, chat)[1:] == (True, True)
    with pytest.raises(RuntimeError, match="BYPASSRLS"):  # `crm_app` no ve todas las filas
        ADD.restore_closed(None, SimpleNamespace(connection=connection))
