"""F7-05a: la política de mensajería (ADR-010). `evaluate` sin base de datos (la ventana en su
borde exacto, cada motivo y su orden) y, con PostgreSQL real, lo que la usa: encolar guarda la
decisión y el despachador la revalúa justo antes de enviar."""

import datetime as dt
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from django.core.management import call_command
from django.db import DatabaseError
from django.utils import timezone

from apps.contacts.models import Contact
from apps.conversations import policy, selectors
from apps.conversations.models import ChannelAccount, Conversation, Message
from core.tenancy.scope import tenant_scope
from tests import test_conversations_tables as tables
from tests.test_conversation_commands import audit, outbound, reload, world, writer
from tests.test_conversations_dispatch import Counting, dispatch, state
from tests.test_memberships import ctx
from tests.test_pipelines_tables import as_migrator

__all__ = ["world", "writer"]  # los fixtures, para pytest
pytestmark = pytest.mark.usefixtures("tenant_db")
NOW = dt.datetime(2026, 11, 10, 15, 0, tzinfo=dt.UTC)
HOUR = dt.timedelta(hours=1)


def decide(**over: Any) -> policy.PolicyDecision:
    facts: dict[str, Any] = {
        "conversation_status": "OPEN", "platform": "WHATSAPP", "account_status": "CONNECTED",
        "account_deleted": False, "contact_blocked": False, "last_inbound_at": NOW - HOUR,
        "now": NOW,
    }  # fmt: skip
    return policy.evaluate(**(facts | over))


def test_the_window_closes_at_exactly_24_hours() -> None:
    edge = NOW - 24 * HOUR
    just_inside = decide(last_inbound_at=edge + dt.timedelta(microseconds=1))
    assert just_inside.allowed and just_inside.reason is None and just_inside.mode == "FREE_FORM"
    at_the_edge = decide(last_inbound_at=edge)  # a las 24 h exactas ya está cerrada
    assert (at_the_edge.allowed, at_the_edge.reason, at_the_edge.mode) == (
        False, "WINDOW_CLOSED", "BLOCKED",
    )  # fmt: skip
    assert decide(last_inbound_at=edge - dt.timedelta(microseconds=1)).reason == "WINDOW_CLOSED"
    assert decide(last_inbound_at=NOW).allowed  # el cliente acaba de escribir
    assert at_the_edge.window_expires_at == NOW and at_the_edge.window_hours == 24
    never = decide(last_inbound_at=None)  # sin mensaje del cliente, la ventana nunca se abrió
    assert (never.reason, never.window_expires_at) == ("WINDOW_CLOSED", None)


def test_the_sandbox_has_no_window() -> None:
    for inbound in (None, NOW - 1000 * HOUR):
        decision = decide(platform="SANDBOX", last_inbound_at=inbound)
        assert decision.allowed and (decision.window_hours, decision.window_expires_at) == (
            None, None,
        )  # fmt: skip
    assert policy.CAPABILITIES.get("WHATSAPP") == policy.ChannelCapabilities(window_hours=24)
    assert policy.CAPABILITIES.get("SANDBOX") == policy.ChannelCapabilities(window_hours=None)
    assert policy.CAPABILITIES.get("INSTAGRAM") is None


@pytest.mark.parametrize("status", ["DISCONNECTED", "ERROR", "PENDING"])
def test_a_channel_that_is_not_connected_denies(status: str) -> None:
    assert decide(account_status=status).reason == "CHANNEL_UNAVAILABLE"
    assert decide(account_status=status, platform="SANDBOX").reason == "CHANNEL_UNAVAILABLE"


def test_a_deleted_account_or_a_platform_without_adapter_denies_and_a_degraded_one_does_not() -> (
    None
):
    assert decide(account_deleted=True).reason == "CHANNEL_UNAVAILABLE"
    assert decide(platform="INSTAGRAM").reason == "CHANNEL_UNAVAILABLE"
    assert decide(account_status="DEGRADED").allowed


def test_a_blocked_contact_never_gets_a_message_even_inside_the_window() -> None:
    assert decide(contact_blocked=True).reason == "CONTACT_BLOCKED"
    assert decide(contact_blocked=True, platform="SANDBOX", last_inbound_at=None).reason == (
        "CONTACT_BLOCKED"
    )


def test_an_internal_note_is_never_sendable_whatever_else_is_true() -> None:
    for platform in ("WHATSAPP", "SANDBOX"):
        note = decide(direction="INTERNAL", platform=platform)
        assert (note.allowed, note.reason) == (False, "INTERNAL_NOTE_NOT_SENDABLE")
    assert decide(direction="OUTBOUND").allowed


