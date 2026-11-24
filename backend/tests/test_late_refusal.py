"""F7b-18: un rechazo síncrono de Meta que llega cuando el plazo ya cerró el envío con
`PROVIDER_UNKNOWN` lo corrige por la rama de F7b-15 (una vez, auditado, con aviso y con el gancho de
F7b-14), y los fallos pasajeros de Meta (`2`, `131000`, `131016`) se reintentan sin reenviar nunca
lo que Meta aceptó. PostgreSQL real con `crm_app`; el simulador sustituye solo la red."""

import threading
import time
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from django.db import connection

from apps.conversations import attention, channels, commands, tasks, whatsapp
from apps.conversations.channels import AmbiguousChannelError, SendResult, TemporaryChannelError
from apps.conversations.models import ChannelAccount, Conversation, Message
from core import http
from core.tenancy.scope import tenant_scope
from tests.conftest import migrator_conninfo
from tests.meta_sim import webhooks
from tests.test_conversation_commands import audit, outbound, world, writer
from tests.test_conversations_dispatch import Retrying, state
from tests.test_conversations_failed_reply import chat, fresh_wake, look, move, reopened
from tests.test_delivery_correction import corrections, failed
from tests.test_delivery_status import run
from tests.test_memberships import ctx
from tests.test_meta_webhook import channel
from tests.test_whatsapp_outbound import graph, redis, talk

__all__ = ["channel", "chat", "fresh_wake", "graph", "redis", "world", "writer"]  # los fixtures
pytestmark = pytest.mark.usefixtures("tenant_db", "channel")
OK = b'{"messages": [{"id": "wamid.LATE000001"}]}'
# Los bloqueos que esperan; `pg_locks` muestra los de todos los roles (`pg_stat_activity` no le
# muestra al migrador la espera de las sesiones de `crm_app`).
WAITING = "SELECT count(*) FROM pg_locks WHERE NOT granted"


def wait_for(migrator: Any, waiting: int) -> None:
    """Hasta que `waiting` sesiones esperan un bloqueo; si no llega, la prueba falla."""
    for _ in range(400):
        if migrator.execute(WAITING).fetchone()[0] >= waiting:
            return
        time.sleep(0.05)
    pytest.fail(f"nunca esperaron {waiting} bloqueos")


def error(code: int) -> bytes:
    return b'{"error": {"code": %d, "message": "texto de Meta", "fbtrace_id": "T"}}' % code


class Late:
    """Un envío al que el barredor adelanta: mientras el adaptador espera a Meta, el plazo vence y
    el mensaje queda `FAILED` con `PROVIDER_UNKNOWN`; después llega la respuesta (`answer`)."""

    def __init__(self, migrator: Any, answer: Callable[[channels.Claim], SendResult]) -> None:
        self.migrator, self.answer = migrator, answer

    def send(self, claim: channels.Claim) -> SendResult:
        self.migrator.execute(
            "UPDATE messages SET sending_at = now() - interval '1 hour' WHERE id = %s",
            [claim.message_id],
        )
        assert tasks.sweep_sending() == 1
        return self.answer(claim)


def late(world: SimpleNamespace, migrator: Any, message: Message, answer: Any) -> Retrying:
    task = Retrying(0)
    tasks.deliver(task, world.a, message.pk, resolve=lambda _: Late(migrator, answer))
    return task


