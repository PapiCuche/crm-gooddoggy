"""F7-05c: el plazo de los `SENDING`, que no se amplifique la cola y que desconectar espere un
reclamo en curso. PostgreSQL real con el rol `crm_app`."""

import datetime as dt
import threading
from types import SimpleNamespace
from typing import Any

import pytest
from django.conf import settings
from django.utils import timezone

from apps.conversations import accounts, commands, tasks
from apps.conversations.channels import AmbiguousChannelError, TemporaryChannelError
from apps.organizations.models import OrganizationMembership
from core.tenancy.scope import tenant_scope
from tests.test_authorization import give
from tests.test_conversation_commands import audit, outbound, world, writer
from tests.test_conversations_dispatch import (
    Counting,
    Retrying,
    backdate,
    deliveries,
    dispatch,
    queue,
    queue_in_b,
    state,
)
from tests.test_memberships import ctx
from tests.test_whatsapp_outbound import graph, talk

__all__ = ["graph", "world", "writer"]  # los fixtures, para pytest
pytestmark = pytest.mark.usefixtures("tenant_db")
DEADLINE = dt.timedelta(minutes=settings.INBOX_SENDING_DEADLINE_MINUTES)


def hold(migrator: Any, message: Any, minutes: int) -> None:
    """Deja el mensaje `SENDING` desde hace `minutes` (un despachador que no cerró)."""
    migrator.execute(
        "UPDATE messages SET delivery_status = 'SENDING', "
        "sending_at = now() - make_interval(mins => %s) WHERE id = %s",
        [minutes, message.pk],
    )


