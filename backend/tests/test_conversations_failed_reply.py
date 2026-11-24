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
from django.db.models import F
from django.utils import timezone

from apps.contacts.models import Contact
from apps.conversations import attention, commands, ingress, selectors
from apps.conversations.models import Conversation, Message
from apps.organizations.models import OrganizationMembership
from core.tenancy.scope import require_scope, tenant_scope
from tests import test_conversations_tables as tables
from tests.conftest import migrator_conninfo
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
INDEX = import_module("apps.conversations.migrations.0063_conversations_unreplied_index")
ADD = import_module("apps.conversations.migrations.0061_conversation_failed_reply")
FIRST = "wamid.SIMOUT000001"  # el `wamid` del primer envío del simulador
REOPENED = "conversation.human_request_reopened"
WAITING = "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock'"


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


def other(world: SimpleNamespace, chat: Conversation, number: str, wrote: bool) -> Conversation:
    """Otra conversación de la misma cuenta de WhatsApp, con o sin un mensaje del cliente."""
    with tenant_scope(ctx(world.a)):
        made = Contact.objects.create(display_name=f"Cliente {number}")
    found: Conversation = tables.conversation(
        world.a, made, SimpleNamespace(pk=chat.channel_account_id), number,
        last_inbound_at=chat.last_inbound_at,
    )  # fmt: skip
    if wrote:
        run(world, commands.record_inbound, contact_id=made.pk, body="Hola", external_message_id=
            f"wamid.{number}", channel_account_id=chat.channel_account_id)  # fmt: skip
    return found


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
    alone = other(world, chat, "CONV-00002", wrote=False)  # el cliente nunca escribió
    first, _ = outbound(world, alone)
    move(world, first, "FAILED", "PROVIDER_REJECTED")
    assert look(world, alone) == (None, False, False)


def test_closing_it_by_hand_keeps_the_saved_reply_and_taking_it_drops_it(
    world: SimpleNamespace, chat: Conversation, migrator: DB
) -> None:
    again, _ = outbound(world, chat)
    run(world, attention.close_request, conversation_id=chat.pk)  # atendida: no hay nada que cerrar
    move(world, again, "FAILED", "PROVIDER_REJECTED")
    assert look(world, chat)[0] is not None  # ni a mano se pierde lo atendido por una que falló
    taken, _ = outbound(world, chat)
    with tenant_scope(ctx(world.a)) as tenant:
        row = Conversation.objects.get(pk=chat.pk)
        attention.clear(tenant, require_scope(tenant), row, "ASSIGNED")
    move(world, taken, "FAILED", "PROVIDER_REJECTED")  # tomada por una persona: no vuelve
    assert look(world, chat)[0] is None and len(reopened(migrator)) == 1


def test_another_organization_cannot_touch_the_conversation_and_the_database_keeps_it_whole(
    world: SimpleNamespace, chat: Conversation, migrator: DB
) -> None:
    message, _ = outbound(world, chat)
    with pytest.raises(Message.DoesNotExist):  # el mensaje de A no existe para B
        run(world, commands.advance_delivery, org="b", message_id=message.pk, status="FAILED")
    assert look(world, chat) == (None, False, False)
    b = {"contact_id": world.contact_b.pk, "channel_account_id": world.account_b.pk}
    theirs = run(world, commands.record_inbound, org="b", external_message_id="wamid.B1",
                 body="B", **b)[0]  # fmt: skip
    for bad in (theirs.pk, uuid4()):  # un mensaje de B, o ninguno
        with pytest.raises(IntegrityError) as error, tenant_scope(ctx(world.a)):
            Conversation.objects.filter(pk=chat.pk).update(human_request_reply_id=bad)
        name = error.value.__cause__.diag.constraint_name  # type: ignore[union-attr]
        assert name == "conversations_human_reply_fk"
    names = ("conversations_human_reply_ck", "conversations_reply_failed_ck")  # de respaldo: el
    sql = "SELECT count(*) FROM pg_constraint WHERE conname = ANY(%s) AND convalidated"
    assert migrator.execute(sql, [list(names)]).fetchone() == (2,)  # disparador va antes


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


