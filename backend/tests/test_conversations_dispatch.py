"""F6-04b: la salida de los mensajes en cola (adaptador Sandbox, reintentos, revalidación y
barredor) con PostgreSQL real y el rol `crm_app`; el outbox y la auditoría, con el rol dueño."""

from types import SimpleNamespace
from typing import Any
from uuid import UUID

import psycopg
import pytest
from django.conf import settings
from django.utils import timezone

from apps.conversations import channels, commands, tasks
from apps.conversations.channels import SendResult, TemporaryChannelError
from apps.conversations.models import ChannelAccount, Conversation, Message
from core.outbox import emit, handlers_for
from core.outbox.publisher import publish_outbox
from core.tenancy.scope import tenant_scope
from tests import test_conversations_tables as tables
from tests.test_conversation_commands import (
    SECRET,
    audit,
    inbound,
    outbound,
    reload,
    run,
    world,
    writer,
)
from tests.test_inventory_stock import race
from tests.test_memberships import ctx

__all__ = ["world", "writer"]  # los fixtures, para pytest
pytestmark = pytest.mark.usefixtures("tenant_db")
DB = psycopg.Connection[Any]


class Counting:
    """Un adaptador que cuenta cuántas veces se le pidió enviar."""

    def __init__(self, result: SendResult | None = None, boom: Exception | None = None) -> None:
        self.calls, self.result, self.boom = 0, result, boom

    def send(self, message: Message) -> SendResult:
        self.calls += 1
        if self.boom:
            raise self.boom
        return self.result or channels.SandboxAdapter().send(message)

    def resolve(self, platform: str) -> Counting:
        return self


def queue(world: SimpleNamespace, text: str = SECRET) -> Message:
    """Un mensaje saliente en cola de la organización A, por el comando de F6-04a."""
    convo = reload(world, Conversation, inbound(world)[0].conversation_id)
    sent: Message = outbound(world, convo, body=text)[0]
    return sent


def queue_in_b(world: SimpleNamespace) -> Message:
    convo = tables.conversation(world.b, world.contact_b, world.account_b)
    return tables.message(  # type: ignore[no-any-return]
        world.b, convo, direction="OUTBOUND", sender_type="USER", sender_user_id=world.luis.pk,
        delivery_status="QUEUED", client_message_id=UUID(int=7),
    )  # fmt: skip


def dispatch(world: SimpleNamespace, message: Any, adapter: Counting | None = None) -> Any:
    wanted: dict[str, Any] = {"message_id": message.pk}
    if adapter is not None:
        wanted["resolve"] = adapter.resolve
    return run(world, commands.dispatch_outbound, **wanted)


def state(world: SimpleNamespace, message: Message) -> Message:
    return reload(world, Message, message.pk)  # type: ignore[no-any-return]


def deliveries(migrator: DB) -> list[tuple[Any, Any]]:
    return [(c.get("delivery_status"), m) for a, c, m in audit(migrator) if "delivery" in a]


def test_the_sandbox_adapter_sends_and_delivers_and_a_second_run_does_nothing(
    world: SimpleNamespace, migrator: DB
) -> None:
    message = queue(world)
    done = dispatch(world, message)
    assert done.delivery_status == "DELIVERED"
    saved = state(world, message)
    assert saved.external_message_id == f"sandbox-{message.pk}"  # sintético y estable
    assert saved.sent_at is not None and saved.delivered_at is not None
    assert saved.failed_at is None
    moves = [
        (c["delivery_status"]) for a, c, m in audit(migrator) if a.endswith("delivery_changed")
    ]
    assert moves == [["QUEUED", "SENT"], ["SENT", "DELIVERED"]]
    assert dispatch(world, message) is None  # ya salió: ni se vuelve a enviar ni se audita
    assert len([a for a, _, _ in audit(migrator) if a.endswith("delivery_changed")]) == 2
    assert SECRET not in str(audit(migrator))  # nada del texto en la auditoría


