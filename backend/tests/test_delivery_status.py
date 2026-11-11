"""F7-06a: los estados de entrega de WhatsApp (`apps.conversations.delivery`, ADR-029 §8). De la
entrega firmada del simulador de Meta al webhook, la ingesta y el procesador, y de ahí al mensaje
saliente que dejó el adaptador. PostgreSQL real con `crm_app`."""

import datetime as dt
import time
from types import SimpleNamespace
from typing import Any

import pytest
from django.utils import timezone

from apps.conversations import delivery, ingress, tasks
from apps.conversations.models import ChannelAccount, Message
from core.tenancy.scope import tenant_scope
from tests import test_conversations_tables as tables
from tests.meta_sim import webhooks
from tests.meta_sim.webhooks import PHONE_NUMBER_ID, WABA_ID
from tests.test_conversation_commands import audit, outbound, world, writer
from tests.test_conversations_dispatch import deliveries, state
from tests.test_memberships import ctx
from tests.test_meta_webhook import channel, post
from tests.test_webhook_ingress import rows
from tests.test_whatsapp_outbound import graph, redis, send, talk

__all__ = ["channel", "graph", "redis", "world", "writer"]  # los fixtures, para pytest
pytestmark = pytest.mark.usefixtures("tenant_db", "channel")
WAMID = "wamid.SIMOUT000001"  # el que el simulador da al primer envío
AT = 1_700_000_100


@pytest.fixture(autouse=True)
def fresh_wake(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ingress, "_last_wake", 0.0)


def run(*statuses: dict[str, Any]) -> dict[str, int]:
    """Entrega firmada de estados a la cuenta de la prueba y procesa la ingesta."""
    value = webhooks.value(statuses=list(statuses), phone_number_id=PHONE_NUMBER_ID)
    post(webhooks.deliver(webhooks.encode(webhooks.envelope(value, waba_id=WABA_ID))))
    return ingress.process()


def sent(world: SimpleNamespace, graph: Any) -> Message:
    """Un mensaje que salió por el adaptador (`SENT`, `wamid.SIMOUT000001`)."""
    message, saved = send(world, talk(world))
    assert saved.external_message_id == WAMID and saved.delivery_status == "SENT"
    return message  # type: ignore[no-any-return]


def stamp(world: SimpleNamespace, message: Message) -> Message:
    return state(world, message)


def test_the_four_states_advance_the_message_with_the_provider_time(
    world: SimpleNamespace, graph: Any, migrator: Any
) -> None:
    message = sent(world, graph)
    assert run(webhooks.status(WAMID, "delivered", at=AT)) == {"PROCESSED": 1}
    saved = stamp(world, message)
    assert saved.delivered_at is not None
    assert saved.delivery_status == "DELIVERED" and saved.delivered_at.timestamp() == AT
    assert run(webhooks.status(WAMID, "read", at=AT + 5)) == {"PROCESSED": 1}
    assert stamp(world, message).delivery_status == "READ"
    assert [c for c, _ in deliveries(migrator)][-2:] == [
        ["SENT", "DELIVERED"],
        ["DELIVERED", "READ"],
    ]
    assert audit(migrator)  # y todo con ids: ni texto ni teléfono


def test_a_late_or_repeated_state_never_goes_back_and_failed_is_terminal(
    world: SimpleNamespace, graph: Any, migrator: Any
) -> None:
    message = sent(world, graph)
    run(webhooks.status(WAMID, "delivered", at=AT))
    failed = run(webhooks.status(WAMID, "failed", error=(131026, "x")))  # tras entregar: nada
    assert failed == {"PROCESSED": 1}  # no es un error: se ignora
    saved = stamp(world, message)
    assert (saved.delivery_status, saved.provider_error_code) == ("DELIVERED", None)
    run(webhooks.status(WAMID, "read", at=AT + 5), webhooks.status(WAMID, "sent", at=AT - 9))
    assert stamp(world, message).delivery_status == "READ"  # lo tardío no retrocede
    moves = [c for c, _ in deliveries(migrator)]
    assert moves[-2:] == [["SENT", "DELIVERED"], ["DELIVERED", "READ"]]  # y nada más cambió


