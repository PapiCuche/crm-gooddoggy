"""F7b-15: un `failed` definitivo de Meta corrige el `PROVIDER_UNKNOWN` del plazo (una sola vez, con
la auditoría, el aviso y los efectos sobre la cuenta), el disparador `messages_failure_settled` no
deja reescribir un motivo definitivo, y WhatsApp traduce igual sus códigos en la respuesta del envío
y en el webhook. PostgreSQL real con `crm_app`."""

import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from django.db import IntegrityError, connection, connections

from apps.conversations import commands, delivery, tasks, whatsapp
from apps.conversations.channels import AmbiguousChannelError, TemporaryChannelError
from apps.conversations.models import ChannelAccount, Message
from core.tenancy.scope import require_scope, tenant_scope
from tests import test_conversations_tables as tables
from tests.conftest import migrator_conninfo
from tests.meta_sim import webhooks
from tests.test_conversation_commands import audit, world, writer
from tests.test_conversations_dispatch import Retrying, state
from tests.test_delivery_status import WAMID, fresh_wake, run
from tests.test_memberships import ctx
from tests.test_meta_webhook import channel
from tests.test_whatsapp_outbound import graph, redis, send, talk

__all__ = ["channel", "fresh_wake", "graph", "redis", "world", "writer"]  # fixtures, para pytest
pytestmark = pytest.mark.usefixtures("tenant_db", "channel")
AT = 1_700_000_100


def unknown(world: SimpleNamespace, graph: Any, migrator: Any) -> Message:
    """Un saliente que Meta aceptó sin que llegara la respuesta y que el plazo cerró."""
    graph.then("cut_after")
    message, _ = send(world, talk(world))
    migrator.execute("UPDATE messages SET sending_at = now() - interval '1 hour'")
    migrator.execute("UPDATE credentials SET created_at = now() - interval '1 day'")  # sin rotar
    assert tasks.sweep_sending() == 1
    closed = state(world, message)
    assert (closed.delivery_status, closed.provider_error_code) == ("FAILED", "PROVIDER_UNKNOWN")
    return message  # type: ignore[no-any-return]


def failed(message: Message, code: int, at: int = AT) -> dict[str, Any]:
    return webhooks.status(WAMID, "failed", opaque=str(message.pk), error=(code, "x"), at=at)


def apply(world: SimpleNamespace, message: Message, payload: dict[str, Any]) -> None:
    with tenant_scope(ctx(world.a)) as tenant:
        account = ChannelAccount.objects.get(pk=message.channel_account_id)
        assert delivery.apply(tenant, account, payload) is None


def corrections(migrator: Any) -> list[Any]:
    return [
        m for a, _, m in audit(migrator) if a == "message.delivery_changed" and "corrected" in m
    ]


@pytest.mark.parametrize(
    ("code", "reason", "account"),
    [(131048, "QUALITY_BLOCKED", ("DEGRADED", "QUALITY_LOW")),
     (190, "TOKEN_INVALID", ("ERROR", "TOKEN_INVALID"))],
)  # fmt: skip
def test_the_correction_changes_the_account_once_however_often_meta_repeats_it(
    world: SimpleNamespace, graph: Any, migrator: Any, code: int, reason: str, account: Any
) -> None:
    message = unknown(world, graph, migrator)
    run(failed(message, code))
    for _ in range(2):  # repetido: por la ingesta y directo al procesador
        run(failed(message, code))
        apply(world, message, failed(message, code))
    saved = state(world, message)
    assert (saved.provider_error_code, saved.provider_error_detail) == (reason, str(code))
    with tenant_scope(ctx(world.a)):
        found = ChannelAccount.objects.get(pk=message.channel_account_id)
    assert (found.status, found.status_detail) == account
    assert len([a for a, *_ in audit(migrator) if a.startswith("channel_account.")]) == 1
    assert corrections(migrator) == [{"reason": reason, "corrected": "PROVIDER_UNKNOWN"}]