def test_a_message_that_is_not_queued_or_not_outbound_is_left_alone(
    world: SimpleNamespace,
) -> None:
    inbound_message, _ = inbound(world)
    assert dispatch(world, inbound_message) is None  # lo entrante no sale
    assert dispatch(world, SimpleNamespace(pk=UUID(int=3))) is None  # inexistente
    with pytest.raises(ValueError):  # noqa: PT011
        run(world, commands.dispatch_outbound, message_id="no-es-un-id")


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"status": "CLOSED", "closed_at": timezone.now()}, "CONVERSATION_CLOSED"),
        ({"account": "DISCONNECTED"}, "CHANNEL_UNAVAILABLE"),
        ({"account": "ERROR"}, "CHANNEL_UNAVAILABLE"),
        ({"account": "PENDING"}, "CHANNEL_UNAVAILABLE"),
        ({"deleted": True}, "CHANNEL_UNAVAILABLE"),
    ],
)
def test_the_policy_is_revalidated_before_the_adapter_and_a_blocked_message_fails(
    world: SimpleNamespace, migrator: DB, change: dict[str, Any], reason: str
) -> None:
    message = queue(world)
    with tenant_scope(ctx(world.a)):
        if "account" in change:
            ChannelAccount.objects.filter(pk=world.account_a.pk).update(status=change["account"])
        if change.get("deleted"):
            ChannelAccount.objects.filter(pk=world.account_a.pk).update(deleted_at=timezone.now())
        if "status" in change:
            Conversation.objects.filter(pk=message.conversation_id).update(**change)
    adapter = Counting()
    failed = dispatch(world, message, adapter)
    assert failed.delivery_status == "FAILED" and adapter.calls == 0  # el adaptador no se llama
    assert state(world, message).failed_at is not None
    why = [m for a, c, m in audit(migrator) if c.get("delivery_status") == ["QUEUED", "FAILED"]]
    assert why == [{"reason": reason}] and SECRET not in str(audit(migrator))


def test_a_rejection_by_the_adapter_fails_with_a_short_known_reason(
    world: SimpleNamespace, migrator: DB
) -> None:
    first, second = queue(world), queue(world)
    assert (
        dispatch(
            world, first, Counting(SendResult(False, reason="NO_ES_UN_MOTIVO"))
        ).delivery_status
        == "FAILED"
    )
    rejected = dispatch(world, second, Counting(SendResult(False, reason="ADAPTER_REJECTED")))
    assert rejected.delivery_status == "FAILED"
    reasons = [m for _, c, m in audit(migrator) if c.get("delivery_status") == ["QUEUED", "FAILED"]]
    assert reasons == [{"reason": "ADAPTER_REJECTED"}] * 2  # lo desconocido se normaliza


def test_an_unknown_platform_fails_as_an_unavailable_channel(world: SimpleNamespace) -> None:
    message = queue(world)

    def unknown(platform: str) -> Any:
        raise channels.UnknownPlatform(platform)

    done = run(world, commands.dispatch_outbound, message_id=message.pk, resolve=unknown)
    assert done.delivery_status == "FAILED"


def test_a_temporary_failure_writes_nothing_and_lets_the_task_retry(
    world: SimpleNamespace, migrator: DB
) -> None:
    message = queue(world)
    flaky = Counting(boom=TemporaryChannelError())
    with pytest.raises(TemporaryChannelError):
        dispatch(world, message, flaky)
    assert state(world, message).delivery_status == "QUEUED" and flaky.calls == 1
    assert deliveries(migrator) == []  # ni estado ni auditoría
    assert dispatch(world, message, Counting()).delivery_status == "DELIVERED"  # y luego sale


class Retrying:
    """Una tarea de Celery de mentira: cuántos reintentos lleva y qué pidió reintentar."""

    def __init__(self, retries: int) -> None:
        self.request = SimpleNamespace(retries=retries)
        self.asked: list[int] = []

    def retry(self, **kwargs: Any) -> Exception:
        self.asked.append(kwargs["countdown"])
        return RuntimeError("retry")