@pytest.mark.parametrize(("meta", "mark"), [(190, "TOKEN_INVALID"), (131048, "QUALITY_LOW")])
def test_a_failure_that_marks_the_account_takes_it_before_the_conversation_like_the_claim(
    world: SimpleNamespace, chat: Conversation, migrator: DB, meta: int, mark: str
) -> None:
    send(world, chat)
    done: list[Any] = []
    failed = webhooks.status(FIRST, "failed", error=(meta, "x"))

    def hook_run() -> None:
        done.append(status(failed))
        connection.close()

    hook = threading.Thread(target=hook_run)
    with psycopg.connect(migrator_conninfo()) as claim:  # su orden: cuenta → … → conversación
        claim.execute("SELECT 1 FROM channel_accounts WHERE id = %s FOR SHARE",
                      [chat.channel_account_id])  # fmt: skip
        hook.start()
        for _ in range(200):  # hasta que el webhook espera a la cuenta
            if migrator.execute(WAITING).fetchone() != (0,):
                break
            time.sleep(0.05)
        claim.execute("SELECT 1 FROM conversations WHERE id = %s FOR NO KEY UPDATE", [chat.pk])
    hook.join(timeout=10)
    assert done == [{"PROCESSED": 1}] and look(world, chat)[1:] == (True, True)
    with tenant_scope(ctx(world.a)):
        assert Conversation.objects.get(pk=chat.pk).channel_account.status_detail == mark


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
    alone, unsure = (
        other(world, chat, n, wrote=n.endswith("3")) for n in ("CONV-00002", "CONV-00003")
    )
    message, _ = outbound(world, chat)
    move(world, message, "FAILED", "PROVIDER_REJECTED")
    answered, _ = outbound(world, chat)  # atendida, pendiente: hacia atrás queda cerrada
    stamp = "UPDATE conversations SET last_reply_failed = false WHERE id = %s"
    migrator.execute(stamp, [chat.pk])
    with as_migrator():
        call_command("migrate", "conversations", "0060", verbosity=0)
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
    move(world, outbound(world, alone)[0], "FAILED", "PROVIDER_REJECTED")  # nadie le escribió
    maybe, _ = outbound(world, unsure)
    move(world, maybe, "SENDING")
    run(world, commands.fail_sending, message_id=maybe.pk, reason="PROVIDER_UNKNOWN")  # pudo salir
    migrator.execute("UPDATE conversations SET last_reply_failed = false")
    with as_migrator():
        call_command("migrate", "conversations", "0061", verbosity=0)
        call_command("migrate", "conversations", verbosity=0)  # la `0062` la marca otra vez
    assert look(world, chat)[1:] == (True, True)
    assert look(world, alone)[1] is False and look(world, unsure)[1] is False
    with pytest.raises(RuntimeError, match="BYPASSRLS"):  # `crm_app` no ve todas las filas
        ADD.restore_closed(None, SimpleNamespace(connection=connection))


def test_what_the_previous_image_writes_keeps_working_on_the_new_columns(
    world: SimpleNamespace, chat: Conversation
) -> None:
    """ADR-033 §7: la imagen anterior (al migrar o tras volver atrás) no conoce las columnas nuevas.
    Lo que escriben sus `record_outbound`, `record_inbound`, `_assign`, `clear` y `_status`."""

    def old(**fields: Any) -> None:
        with tenant_scope(ctx(world.a)):
            Conversation.objects.filter(pk=chat.pk).update(**fields, updated_at=timezone.now())

    def flagged() -> None:
        move(world, outbound(world, chat)[0], "FAILED", "PROVIDER_REJECTED")
        assert look(world, chat)[1] is True

    flagged()
    old(last_message_at=timezone.now(), last_message_preview="Hola",
        last_message_direction="OUTBOUND")  # fmt: skip
    assert look(world, chat)[1] is False  # un mensaje nuevo quita la marca
    flagged()
    old(last_message_at=timezone.now(), last_message_preview="¿Hola?", standby_since=None,
        last_message_direction="INBOUND", last_inbound_at=timezone.now())  # fmt: skip
    assert look(world, chat)[1:] == (False, True)
    outbound(world, chat)  # atendida: la respuesta guardada
    old(assigned_user_id=world.ana.pk, assigned_at=timezone.now(), status="OPEN",
        assignment_version=F("assignment_version") + 1, version=F("version") + 1)  # fmt: skip
    old(**dict.fromkeys(attention.FIELDS))  # su `clear`: la respuesta se va con la solicitud
    old(status="RESOLVED", resolved_at=timezone.now(), version=F("version") + 1)
    with tenant_scope(ctx(world.a)):
        row = Conversation.objects.get(pk=chat.pk)
    assert (row.human_request_reply_id, row.last_reply_failed, row.status) == (
        None,
        False,
        "RESOLVED",
    )