@pytest.mark.parametrize(
    ("meta", "code"),
    [(190, "TOKEN_INVALID"), (131047, "WINDOW_CLOSED"), (131026, "RECIPIENT_UNREACHABLE"),
     (130429, "RATE_LIMITED"), (131056, "RATE_LIMITED"), (4, "RATE_LIMITED"), (17, "RATE_LIMITED"),
     (613, "RATE_LIMITED"), (80007, "RATE_LIMITED"), (131048, "QUALITY_BLOCKED"),
     (132001, "TEMPLATE_UNAVAILABLE"), (131000, "PROVIDER_ERROR"), (999999, "PROVIDER_REJECTED")],
)  # fmt: skip
def test_each_meta_error_becomes_its_own_closed_code_and_never_its_text(
    world: SimpleNamespace, graph: Any, migrator: Any, caplog: pytest.LogCaptureFixture,
    meta: int, code: str,
) -> None:  # fmt: skip
    message = sent(world, graph)
    run(webhooks.status(WAMID, "failed", error=(meta, "TITULO-SECRETO-DE-META")))
    saved = stamp(world, message)
    assert (saved.delivery_status, saved.provider_error_code) == ("FAILED", code)
    assert saved.failed_at is not None
    everything = str(audit(migrator)) + caplog.text + str(rows(migrator))
    assert (
        "TITULO-SECRETO" not in everything
    )  # ni en la auditoría, ni en los registros, ni en filas
    assert str([m for _, m in deliveries(migrator) if m]) == str([{"reason": code}])


def test_a_failed_status_after_the_send_sets_the_reason_even_for_a_policy_failure(
    world: SimpleNamespace, graph: Any
) -> None:
    graph.then("window_closed")  # el despachador también deja su motivo en el mensaje
    _, saved = send(world, talk(world))
    assert (saved.delivery_status, saved.provider_error_code) == ("FAILED", "WINDOW_CLOSED")


@pytest.mark.parametrize("outcome", ["cut_after", "timeout_after"])
def test_a_sending_message_is_reconciled_by_the_opaque_data_with_the_first_status(
    world: SimpleNamespace, graph: Any, outcome: str
) -> None:
    graph.then(outcome)  # Meta lo aceptó pero la respuesta se perdió: `SENDING` sin `wamid`
    convo = talk(world)
    message, saved = send(world, convo)
    assert saved.delivery_status == "SENDING" and saved.external_message_id is None
    assert run(webhooks.status(WAMID, "delivered", opaque=str(message.pk), at=AT)) == {
        "PROCESSED": 1
    }
    saved = stamp(world, message)
    assert (saved.delivery_status, saved.external_message_id) == ("DELIVERED", WAMID)
    assert saved.delivered_at is not None and saved.failed_at is None


def test_the_opaque_data_only_reconciles_a_sending_message_without_wamid(
    world: SimpleNamespace, graph: Any
) -> None:
    message = sent(world, graph)  # `SENT` y con su `wamid`: otro id con su dato opaco no lo toca
    assert run(webhooks.status("wamid.OTRO", "read", opaque=str(message.pk))) == {"FAILED": 1}
    saved = stamp(world, message)
    assert (saved.delivery_status, saved.external_message_id) == ("SENT", WAMID)


def test_a_failed_status_closes_a_sending_message_with_its_code(
    world: SimpleNamespace, graph: Any
) -> None:
    graph.then("cut_after")
    message, _ = send(world, talk(world))
    run(webhooks.status(WAMID, "failed", opaque=str(message.pk), error=(131047, "x")))
    saved = stamp(world, message)
    assert (saved.delivery_status, saved.provider_error_code) == ("FAILED", "WINDOW_CLOSED")