@pytest.mark.parametrize(("retries", "countdown"), [(0, 30), (1, 60), (3, 240), (4, 480), (6, 600)])
def test_retries_back_off_up_to_a_bound_and_then_the_message_fails(
    world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, retries: int, countdown: int
) -> None:
    message = queue(world)
    monkeypatch.setattr(tasks, "MAX_RETRIES", 10)  # para llegar al tope de la espera
    monkeypatch.setitem(channels.ADAPTERS, "SANDBOX", Counting(boom=TemporaryChannelError()))
    task = Retrying(retries)
    with tenant_scope(ctx(world.a)), pytest.raises(RuntimeError, match="retry"):
        tasks.deliver(task, message.pk)
    assert task.asked == [countdown] and state(world, message).delivery_status == "QUEUED"
    assert tasks.BACKOFF_MAX >= countdown


def test_when_the_retries_run_out_the_message_fails_without_overriding_a_sent_one(
    world: SimpleNamespace, migrator: DB, monkeypatch: pytest.MonkeyPatch
) -> None:
    gone, sent = queue(world), queue(world)
    dispatch(world, sent, Counting(SendResult(True, "x-1")))  # ya salió (SENT), sin entregar
    monkeypatch.setitem(channels.ADAPTERS, "SANDBOX", Counting(boom=TemporaryChannelError()))
    for message in (gone, sent):
        with tenant_scope(ctx(world.a)):
            tasks.deliver(Retrying(tasks.MAX_RETRIES), message.pk)
    assert state(world, gone).delivery_status == "FAILED"
    assert state(world, sent).delivery_status == "SENT"  # lo enviado no se pisa
    assert run(world, commands.fail_queued, message_id=sent.pk, reason="RETRIES_EXHAUSTED") is None
    assert state(world, sent).delivery_status == "SENT"
    assert [
        m for _, c, m in audit(migrator) if c.get("delivery_status") == ["QUEUED", "FAILED"]
    ] == [{"reason": "RETRIES_EXHAUSTED"}]


def test_the_tasks_run_in_the_tenant_of_the_call_and_never_touch_another_organization(
    world: SimpleNamespace,
) -> None:
    mine, theirs = queue(world), queue_in_b(world)
    wrong = tasks.dispatch_message.apply(
        kwargs={"message_id": str(theirs.pk), "organization_id": str(world.a)}
    )
    assert wrong.successful() and state_in(world.b, theirs).delivery_status == "QUEUED"
    right = tasks.dispatch_message.apply(
        kwargs={"message_id": str(mine.pk), "organization_id": str(world.a)}
    )
    assert right.successful() and state(world, mine).delivery_status == "DELIVERED"
    own = tasks.dispatch_message.apply(
        kwargs={"message_id": str(theirs.pk), "organization_id": str(world.b)}
    )
    assert own.successful() and state_in(world.b, theirs).delivery_status == "DELIVERED"


def state_in(org: UUID, message: Message) -> Message:
    with tenant_scope(ctx(org)):
        found: Message = Message.objects.get(pk=message.pk)
        return found