def test_a_closed_conversation_denies_and_a_resolved_one_does_not() -> None:
    assert decide(conversation_status="CLOSED").reason == "CONVERSATION_CLOSED"
    assert decide(conversation_status="RESOLVED").allowed  # D-F6-11


def test_the_reasons_come_in_a_fixed_order() -> None:
    worst: dict[str, Any] = {
        "direction": "INTERNAL", "conversation_status": "CLOSED", "account_status": "ERROR",
        "contact_blocked": True, "last_inbound_at": None,
    }  # fmt: skip
    seen = []
    for drop in ("direction", "conversation_status", "account_status", "contact_blocked"):
        seen.append(decide(**worst).reason)
        del worst[drop]
    seen.append(decide(**worst).reason)
    assert seen == list(policy.REASONS)  # y `last_inbound_at=None` queda al final: la ventana


def test_the_stored_snapshot_has_only_the_decision_and_no_customer_text() -> None:
    snap = decide(last_inbound_at=NOW - 25 * HOUR).as_json()
    assert snap == {
        "allowed": False, "mode": "BLOCKED", "reason": "WINDOW_CLOSED",
        "window_expires_at": (NOW - HOUR).isoformat(), "evaluated_at": NOW.isoformat(),
    }  # fmt: skip


# ---- con la base de datos ------------------------------------------------------------------


def whatsapp(org: Any, number: str = "15550007001") -> ChannelAccount:
    """Una cuenta de WhatsApp que puede enviar (`DEGRADED`: la base no deja `CONNECTED` sin
    credencial, y `DEGRADED` envía)."""
    with tenant_scope(ctx(org)):
        made: ChannelAccount = ChannelAccount.objects.create(
            platform="WHATSAPP", name="WA", status="DEGRADED", external_account_id=number,
            external_parent_id="1234567890",
        )  # fmt: skip
    return made


def talk(world: SimpleNamespace, *, ago: dt.timedelta, **over: Any) -> Conversation:
    """Una conversación de WhatsApp en la que el cliente escribió hace `ago`."""
    stamp = None if ago is None else timezone.now() - ago
    convo: Conversation = tables.fresh(
        world.a, whatsapp(world.a), "CONV-07001", last_inbound_at=stamp, **over
    )
    return convo


def saved_decision(world: SimpleNamespace, message: Message) -> dict[str, Any]:
    found = state(world, message).policy_decision
    assert found is not None
    return found  # type: ignore[no-any-return]


def block(world: SimpleNamespace, convo: Conversation, blocked: bool = True) -> None:
    with tenant_scope(ctx(world.a)):
        Contact.objects.filter(pk=convo.contact_id).update(is_blocked=blocked)


def test_queueing_saves_the_decision_in_the_message(world: SimpleNamespace) -> None:
    convo = talk(world, ago=2 * HOUR)
    message, _ = outbound(world, convo, body="Hola")
    saved = reload(world, Message, message.pk).policy_decision
    assert saved["allowed"] is True and saved["mode"] == "FREE_FORM" and saved["reason"] is None
    expires = dt.datetime.fromisoformat(saved["window_expires_at"])
    assert expires == convo.last_inbound_at + 24 * HOUR  # type: ignore[operator]
    assert set(saved) == {"allowed", "mode", "reason", "window_expires_at", "evaluated_at"}
    assert "Hola" not in str(saved)


@pytest.mark.parametrize(
    ("ago", "blocked", "error"),
    [(24 * HOUR, False, policy.WindowClosed), (HOUR, True, policy.ContactBlocked)],
)
def test_queueing_outside_the_window_or_to_a_blocked_contact_is_refused_and_writes_nothing(
    world: SimpleNamespace, ago: dt.timedelta, blocked: bool, error: type[Exception]
) -> None:
    convo = talk(world, ago=ago)
    block(world, convo, blocked)
    with pytest.raises(error):
        outbound(world, convo)
    with tenant_scope(ctx(world.a)):
        assert not Message.objects.exists()