def test_every_new_message_ends_the_failed_mark(world: SimpleNamespace, chat: Conversation) -> None:
    failed, _ = outbound(world, chat)
    move(world, failed, "FAILED", "PROVIDER_REJECTED")
    with tenant_scope(ctx(world.a)):  # en cola otra vez (soltada): la respuesta la abre
        Conversation.objects.filter(pk=chat.pk).update(status="PENDING")
    again, _ = outbound(world, chat)
    assert look(world, chat)[1:] == (False, False)
    move(world, again, "FAILED", "PROVIDER_REJECTED")
    confirmed = run(world, commands.queue_confirmation, conversation_id=chat.pk, body="Listo")
    assert look(world, chat)[1:] == (False, False)  # la confirmación de una baja también
    move(world, confirmed, "FAILED", "PROVIDER_REJECTED")
    with tenant_scope(ctx(world.a)):
        Conversation.objects.filter(pk=chat.pk).update(
            status="RESOLVED", resolved_at=timezone.now()
        )
    run(world, commands.record_inbound, contact_id=world.contact_a.pk, body="¿Hola?",
        channel_account_id=chat.channel_account_id, external_message_id="wamid.IN3")  # fmt: skip
    assert look(world, chat)[1:] == (False, True)  # la reabre el cliente


def test_only_what_counts_since_the_customer_wrote_and_only_a_person_answers_the_request(
    world: SimpleNamespace, chat: Conversation, migrator: DB
) -> None:
    old, _ = outbound(world, chat)
    move(world, old, "DELIVERED")  # contestó lo anterior, no lo de ahora
    run(world, commands.record_inbound, contact_id=world.contact_a.pk, body="¿Y ahora?",
        channel_account_id=chat.channel_account_id, external_message_id="wamid.IN9")  # fmt: skip
    run(world, attention.request_human, conversation_id=chat.pk, reason="AGENT")
    late, _ = outbound(world, chat, body="Ya voy")
    run(world, commands.queue_confirmation, conversation_id=chat.pk, body="Listo")  # del sistema
    move(world, late, "FAILED", "PROVIDER_REJECTED")
    assert look(world, chat)[0] is not None and len(reopened(migrator)) == 1  # no la atiende
    assert look(world, chat)[1] is False  # pero algo pudo llegar
    gone, _ = outbound(world, chat)
    run(world, commands.record_inbound, contact_id=world.contact_a.pk, body="¿Sigues?",
        channel_account_id=chat.channel_account_id, external_message_id="wamid.IN10")  # fmt: skip
    move(world, gone, "FAILED", "PROVIDER_REJECTED")  # llega tras el cliente: se reabre igual
    assert look(world, chat)[1:] == (False, True) and len(reopened(migrator)) == 2
    fresh, _ = outbound(world, chat)
    move(world, fresh, "FAILED", "PROVIDER_REJECTED")  # tras la del cliente, nada le llegó
    assert look(world, chat)[1:] == (True, True)
    closing, _ = outbound(world, chat)
    with tenant_scope(ctx(world.a)) as tenant:  # resuelta: ya no espera a nadie
        row = Conversation.objects.select_for_update().get(pk=chat.pk)
        commands._status(tenant, require_scope(tenant), row, "RESOLVED", "conversation.resolved",
                         resolved_at=timezone.now())  # fmt: skip
    move(world, closing, "FAILED", "PROVIDER_REJECTED")
    assert look(world, chat)[0] is None and len(reopened(migrator)) == 3


def test_a_provider_unknown_reply_that_meta_then_fails_reopens_it_like_any_failure(
    world: SimpleNamespace, chat: Conversation, migrator: DB
) -> None:
    asked = look(world, chat)[0]
    message, _ = outbound(world, chat)
    move(world, message, "SENDING")
    run(world, commands.fail_sending, message_id=message.pk, reason="PROVIDER_UNKNOWN")
    assert look(world, chat) == (None, False, False)  # pudo salir: contestada
    late = webhooks.status("wamid.LATE01", "failed", opaque=str(message.pk), error=(131031, "x"))
    assert status(late) == {"PROCESSED": 1}  # F7b-15: Meta dice que no salió
    assert look(world, chat) == (asked, True, True) and len(reopened(migrator)) == 1