def hooks(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Cada llamada al gancho de F7b-14 (`attention.reply_failed`), con el mensaje."""
    seen: list[Any] = []
    real = attention.reply_failed

    def spy(ctx: Any, alias: str, convo: Conversation, message_id: Any) -> None:
        seen.append(message_id)
        real(ctx, alias, convo, message_id)

    monkeypatch.setattr(attention, "reply_failed", spy)
    return seen


def updates(migrator: Any, message: Message) -> int:
    found = migrator.execute(
        "SELECT count(*) FROM outbox_events WHERE event_type = 'message.updated' "
        "AND aggregate_id = %s",
        [message.pk],
    )
    return int(found.fetchone()[0])


def account_audits(migrator: Any) -> list[str]:
    return [a for a, *_ in audit(migrator) if a.startswith("channel_account.")]


@pytest.mark.parametrize(
    ("outcome", "reason", "detail", "account"),
    [("quality", "QUALITY_BLOCKED", "131048", "DEGRADED"),
     ("token", "TOKEN_INVALID", "190", "ERROR"),
     ("window_closed", "WINDOW_CLOSED", "131047", "CONNECTED")],
)  # fmt: skip
def test_a_late_refusal_corrects_the_unknown_once_with_audit_event_hook_and_account(
    world: SimpleNamespace, chat: Conversation, graph: Any, migrator: Any,
    monkeypatch: pytest.MonkeyPatch, outcome: str, reason: str, detail: str, account: str,
) -> None:  # fmt: skip
    asked, seen = look(world, chat)[0], hooks(monkeypatch)
    graph.then(outcome)
    message, _ = outbound(world, chat)
    adapter = whatsapp.WhatsAppAdapter()
    task = late(world, migrator, message, adapter.send)  # Meta lo rechaza tras el plazo
    saved = state(world, message)
    assert (saved.delivery_status, saved.provider_error_code) == ("FAILED", reason)
    assert saved.provider_error_detail == detail and task.asked == []
    assert corrections(migrator) == [{"reason": reason, "corrected": "PROVIDER_UNKNOWN"}]
    assert updates(migrator, message) == 3  # SENDING, el plazo y la corrección
    assert seen == [message.pk] and look(world, chat) == (asked, True, True)
    assert len(reopened(migrator)) == 1
    with tenant_scope(ctx(world.a)):
        assert ChannelAccount.objects.get(pk=chat.channel_account_id).status == account
    assert len(account_audits(migrator)) == (account != "CONNECTED")
    with tenant_scope(ctx(world.a)) as tenant:  # repetido: nada más cambia
        again = SendResult(False, reason="PROVIDER_REJECTED", detail="100")
        assert commands.settle_outbound(tenant, message_id=message.pk, result=again) is None
    tasks.deliver(Retrying(0), world.a, message.pk)  # ni se reenvía
    assert state(world, message).provider_error_code == reason and len(graph.requests) == 1
    assert len(corrections(migrator)) == 1 and seen == [message.pk]
    assert updates(migrator, message) == 3 and len(reopened(migrator)) == 1


def test_a_late_refusal_after_the_webhook_corrected_it_changes_nothing_more(
    world: SimpleNamespace, chat: Conversation, graph: Any, migrator: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    seen = hooks(monkeypatch)
    graph.then("quality")
    message, _ = outbound(world, chat)

    def webhook_first(claim: channels.Claim) -> SendResult:
        assert run(failed(message, 131048)) == {"PROCESSED": 1}  # F7b-15 corrige primero
        return whatsapp.WhatsAppAdapter().send(claim)

    late(world, migrator, message, webhook_first)
    assert state(world, message).provider_error_code == "QUALITY_BLOCKED"
    assert len(corrections(migrator)) == 1 and seen == [message.pk]
    assert account_audits(migrator) == ["channel_account.degraded"]  # la cuenta, una vez


@pytest.mark.parametrize(
    ("outcome", "status", "action"),
    [("quality", "DEGRADED", "channel_account.degraded"),
     ("token", "ERROR", "channel_account.token_invalid")],
)  # fmt: skip
def test_a_late_refusal_over_a_message_already_settled_still_changes_the_account_once(
    world: SimpleNamespace, chat: Conversation, graph: Any, migrator: Any,
    outcome: str, status: str, action: str,
) -> None:  # fmt: skip
    graph.then(outcome)
    message, _ = outbound(world, chat)

    def webhook_first(claim: channels.Claim) -> SendResult:  # otro motivo, sin tocar la cuenta
        assert run(failed(message, 131047)) == {"PROCESSED": 1}
        return whatsapp.WhatsAppAdapter().send(claim)

    late(world, migrator, message, webhook_first)  # `settle_outbound` ya no cambia nada: `None`
    assert state(world, message).provider_error_code == "WINDOW_CLOSED"  # el primero se queda
    with tenant_scope(ctx(world.a)):
        assert ChannelAccount.objects.get(pk=chat.channel_account_id).status == status
    assert account_audits(migrator) == [action]  # lo que Meta dijo de la cuenta, una vez
    tasks.deliver(Retrying(0), world.a, message.pk)  # ni se reenvía ni se repite
    assert account_audits(migrator) == [action] and len(graph.requests) == 1


def test_a_refusal_after_meta_reported_the_message_sent_never_fails_it(
    world: SimpleNamespace, chat: Conversation, graph: Any, migrator: Any
) -> None:
    message, _ = outbound(world, chat)

    class Reported:  # el webhook `sent` (por el id opaco) gana a la respuesta, que dice otra cosa
        def send(self, claim: channels.Claim) -> SendResult:
            sent = webhooks.status("wamid.SENT01", "sent", opaque=str(claim.message_id))
            assert run(sent) == {"PROCESSED": 1}
            return SendResult(False, reason="WINDOW_CLOSED", detail="131047")

    tasks.deliver(Retrying(0), world.a, message.pk, resolve=lambda _: Reported())
    saved = state(world, message)
    assert (saved.delivery_status, saved.provider_error_code) == ("SENT", None)
    assert corrections(migrator) == [] and reopened(migrator) == []


def test_a_late_acceptance_waits_for_the_webhook_to_reconcile_the_unknown(
    world: SimpleNamespace, chat: Conversation, graph: Any, migrator: Any
) -> None:
    message, _ = outbound(world, chat)
    late(world, migrator, message, lambda _: SendResult(True, "wamid.LATEOK01"))
    saved = state(world, message)
    assert (saved.delivery_status, saved.provider_error_code) == ("FAILED", "PROVIDER_UNKNOWN")
    assert saved.external_message_id is None and corrections(migrator) == []
    sent = webhooks.status("wamid.LATEOK01", "sent", opaque=str(message.pk))
    assert run(sent) == {"PROCESSED": 1}  # el camino de siempre (ADR-029 §6)
    saved = state(world, message)
    assert (saved.delivery_status, saved.external_message_id) == ("SENT", "wamid.LATEOK01")


@pytest.mark.parametrize("detail", ["2", "131000", "131016/2494010", None])
def test_a_late_transient_refusal_corrects_the_unknown_and_is_never_requeued(
    world: SimpleNamespace, chat: Conversation, graph: Any, migrator: Any,
    monkeypatch: pytest.MonkeyPatch, detail: str | None,
) -> None:  # fmt: skip
    asked, seen = look(world, chat)[0], hooks(monkeypatch)
    message, _ = outbound(world, chat)

    def refused(_: channels.Claim) -> SendResult:
        raise TemporaryChannelError(detail)  # Meta no lo aceptó (o ni se llegó a llamar)

    task = late(world, migrator, message, refused)
    saved = state(world, message)
    assert (saved.delivery_status, saved.provider_error_code) == ("FAILED", "RETRIES_EXHAUSTED")
    assert (saved.provider_error_detail, saved.dispatch_attempts, task.asked) == (detail, 0, [])
    assert corrections(migrator) == [
        {"reason": "RETRIES_EXHAUSTED", "corrected": "PROVIDER_UNKNOWN"}
    ]
    assert seen == [message.pk] and look(world, chat) == (asked, True, True)
    tasks.deliver(Retrying(1), world.a, message.pk)  # un reintento viejo: no hay nada que reclamar
    assert graph.requests == [] and state(world, message).delivery_status == "FAILED"


def answers(monkeypatch: pytest.MonkeyPatch, *replies: tuple[int, bytes]) -> list[Any]:
    """Meta contesta, por orden, cada `(estado HTTP, cuerpo)`; devuelve lo que se le envió."""
    queue, calls = list(replies), []

    def fake(*args: Any, **kwargs: Any) -> Any:
        calls.append(kwargs.get("json"))
        status, raw = queue.pop(0)
        return SimpleNamespace(status=status, data=raw)

    monkeypatch.setattr(http, "request", fake)
    return calls


@pytest.mark.parametrize("code", [2, 131000, 131016])
def test_a_transient_meta_failure_is_retried_and_goes_out_once(
    world: SimpleNamespace, graph: Any, migrator: Any, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    calls = answers(monkeypatch, (400, error(code)), (200, OK))
    message, _ = outbound(world, talk(world))
    task = Retrying(0)
    with pytest.raises(RuntimeError, match="retry"):
        tasks.deliver(task, world.a, message.pk)
    saved = state(world, message)
    assert (saved.delivery_status, saved.dispatch_attempts, task.asked) == ("QUEUED", 1, [30])
    migrator.execute("UPDATE messages SET not_before = NULL")  # pasó la espera
    tasks.deliver(Retrying(1), world.a, message.pk)
    tasks.deliver(Retrying(2), world.a, message.pk)  # un evento repetido: ya salió
    saved = state(world, message)
    assert (saved.delivery_status, saved.external_message_id) == ("SENT", "wamid.LATE000001")
    assert len(calls) == 2 and calls[0] == calls[1]  # el mismo mensaje, una vez aceptado


@pytest.mark.parametrize("code", [2, 131000, 131016])
def test_a_transient_meta_failure_that_never_ends_fails_with_its_code(
    world: SimpleNamespace, graph: Any, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    calls = answers(monkeypatch, (400, error(code)))
    message, _ = outbound(world, talk(world))
    tasks.deliver(Retrying(tasks.MAX_RETRIES), world.a, message.pk)  # el último intento
    saved = state(world, message)
    assert (saved.provider_error_code, saved.provider_error_detail) == (
        "RETRIES_EXHAUSTED", str(code)
    )  # fmt: skip
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("status", "code", "outcome"),
    [(400, 1, "PROVIDER_ERROR"),  # «Invalid request or possible server error»: definitivo
     *[(400, c, TemporaryChannelError) for c in (2, 131000, 131016)],
     *[(s, c, AmbiguousChannelError) for s in (500, 503) for c in (1, 2, 131000, 131016)]],
)  # fmt: skip
def test_only_a_transient_code_meta_refused_without_a_5xx_is_retried(
    status: int, code: int, outcome: Any
) -> None:
    raw = error(code)
    if isinstance(outcome, str):
        result = whatsapp._refused(status, raw, AmbiguousChannelError)
        assert (result.reason, result.detail) == (outcome, str(code))
        return
    with pytest.raises(outcome) as caught:  # un 5xx pudo haber salido: nunca se reenvía
        whatsapp._refused(status, raw, AmbiguousChannelError)
    if outcome is TemporaryChannelError:
        assert (caught.value.detail, caught.value.args) == (str(code), ())


@pytest.mark.parametrize("code", [2, 131000, 131016])
def test_a_transient_code_meta_sends_after_accepting_it_is_a_definitive_failure(
    world: SimpleNamespace, graph: Any, migrator: Any, code: int
) -> None:
    message, _ = outbound(world, talk(world))
    tasks.deliver(Retrying(0), world.a, message.pk)  # Meta lo acepta (`SENT`)
    status = failed(message, code)
    status.pop("biz_opaque_callback_data", None)
    assert run(status) == {"PROCESSED": 1}  # y después el webhook dice que falló
    saved = state(world, message)
    assert (saved.delivery_status, saved.provider_error_code) == ("FAILED", "PROVIDER_ERROR")
    tasks.deliver(Retrying(0), world.a, message.pk)  # lo aceptado no se reenvía
    assert len(graph.requests) == 1 and state(world, message).dispatch_attempts == 0


@pytest.mark.parametrize("first", ["sweeper", "refusal"])
def test_the_sweeper_and_a_late_refusal_at_once_end_with_the_refusal_and_one_hook(
    world: SimpleNamespace, chat: Conversation, migrator: Any,
    monkeypatch: pytest.MonkeyPatch, first: str,
) -> None:  # fmt: skip
    seen = hooks(monkeypatch)
    message, _ = outbound(world, chat)
    move(world, message, "SENDING")
    old = "UPDATE messages SET sending_at = now() - interval '1 hour' WHERE id = %s"
    migrator.execute(old, [message.pk])
    done: dict[str, Any] = {}
    refusal = SendResult(False, reason="WINDOW_CLOSED", detail="131047")

    def sweeper() -> None:
        try:
            done["sweeper"] = tasks.sweep_sending()
        finally:
            connection.close()

    def refuse() -> None:
        try:
            with tenant_scope(ctx(world.a)) as tenant:
                found = commands.settle_outbound(tenant, message_id=message.pk, result=refusal)
                done["refusal"] = found is not None
        finally:
            connection.close()

    order = {"sweeper": sweeper, "refusal": refuse}
    second = "refusal" if first == "sweeper" else "sweeper"
    threads = [threading.Thread(target=order[name]) for name in (first, second)]
    with psycopg.connect(migrator_conninfo()) as held:  # la fila bloqueada: los dos esperan
        held.execute("SELECT 1 FROM messages WHERE id = %s FOR UPDATE", [message.pk])
        for waiting, thread in enumerate(threads, start=1):  # en este orden
            thread.start()
            wait_for(migrator, waiting)
    for thread in threads:
        thread.join(timeout=10)
    saved = state(world, message)
    assert (saved.delivery_status, saved.provider_error_code) == ("FAILED", "WINDOW_CLOSED")
    assert done == {"sweeper": int(first == "sweeper"), "refusal": True}
    assert len(corrections(migrator)) == int(first == "sweeper")  # solo si el plazo cerró antes
    assert seen == [message.pk] and len(reopened(migrator)) == 1