def test_a_corrected_190_from_before_a_rotation_leaves_the_new_token_alone(
    world: SimpleNamespace, graph: Any, migrator: Any
) -> None:
    from apps.conversations import credentials
    from tests.meta_sim.graph import TEST_TOKEN

    message = unknown(world, graph, migrator)  # reclamado antes de la rotación
    with tenant_scope(ctx(world.a)) as tenant:  # se rota el token tras el plazo
        account = ChannelAccount.objects.get(pk=message.channel_account_id)
        credentials.revoke(tenant, account.credential_id)
        fresh = credentials.store(tenant, account.pk, TEST_TOKEN, rotated=True)
        ChannelAccount.objects.filter(pk=account.pk).update(credential_id=fresh.pk)
    run(failed(message, 190))  # llega tarde: era del token viejo
    assert state(world, message).provider_error_code == "TOKEN_INVALID"  # sí se corrige
    with tenant_scope(ctx(world.a)):
        assert ChannelAccount.objects.get(pk=account.pk).status == "CONNECTED"
    assert corrections(migrator) == [{"reason": "TOKEN_INVALID", "corrected": "PROVIDER_UNKNOWN"}]


def test_two_failed_statuses_at_once_correct_the_message_once(
    world: SimpleNamespace, graph: Any, migrator: Any
) -> None:
    message = unknown(world, graph, migrator)
    migrator.execute("DELETE FROM outbox_events")
    start = threading.Barrier(3)

    def go(code: int) -> str:
        try:
            start.wait(timeout=10)
            apply(world, message, failed(message, code))
            return "APPLIED"
        except delivery.NotYet:  # el otro ya le puso el `wamid`: la ingesta lo repite
            return "RETRY"
        finally:
            connection.close()

    with psycopg.connect(migrator_conninfo()) as held:  # la fila bloqueada: esperan los dos
        held.execute("SELECT 1 FROM messages WHERE id = %s FOR UPDATE", [message.pk])
        with ThreadPoolExecutor(2) as pool:
            done = pool.map(go, (131047, 131026))
            start.wait(timeout=10)
            threading.Event().wait(0.5)  # los dos ya esperan el bloqueo
            held.commit()
            outcomes = sorted(done)
    assert len(corrections(migrator)) == 1 and outcomes in (["APPLIED"] * 2, ["APPLIED", "RETRY"])
    apply(world, message, failed(message, 131026))  # el reintento ya no cambia nada
    assert state(world, message).provider_error_code in ("WINDOW_CLOSED", "RECIPIENT_UNREACHABLE")
    assert migrator.execute("SELECT count(*) FROM outbox_events").fetchone() == (1,)


def test_only_a_definite_reason_corrects_an_unknown_one(
    world: SimpleNamespace, graph: Any, migrator: Any
) -> None:
    message = unknown(world, graph, migrator)
    with tenant_scope(ctx(world.a)) as tenant:
        for reason in (None, "PROVIDER_UNKNOWN"):  # sin motivo, o el mismo: nada cambia
            kept = commands.advance_delivery(
                tenant, message_id=message.pk, status="FAILED", reason=reason
            )
            assert kept.provider_error_code == "PROVIDER_UNKNOWN"
    assert corrections(migrator) == []
    run(failed(message, 999999))  # uno que no está en la tabla también es definitivo
    assert state(world, message).provider_error_code == "PROVIDER_REJECTED"


def test_a_message_of_another_organization_or_account_is_never_corrected(
    world: SimpleNamespace, graph: Any, migrator: Any
) -> None:
    unsure = {"direction": "OUTBOUND", "sender_type": "USER", "delivery_status": "FAILED",
              "provider_error_code": "PROVIDER_UNKNOWN"}  # fmt: skip
    theirs = tables.message(
        world.b, tables.conversation(world.b, world.contact_b, world.account_b),
        sender_user_id=world.luis.pk, **unsure,
    )  # fmt: skip
    mine = tables.message(  # de A, pero de otra cuenta (la de la prueba, no la de WhatsApp)
        world.a, tables.conversation(world.a, world.contact_a, world.account_a, "CONV-08001"),
        sender_user_id=world.ana.pk, **unsure,
    )  # fmt: skip
    talk(world)  # la cuenta de WhatsApp de A, a la que llega el estado
    for n, other in enumerate((theirs, mine)):
        status = webhooks.status(
            f"wamid.NOPE{n}", "failed", opaque=str(other.pk), error=(131047, "x")
        )
        assert run(status) == {"FAILED": 1}  # no existe para esta cuenta: se reintenta
        migrator.execute("UPDATE webhook_ingress SET status = 'PROCESSED', payload = NULL")
    with tenant_scope(ctx(world.b)):
        assert Message.objects.get(pk=theirs.pk).provider_error_code == "PROVIDER_UNKNOWN"
    assert state(world, mine).provider_error_code == "PROVIDER_UNKNOWN"
    assert corrections(migrator) == []