def test_a_status_before_the_send_confirmation_is_retried_and_then_applies(
    world: SimpleNamespace, graph: Any, migrator: Any
) -> None:
    graph.then("cut_after")
    message, _ = send(world, talk(world))  # `SENDING`, todavía sin `wamid`
    assert run(webhooks.status(WAMID, "delivered")) == {"FAILED": 1}  # sin id opaco: no se conoce
    failure = migrator.execute("SELECT reason FROM webhook_failures").fetchone()
    assert failure == ("MESSAGE_NOT_FOUND",) and stamp(world, message).delivery_status == "SENDING"
    migrator.execute(  # el envío confirma su id (lo hace el despachador)
        "UPDATE messages SET external_message_id = %s, delivery_status = 'SENT' WHERE id = %s",
        [WAMID, message.pk],
    )
    migrator.execute("UPDATE webhook_ingress SET next_attempt_at = now()")
    assert ingress.process() == {"PROCESSED": 1}
    assert stamp(world, message).delivery_status == "DELIVERED"


def test_a_status_for_a_message_the_crm_never_sent_is_closed_as_ignored_with_its_payload_cleared(
    world: SimpleNamespace, graph: Any, migrator: Any, caplog: pytest.LogCaptureFixture
) -> None:
    sent(world, graph)  # otro mensaje del mismo número, enviado desde la app del teléfono
    outcomes = []
    for _ in range(ingress.UNKNOWN_AFTER):
        migrator.execute("UPDATE webhook_ingress SET next_attempt_at = now()")
        outcomes.append(run(webhooks.status("wamid.DEL-TELEFONO", "read")))
    assert outcomes == [
        {"FAILED": 1},
        {"FAILED": 1},
        {"IGNORED": 1},
    ]  # solo la carrera se reintenta
    row = migrator.execute("SELECT status, ignored_reason, payload FROM webhook_ingress").fetchone()
    assert row == ("IGNORED", "UNKNOWN_MESSAGE", None)  # sin el teléfono del cliente (recipient_id)
    assert "wamid.DEL-TELEFONO" not in caplog.text and webhooks.CUSTOMER not in caplog.text


def test_a_status_for_a_message_of_another_organization_or_account_does_not_exist(
    world: SimpleNamespace, graph: Any
) -> None:
    mine = sent(world, graph)
    other_org = tables.message(  # un saliente de B con el `wamid` que nombra el estado
        world.b, tables.conversation(world.b, world.contact_b, world.account_b),
        direction="OUTBOUND", sender_type="USER", sender_user_id=world.luis.pk,
        delivery_status="SENT", external_message_id="wamid.OTHER-ORG",
    )  # fmt: skip
    assert run(webhooks.status("wamid.OTHER-ORG", "read")) == {"FAILED": 1}  # no existe para A
    with tenant_scope(ctx(world.b)):
        assert Message.objects.get(pk=other_org.pk).delivery_status == "SENT"
    with tenant_scope(ctx(world.a)):  # otra cuenta de la misma organización
        second = ChannelAccount.objects.create(
            platform="SANDBOX", name="S", external_account_id="otra-cuenta"
        )
    other_account = tables.message(
        world.a, tables.conversation(world.a, world.contact_a, second, "CONV-08001"),
        direction="OUTBOUND", sender_type="USER", sender_user_id=world.ana.pk,
        delivery_status="SENT", external_message_id="wamid.OTHER-ACCOUNT",
    )  # fmt: skip
    assert run(webhooks.status("wamid.OTHER-ACCOUNT", "read")) == {"FAILED": 1}
    assert stamp(world, other_account).delivery_status == "SENT"
    assert stamp(world, mine).delivery_status == "SENT"


def test_a_refused_token_or_a_quality_block_changes_the_account_once_and_says_so(
    world: SimpleNamespace, graph: Any, migrator: Any
) -> None:
    convo = talk(world)
    first, second, third = (send(world, convo)[0] for _ in range(3))
    run(webhooks.status(WAMID, "failed", error=(131048, "x")))  # calidad: DEGRADED
    run(webhooks.status("wamid.SIMOUT000002", "failed", error=(190, "x")))  # token: ERROR
    run(webhooks.status("wamid.SIMOUT000003", "failed", error=(131048, "x")))  # ya en ERROR
    with tenant_scope(ctx(world.a)):
        account = ChannelAccount.objects.get(pk=convo.channel_account_id)
    assert (account.status, account.status_detail) == ("ERROR", "TOKEN_INVALID")
    actions = [a for a, *_ in audit(migrator) if a.startswith("channel_account.")]
    assert actions == ["channel_account.degraded", "channel_account.token_invalid"]
    assert {stamp(world, m).delivery_status for m in (first, second, third)} == {"FAILED"}