@pytest.mark.parametrize(
    ("change", "reason"),
    [("window", "WINDOW_CLOSED"), ("blocked", "CONTACT_BLOCKED"),
     ("account", "CHANNEL_UNAVAILABLE")],
)  # fmt: skip
def test_the_dispatcher_rechecks_the_policy_right_before_sending(
    world: SimpleNamespace, migrator: Any, change: str, reason: str
) -> None:
    convo = talk(world, ago=23 * HOUR)
    message, _ = outbound(world, convo)  # permitido al encolar; mientras esperaba, cambió algo
    with tenant_scope(ctx(world.a)):
        if change == "window":
            Conversation.objects.filter(pk=convo.pk).update(
                last_inbound_at=timezone.now() - 24 * HOUR
            )
        elif change == "blocked":
            Contact.objects.filter(pk=convo.contact_id).update(is_blocked=True)
        else:
            ChannelAccount.objects.filter(pk=convo.channel_account_id).update(status="ERROR")
    adapter = Counting()
    done = dispatch(world, message, adapter)
    assert done.delivery_status == "FAILED" and adapter.calls == 0  # nunca llegó al canal
    saved = saved_decision(world, message)  # la última decisión queda en el mensaje
    assert (saved["allowed"], saved["reason"]) == (False, reason)
    failed = [m for a, _, m in audit(migrator) if a.endswith("delivery_changed")]
    assert failed[-1].get("reason") == reason


def test_the_dispatcher_sends_inside_the_window_and_stores_the_fresh_decision(
    world: SimpleNamespace,
) -> None:
    convo = talk(world, ago=HOUR)
    message, _ = outbound(world, convo)
    before = saved_decision(world, message)["evaluated_at"]
    adapter = Counting()
    assert dispatch(world, message, adapter).delivery_status == "DELIVERED"
    saved = saved_decision(world, message)
    assert adapter.calls == 1 and saved["allowed"] is True
    assert dt.datetime.fromisoformat(saved["evaluated_at"]) >= dt.datetime.fromisoformat(before)


def test_the_decision_reads_only_the_organizations_own_rows_and_fails_closed(
    world: SimpleNamespace,
) -> None:
    convo = talk(world, ago=HOUR)
    with tenant_scope(ctx(world.b)):  # otra organización: ni la cuenta ni el contacto se ven
        stranger = selectors.messaging_decision(convo)
    assert (stranger.allowed, stranger.reason) == (False, "CHANNEL_UNAVAILABLE")
    with tenant_scope(ctx(world.a)):
        assert selectors.messaging_decision(convo).allowed
        # una conversación puede ser de un contacto que ya no se ve: cerrado ante la duda
        gone = Conversation(
            contact_id=uuid4(), channel_account_id=convo.channel_account_id, status="OPEN",
            last_inbound_at=convo.last_inbound_at,
        )  # fmt: skip
        assert selectors.messaging_decision(gone).reason == "CONTACT_BLOCKED"


def test_only_an_outbound_message_carries_a_decision(world: SimpleNamespace) -> None:
    convo = tables.first(world)
    saved = {"allowed": True}
    with pytest.raises(DatabaseError, match="messages_policy_ck"):
        tables.message(world.a, convo, policy_decision=saved)  # entrante: la base lo impide
    out = {"direction": "OUTBOUND", "sender_type": "USER", "sender_user_id": world.ana.pk,
           "delivery_status": "QUEUED"}  # fmt: skip
    assert tables.message(world.a, convo, policy_decision=saved, **out).policy_decision == saved


def test_the_migrations_go_back_and_forward(world: SimpleNamespace, migrator: Any) -> None:
    column = (
        "SELECT count(*) FROM information_schema.columns WHERE table_name = %s AND column_name = %s"
    )
    grant = "SELECT has_column_privilege('crm_app', 'messages', 'policy_decision', 'UPDATE')"
    assert migrator.execute(grant).fetchone() == (True,)  # la despachadora la reescribe
    with as_migrator():
        call_command("migrate", "conversations", "0009", verbosity=0)
        call_command("migrate", "contacts", "0006", verbosity=0)
    assert migrator.execute(column, ["messages", "policy_decision"]).fetchone() == (0,)
    assert migrator.execute(column, ["contacts", "is_blocked"]).fetchone() == (0,)
    with as_migrator():
        call_command("migrate", verbosity=0)
    assert migrator.execute(column, ["messages", "policy_decision"]).fetchone() == (1,)
    assert migrator.execute(column, ["contacts", "is_blocked"]).fetchone() == (1,)
    assert migrator.execute(grant).fetchone() == (True,)
    blocked = "SELECT is_blocked FROM contacts WHERE id = %s"  # nace sin bloquear
    assert migrator.execute(blocked, [world.contact_a.pk]).fetchone() == (False,)