def test_the_outbox_event_reaches_the_dispatcher_end_to_end(
    world: SimpleNamespace, migrator: DB, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert tasks.dispatch_event in handlers_for("message.queued")
    message = queue(world)
    monkeypatch.setattr(
        tasks.dispatch_event, "apply_async", lambda **kw: tasks.dispatch_event.apply(**kw)
    )
    assert publish_outbox() == 1
    assert state(world, message).delivery_status == "DELIVERED"
    assert migrator.execute(
        "SELECT count(*) FROM outbox_events WHERE published_at IS NULL"
    ).fetchone() == (0,)


def test_an_event_of_another_organization_is_not_found_by_the_handler(
    world: SimpleNamespace, migrator: DB
) -> None:
    theirs = queue_in_b(world)
    with tenant_scope(ctx(world.b)) as tenant:
        event = emit(tenant, "message.queued", aggregate_type="message", aggregate_id=theirs.pk)
    done = tasks.dispatch_event.apply(
        kwargs={"event_id": str(event), "organization_id": str(world.a)}  # el evento es de B
    )
    assert done.successful() and state_in(world.b, theirs).delivery_status == "QUEUED"


def test_an_event_of_another_type_does_not_dispatch(world: SimpleNamespace) -> None:
    message = queue(world)
    with tenant_scope(ctx(world.a)) as tenant:
        event = emit(tenant, "message.other", aggregate_type="message", aggregate_id=message.pk)
    kwargs = {"event_id": str(event), "organization_id": str(world.a)}
    assert tasks.dispatch_event.apply(kwargs=kwargs).successful()
    assert state(world, message).delivery_status == "QUEUED"


def test_the_handler_and_the_sweeper_at_once_send_it_once(world: SimpleNamespace) -> None:
    message = queue(world)
    adapter = Counting()

    def go(tenant: Any) -> Any:
        return commands.dispatch_outbound(tenant, message_id=message.pk, resolve=adapter.resolve)

    first, second = race(world.a, world.ana, [go, go])  # el segundo espera el bloqueo del primero
    assert first.delivery_status == "DELIVERED" and second is None and adapter.calls == 1


def backdate(migrator: DB, message: Message, minutes: int) -> None:
    migrator.execute(
        "UPDATE messages SET created_at = now() - make_interval(mins => %s) WHERE id = %s",
        [minutes, message.pk],
    )


def test_the_sweeper_requeues_only_the_old_queued_messages_with_their_own_organization(
    world: SimpleNamespace, migrator: DB, monkeypatch: pytest.MonkeyPatch
) -> None:
    old, fresh, done = queue(world), queue(world), queue(world)
    other = queue_in_b(world)
    dispatch(world, done)
    for message in (old, done, other):
        backdate(migrator, message, 30)
    sent: list[dict[str, str]] = []
    monkeypatch.setattr(tasks.dispatch_message, "apply_async", lambda kwargs: sent.append(kwargs))
    assert tasks.sweep_queued() == 2
    assert sorted(sent, key=lambda k: k["message_id"]) == sorted(
        [
            {"message_id": str(old.pk), "organization_id": str(world.a)},
            {"message_id": str(other.pk), "organization_id": str(world.b)},
        ],
        key=lambda k: k["message_id"],
    )  # sin la reciente ni la ya entregada; cada una con su organización
    assert str(fresh.pk) not in {k["message_id"] for k in sent}
    assert tasks.sweep_queued(batch_size=1) == 2  # un lote de 1 por organización


def test_the_sweeper_lists_organizations_through_a_function_that_only_returns_ids(
    world: SimpleNamespace, migrator: DB
) -> None:
    for message in (queue(world), queue_in_b(world)):
        backdate(migrator, message, 30)
    before = timezone.now()
    with tenant_scope(ctx(world.a)):
        found = tasks.queued_organizations(before, 10)
        assert sorted(found) == sorted([world.a, world.b])
        assert len(tasks.queued_organizations(before, 1)) == 1  # el tope
        for bad in (0, -1, None):
            assert tasks.queued_organizations(before, bad) == []  # type: ignore[arg-type]
    row = migrator.execute(
        "SELECT has_function_privilege('crm_app', 'public.messages_queued_organizations(timestamptz, integer)', 'EXECUTE'), "  # noqa: E501
        "has_function_privilege('public', 'public.messages_queued_organizations(timestamptz, integer)', 'EXECUTE')"  # noqa: E501
    ).fetchone()
    assert row == (True, False)


def test_the_partial_index_and_the_schedule_exist(migrator: DB) -> None:
    index = migrator.execute(
        "SELECT indexdef FROM pg_indexes WHERE indexname = 'messages_queued_idx'"
    ).fetchone()
    assert index is not None and "(organization_id, id)" in index[0]
    assert "delivery_status" in index[0] and "QUEUED" in index[0]  # parcial
    assert settings.CELERY_BEAT_SCHEDULE["conversations.sweep_queued"]["schedule"] == 60.0
    assert settings.INBOX_SWEEP_AFTER_MINUTES == 5 and tasks.sweep_queued.tenancy == "platform"


def test_a_failure_reason_only_goes_with_failed_and_comes_from_the_short_list(
    world: SimpleNamespace,
) -> None:
    message = queue(world)
    for status, reason in (("SENT", "ADAPTER_REJECTED"), ("FAILED", "porque sí")):
        with pytest.raises(ValueError):  # noqa: PT011
            run(
                world,
                commands.advance_delivery,
                message_id=message.pk,
                status=status,
                reason=reason,
            )
    assert state(world, message).delivery_status == "QUEUED"