@pytest.mark.parametrize("status", [{"status": "deleted"}, {"id": "con espacio"}])
def test_a_status_that_is_not_a_delivery_state_is_ignored_with_a_closed_reason(
    world: SimpleNamespace, graph: Any, migrator: Any, status: dict[str, Any]
) -> None:
    message = sent(world, graph)
    run(webhooks.status(WAMID, "delivered") | status)
    stored = migrator.execute("SELECT status, ignored_reason FROM webhook_ingress").fetchone()
    assert stored == ("IGNORED", "BAD_STATUS") and stamp(world, message).delivery_status == "SENT"


FUTURE = str(int(time.time()) + 86_400)  # mañana: creíble, pero nunca del futuro


@pytest.mark.parametrize("raw", [FUTURE, "99999999999", "abc", "1"])
def test_the_provider_time_is_never_from_the_future_and_a_bad_one_is_now(
    world: SimpleNamespace, graph: Any, raw: str
) -> None:
    message = sent(world, graph)
    before = timezone.now()
    run(webhooks.status(WAMID, "delivered") | {"timestamp": raw})
    saved = stamp(world, message)
    assert saved.delivered_at is not None
    assert before - dt.timedelta(seconds=1) <= saved.delivered_at <= timezone.now()


def test_a_change_leaves_a_realtime_hint_with_only_ids(
    world: SimpleNamespace, graph: Any, migrator: Any
) -> None:
    message = sent(world, graph)
    migrator.execute("DELETE FROM outbox_events")
    run(webhooks.status(WAMID, "delivered"))
    event = migrator.execute(
        "SELECT event_type, aggregate_id, payload::text FROM outbox_events"
    ).fetchone()
    assert event[:2] == ("message.updated", message.pk) and "conversation_id" in event[2]
    migrator.execute("DELETE FROM outbox_events")
    run(webhooks.status(WAMID, "sent", at=AT - 9))  # atrasado: no cambia, no avisa
    assert migrator.execute("SELECT count(*) FROM outbox_events").fetchone() == (0,)


def test_the_error_code_only_exists_on_a_failed_message_and_from_the_closed_set(
    world: SimpleNamespace, graph: Any, migrator: Any
) -> None:
    message = sent(world, graph)
    for bad in ("TITULO LIBRE", "WINDOW_CLOSED"):  # fuera del conjunto, o con un mensaje no fallido
        with pytest.raises(Exception, match="messages_error_code_ck"):
            migrator.execute("UPDATE messages SET provider_error_code = %s WHERE id = %s",
                             [bad, message.pk])  # fmt: skip
        migrator.rollback()


def test_a_late_status_reconciles_a_message_the_deadline_closed_as_unknown_and_nothing_else(
    world: SimpleNamespace, graph: Any, migrator: Any
) -> None:
    graph.then("cut_after")
    convo = talk(world)
    unknown, _ = send(world, convo)  # Meta lo aceptó y la respuesta se perdió
    migrator.execute("UPDATE messages SET sending_at = now() - interval '1 hour'")
    assert tasks.sweep_sending() == 1
    closed = stamp(world, unknown)
    assert (closed.delivery_status, closed.provider_error_code) == ("FAILED", "PROVIDER_UNKNOWN")
    run(webhooks.status(WAMID, "delivered", opaque=str(unknown.pk), at=AT))  # sí había salido
    saved = stamp(world, unknown)
    assert (saved.delivery_status, saved.provider_error_code) == ("DELIVERED", None)
    assert saved.failed_at is None and saved.external_message_id == WAMID
    moved = [m for _, m in deliveries(migrator) if m]
    assert moved[-1] == {"reconciled": "PROVIDER_UNKNOWN"}
    other, _ = send(world, convo)  # un `FAILED` con otro motivo sigue terminal
    run(webhooks.status("wamid.SIMOUT000002", "failed", error=(131047, "x")))
    assert run(webhooks.status("wamid.SIMOUT000002", "delivered")) == {"PROCESSED": 1}
    assert stamp(world, other).delivery_status == "FAILED"