def as_app(world: SimpleNamespace, sql: str, *params: Any) -> None:
    """SQL crudo como `crm_app`, en su propia transacción."""
    with tenant_scope(ctx(world.a)) as tenant, connections[require_scope(tenant)].cursor() as cur:
        cur.execute(sql, list(params))


def test_the_database_lets_only_an_unknown_reason_be_corrected_once(
    world: SimpleNamespace, graph: Any, migrator: Any
) -> None:
    message = unknown(world, graph, migrator)
    code = "UPDATE messages SET provider_error_code = %s WHERE id = %s"
    unsure = [
        (code, None),
        ("UPDATE messages SET failed_at = now() - %s::interval WHERE id = %s", "1 day"),
    ]
    for sql, value in unsure:  # de «no se sabe» a nada, o solo otra hora
        with pytest.raises(IntegrityError, match="messages_failure_settled"):
            as_app(world, sql, value, message.pk)
    as_app(world, code, "WINDOW_CLOSED", message.pk)  # la corrección
    writes = [
        (code, "RECIPIENT_UNREACHABLE"), (code, "PROVIDER_UNKNOWN"), (code, None),
        ("UPDATE messages SET provider_error_detail = %s WHERE id = %s", "131047"),
        ("UPDATE messages SET failed_at = now() - %s::interval WHERE id = %s", "1 day"),
    ]  # fmt: skip
    for sql, value in writes:  # un motivo definitivo no se reescribe, ni su código ni su hora
        with pytest.raises(IntegrityError, match="messages_failure_settled"):
            as_app(world, sql, value, message.pk)
    with pytest.raises(Exception, match="messages_failure_settled"):  # tampoco el migrador
        migrator.execute(code, ["RECIPIENT_UNREACHABLE", message.pk])
    as_app(world, code, "WINDOW_CLOSED", message.pk)  # escribir lo mismo no es un cambio
    saved = state(world, message)
    assert (saved.provider_error_code, saved.provider_error_detail) == ("WINDOW_CLOSED", None)


def test_the_settled_trigger_runs_as_the_caller(migrator: Any) -> None:
    oid = "'public.messages_failure_settled()'::regprocedure"  # `pg_temp` último: otro test
    found = migrator.execute(f"SELECT prosecdef FROM pg_proc WHERE oid = {oid}")  # noqa: S608
    assert found.fetchone() == (False,)


SYNC = [*whatsapp.CODES, 132000, 132001, 132999, 131031, 100]


def test_every_rate_limited_code_of_the_table_is_retried_first() -> None:
    limited = {code for code, own in whatsapp.CODES.items() if own == "RATE_LIMITED"}
    assert set(whatsapp.TOO_FAST) == limited == {4, 17, 613, 80007, 130429, 131056}


@pytest.mark.parametrize("code", SYNC)
def test_the_send_answer_and_the_webhook_give_the_same_reason_for_each_code(code: int) -> None:
    raw = b'{"error": {"code": %d}}' % code
    if code in whatsapp.TOO_FAST:  # un límite de tasa: no lo aceptó, primero se reintenta
        with pytest.raises(TemporaryChannelError):
            whatsapp._refused(400, raw, AmbiguousChannelError)
        assert whatsapp.code_for(code) == "RATE_LIMITED"
        return
    assert whatsapp._refused(400, raw, AmbiguousChannelError).reason == whatsapp.code_for(code)


@pytest.mark.parametrize(
    ("outcome", "reason", "account"),
    [("quality", "QUALITY_BLOCKED", "DEGRADED"), ("template", "TEMPLATE_UNAVAILABLE", "CONNECTED")],
)
def test_a_refused_send_gets_the_webhook_reason_and_a_quality_block_degrades_the_account(
    world: SimpleNamespace, graph: Any, migrator: Any, outcome: str, reason: str, account: str
) -> None:
    graph.then(outcome)
    message, saved = send(world, talk(world))
    assert (saved.delivery_status, saved.provider_error_code) == ("FAILED", reason)
    with tenant_scope(ctx(world.a)):
        assert ChannelAccount.objects.get(pk=message.channel_account_id).status == account
    tasks.deliver(Retrying(0), world.a, message.pk)  # ya cerrado: ni reenvía ni repite el aviso
    assert len(graph.requests) == 1
    degraded = [a for a, *_ in audit(migrator) if a == "channel_account.degraded"]
    assert len(degraded) == (account == "DEGRADED")