def test_a_sending_message_fails_exactly_at_the_deadline_and_is_never_resent(
    world: SimpleNamespace, migrator: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    message, lost = queue(world), Counting(boom=AmbiguousChannelError())
    assert dispatch(world, message, lost).delivery_status == "SENDING"  # resultado ambiguo
    start = timezone.now() - dt.timedelta(days=1)
    migrator.execute("UPDATE messages SET sending_at = %s WHERE id = %s", [start, message.pk])

    def sweep(at: dt.datetime) -> int:
        with monkeypatch.context() as patch:
            patch.setattr(timezone, "now", lambda: at)
            return int(tasks.sweep_sending())

    assert sweep(start + DEADLINE - dt.timedelta(microseconds=1)) == 0  # aún no
    assert state(world, message).delivery_status == "SENDING"
    assert sweep(start + DEADLINE) == 1 and sweep(start + DEADLINE) == 0  # al cumplirse, una vez
    assert state(world, message).delivery_status == "FAILED" and lost.calls == 1  # sin reenvío
    why = [m for _, m in deliveries(migrator) if m]
    assert why == [{"reason": "PROVIDER_UNKNOWN"}] and "Hola" not in str(audit(migrator))


def test_the_sweeper_closes_each_organizations_messages_in_its_own_scope_in_batches(
    world: SimpleNamespace, migrator: Any
) -> None:
    mine, more, theirs, recent = queue(world), queue(world), queue_in_b(world), queue(world)
    for message in (mine, more, theirs):
        hold(migrator, message, 60)
    hold(migrator, recent, 1)  # dentro del plazo: no se toca
    before = timezone.now() - DEADLINE
    with tenant_scope(ctx(world.a)):  # la función solo da ids, con tope y solo a `crm_app`
        assert sorted(tasks.sending_organizations(before, 10)) == sorted([world.a, world.b])
        assert len(tasks.sending_organizations(before, 1)) == 1
        for bad in (0, -1, None):
            assert tasks.sending_organizations(before, bad) == []  # type: ignore[arg-type]
    sig = "public.messages_sending_organizations(timestamptz, integer)"
    row = migrator.execute(
        f"SELECT has_function_privilege('crm_app', '{sig}', 'EXECUTE'), "  # noqa: S608
        f"has_function_privilege('public', '{sig}', 'EXECUTE')"
    ).fetchone()
    assert row == (True, False)
    assert tasks.sweep_sending(batch_size=1) == 2  # un mensaje por organización y pasada
    assert tasks.sweep_sending() == 1 and tasks.sweep_sending() == 0
    assert state(world, recent).delivery_status == "SENDING"
    assert {state(world, m).delivery_status for m in (mine, more)} == {"FAILED"}


def test_the_queue_sweeper_waits_for_not_before_and_gives_up_after_too_many_attempts(
    world: SimpleNamespace, migrator: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    ready, waiting, spent = queue(world), queue(world), queue(world)
    for message in (ready, waiting, spent):
        backdate(migrator, message, 30)
    migrator.execute("UPDATE messages SET not_before = now() + interval '10 min' WHERE id = %s",
                     [waiting.pk])  # fmt: skip
    migrator.execute("UPDATE messages SET dispatch_attempts = %s WHERE id = %s",
                     [tasks.MAX_RETRIES + 1, spent.pk])  # fmt: skip
    sent: list[dict[str, str]] = []
    waits: list[int] = []

    def enqueue(kwargs: dict[str, str], countdown: int) -> None:
        sent.append(kwargs)
        waits.append(countdown)

    monkeypatch.setattr(tasks.dispatch_message, "apply_async", enqueue)
    assert tasks.sweep_queued() == 1 and [k["message_id"] for k in sent] == [str(ready.pk)]
    assert state(world, spent).delivery_status == "FAILED"  # ya no se reencola, ni amplifica
    assert state(world, waiting).delivery_status == "QUEUED"
    left = (state(world, waiting).not_before - timezone.now()).total_seconds()  # type: ignore[operator]
    assert left > 500  # su espera no se toca: solo lo que se reencola recibe la nueva
    wait = (state(world, ready).not_before - timezone.now()).total_seconds()  # type: ignore[operator]
    assert 110 < wait <= tasks.REQUEUE_WAIT and waits == [tasks.REQUEUE_WAIT + 1]
    assert tasks.sweep_queued() == 0  # el minuto siguiente no lo reencola otra vez


def test_a_message_still_inside_its_wait_is_not_claimed_by_a_redelivered_event(
    world: SimpleNamespace, migrator: Any
) -> None:
    message, adapter = queue(world), Counting()
    migrator.execute("UPDATE messages SET not_before = now() + interval '1 min'")
    assert dispatch(world, message, adapter).delivery_status == "QUEUED" and adapter.calls == 0
    migrator.execute("UPDATE messages SET not_before = now() - interval '1 sec'")
    assert dispatch(world, message, adapter).delivery_status == "DELIVERED" and adapter.calls == 1


def test_a_returned_claim_counts_the_attempt_and_waits_and_the_attempts_outlive_the_task(
    world: SimpleNamespace, migrator: Any
) -> None:
    message, flaky = queue(world), Counting(boom=TemporaryChannelError())
    with pytest.raises(RuntimeError, match="retry"):
        tasks.deliver(Retrying(0), world.a, message.pk, flaky.resolve)
    saved = state(world, message)
    wait = (saved.not_before - timezone.now()).total_seconds()  # type: ignore[operator]
    assert saved.dispatch_attempts == 1 and 25 < wait <= tasks.BACKOFF_SECONDS
    migrator.execute("UPDATE messages SET not_before = NULL")  # pasada su espera
    with tenant_scope(ctx(world.a)) as tenant:  # varias cadenas: el contador es del mensaje
        for _ in range(tasks.MAX_RETRIES - 1):
            commands.claim_outbound(tenant, message_id=message.pk)
            commands.release_outbound(tenant, message_id=message.pk, wait=dt.timedelta(0))
    tasks.deliver(
        Retrying(0), world.a, message.pk, flaky.resolve
    )  # una cadena nueva, sin reintentos
    assert state(world, message).delivery_status == "FAILED"


def test_disconnecting_an_account_waits_for_the_claim_in_flight(
    world: SimpleNamespace, graph: Any
) -> None:
    convo = talk(world)
    message, _ = outbound(world, convo)
    with tenant_scope(ctx(world.a)):
        mine = OrganizationMembership.objects.get(user_id=world.ana.pk).pk
    give(world.a, mine, {"channels.manage": None})
    claimed, release, done = threading.Event(), threading.Event(), threading.Event()
    seen: list[str] = []

    def claim() -> None:
        with tenant_scope(ctx(world.a, world.ana)) as tenant:
            seen.append(str(commands.claim_outbound(tenant, message_id=message.pk) is not None))
            claimed.set()
            release.wait(timeout=10)  # el reclamo sigue abierto: la cuenta, con `FOR SHARE`

    def disconnect() -> None:
        with tenant_scope(ctx(world.a, world.ana)) as tenant:
            accounts.disconnect(tenant, channel_account_id=convo.channel_account_id)
        done.set()

    holder = threading.Thread(target=claim)
    holder.start()
    claimed.wait(timeout=10)
    closer = threading.Thread(target=disconnect)
    closer.start()
    assert not done.wait(timeout=1)  # desconectar espera al reclamo
    release.set()
    holder.join(timeout=10)
    closer.join(timeout=10)
    assert seen == ["True"] and done.is_set()