def test_the_trigger_lets_only_an_unknown_failure_become_delivered_and_never_go_back(
    world: SimpleNamespace, graph: Any, migrator: Any
) -> None:
    message = sent(world, graph)
    update = "UPDATE messages SET delivery_status = %s, provider_error_code = %s WHERE id = %s"
    migrator.execute(update, ["FAILED", "PROVIDER_UNKNOWN", message.pk])
    with pytest.raises(Exception, match="solo avanza"):  # ni a la cola ni a `FAILED` otra vez
        migrator.execute(update, ["QUEUED", None, message.pk])
    migrator.rollback()
    migrator.execute(update, ["READ", None, message.pk])  # sí a un estado de entrega
    with pytest.raises(Exception, match="solo avanza"):  # y desde ahí, `FAILED` ya no
        migrator.execute(update, ["FAILED", "WINDOW_CLOSED", message.pk])
    migrator.rollback()


def test_the_opaque_data_never_reconciles_a_queued_or_a_definitively_failed_message(
    world: SimpleNamespace, graph: Any, migrator: Any
) -> None:
    """Solo un `SENDING` (o el que cerró el plazo) sin `wamid`: un `delivered` no puede dar por
    entregado lo que nunca salió."""
    convo = talk(world)
    queued, _ = outbound(world, convo)
    refused, _ = outbound(world, convo)
    migrator.execute(
        "UPDATE messages SET delivery_status = 'FAILED', provider_error_code = 'RETRIES_EXHAUSTED' "
        "WHERE id = %s", [refused.pk])  # fmt: skip
    for n, message in enumerate((queued, refused)):
        status = webhooks.status(f"wamid.NEVER{n}", "delivered", opaque=str(message.pk))
        assert run(status) == {"FAILED": 1}
        migrator.execute("UPDATE webhook_ingress SET status = 'PROCESSED', payload = NULL")
    assert stamp(world, queued).delivery_status == "QUEUED"
    assert stamp(world, refused).provider_error_code == "RETRIES_EXHAUSTED"
    assert stamp(world, queued).external_message_id is None


def test_a_failed_status_after_the_deadline_leaves_the_unknown_reason_alone(
    world: SimpleNamespace, graph: Any, migrator: Any
) -> None:
    graph.then("cut_after")
    unknown, _ = send(world, talk(world))
    migrator.execute("UPDATE messages SET sending_at = now() - interval '1 hour'")
    assert tasks.sweep_sending() == 1
    assert run(webhooks.status(WAMID, "failed", opaque=str(unknown.pk), error=(131047, "x"))) == {
        "PROCESSED": 1
    }  # ni error ni cambio: el único camino fuera de `FAILED` es hacia lo entregado
    saved = stamp(world, unknown)
    assert (saved.delivery_status, saved.provider_error_code) == ("FAILED", "PROVIDER_UNKNOWN")


@pytest.mark.parametrize(
    ("code", "own"),
    [(132000, "TEMPLATE_UNAVAILABLE"), (132999, "TEMPLATE_UNAVAILABLE"),
     (133000, "PROVIDER_REJECTED"), (131999, "PROVIDER_REJECTED"), (True, "PROVIDER_REJECTED"),
     ("190", "PROVIDER_REJECTED"), (None, "PROVIDER_REJECTED"), (2, "PROVIDER_ERROR")],
)  # fmt: skip
def test_the_error_table_edges_and_a_code_that_is_not_a_number(code: Any, own: str) -> None:
    assert delivery.code_for(code) == own
