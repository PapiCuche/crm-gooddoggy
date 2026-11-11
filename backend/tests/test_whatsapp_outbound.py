"""F7-05b: el envío por WhatsApp contra la Graph API falsa (`tests/meta_sim`): reclamo en `SENDING`,
llamada sin transacción, resultados ambiguos que no se reenvían, token y tenant. PostgreSQL real con
el rol `crm_app`; el simulador sustituye solo la capa de red de `core.http`."""

import dataclasses
import datetime as dt
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import Any, cast

import pytest
import urllib3
from django.core.management import call_command
from django.db import DatabaseError, connection
from django.utils import timezone

from apps.contacts.models import Contact
from apps.conversations import accounts, channels, commands, credentials, tasks, whatsapp
from apps.conversations.channels import AmbiguousChannelError, TemporaryChannelError
from apps.conversations.models import TOKEN_PURPOSE, ChannelAccount, Conversation, Credential
from apps.conversations.policy import ChannelUnavailable
from core import http, sealed
from core.ids import new_id
from core.tenancy.scope import tenant_scope
from tests import test_conversations_tables as tables
from tests.meta_sim import GRAPH_VERSION, transport
from tests.meta_sim.graph import TEST_TOKEN, FakeGraph
from tests.meta_sim.webhooks import PHONE_NUMBER_ID, WABA_ID
from tests.test_conversation_commands import audit, outbound, world, writer
from tests.test_conversations_dispatch import Retrying, deliveries, state
from tests.test_memberships import ctx
from tests.test_pipelines_tables import as_migrator
from tests.test_whatsapp_account import credential as tables_credential

__all__ = ["world", "writer"]  # los fixtures, para pytest
pytestmark = pytest.mark.usefixtures("tenant_db")
PHONE = "+51900000111"


@pytest.fixture
def graph(monkeypatch: pytest.MonkeyPatch, settings: Any) -> FakeGraph:
    settings.META_ENABLED, settings.META_GRAPH_VERSION = True, GRAPH_VERSION
    settings.HTTP_ALLOWED_HOSTS = [transport.GRAPH_HOST]
    fake = FakeGraph()
    transport.install(monkeypatch, fake)
    return fake


def talk(world: SimpleNamespace) -> Conversation:
    """Una conversación de WhatsApp conectada, a la que el cliente escribió hace una hora."""
    mine = new_id()
    with tenant_scope(ctx(world.a)) as tenant:
        stored = credentials.store(tenant, mine, TEST_TOKEN)
        made = ChannelAccount.objects.create(
            id=mine, platform="WHATSAPP", name="WA", status="CONNECTED", credential_id=stored.pk,
            external_account_id=PHONE_NUMBER_ID, external_parent_id=WABA_ID,
            last_healthcheck_at=timezone.now(),
        )  # fmt: skip
        Contact.objects.filter(pk=world.contact_a.pk).update(primary_phone_e164=PHONE)
    return tables.conversation(  # type: ignore[no-any-return]
        world.a, world.contact_a, made, last_inbound_at=timezone.now() - dt.timedelta(hours=1)
    )


def send(world: SimpleNamespace, convo: Conversation) -> Any:
    message, _ = outbound(world, convo, body="Hola, ¿en qué te ayudo?")
    tasks.deliver(Retrying(0), world.a, message.pk)
    return message, state(world, message)


def test_a_reply_inside_the_window_goes_out_with_its_token_and_callback_data(
    world: SimpleNamespace, graph: FakeGraph, migrator: Any, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("DEBUG")
    message, saved = send(world, talk(world))
    assert (saved.delivery_status, saved.external_message_id) == ("SENT", "wamid.SIMOUT000001")
    sent, call = graph.sent[0], graph.calls[0]
    assert (sent["to"], sent["type"], sent["messaging_product"]) == (
        "51900000111",
        "text",
        "whatsapp",
    )
    assert sent["biz_opaque_callback_data"] == str(message.pk) and sent["text"]["body"].startswith(
        "Hola"
    )
    assert call["path"] == f"/{GRAPH_VERSION}/{PHONE_NUMBER_ID}/messages"
    assert (
        call["headers"]["Authorization"] == f"Bearer {TEST_TOKEN}"
    )  # en la cabecera, no en la URL
    assert saved.sending_at is not None
    assert [c for c, _ in deliveries(migrator)] == [["QUEUED", "SENDING"], ["SENDING", "SENT"]]
    everything = str(audit(migrator)) + caplog.text + str(call["path"])
    assert TEST_TOKEN not in everything and "Hola, ¿en" not in everything  # ni token ni texto


REFUSALS = {
    "window_closed": "WINDOW_CLOSED", "unreachable": "RECIPIENT_UNREACHABLE",
    "quality": "PROVIDER_REJECTED",
}  # fmt: skip


@pytest.mark.parametrize("outcome", list(REFUSALS))
def test_a_refusal_of_meta_is_a_closed_reason_and_never_its_text(
    world: SimpleNamespace, graph: FakeGraph, migrator: Any, outcome: str
) -> None:
    graph.then(outcome)
    _, saved = send(world, talk(world))
    assert saved.delivery_status == "FAILED" and graph.sent == []
    why = [m for _, m in deliveries(migrator) if m]
    assert why == [{"reason": REFUSALS[outcome]}] and "Re-engagement" not in str(audit(migrator))


@pytest.mark.parametrize("outcome", ["cut", "timeout", "cut_after", "timeout_after", "server"])
def test_an_ambiguous_result_is_never_resent_and_the_message_stays_sending(
    world: SimpleNamespace, graph: FakeGraph, outcome: str
) -> None:
    graph.then(outcome)
    message, saved = send(world, talk(world))
    assert saved.delivery_status == "SENDING"
    tasks.deliver(Retrying(0), world.a, message.pk)  # otro despachador, el barredor o un reintento
    assert state(world, message).delivery_status == "SENDING"
    assert len(graph.requests) == 1  # Meta recibió una sola petición: no se reenvió a ciegas
    assert len(graph.sent) == (1 if outcome.endswith("_after") else 0)  # lo aceptado, solo una vez


def test_a_refused_token_puts_the_account_in_error_and_stops_the_queue(
    world: SimpleNamespace, graph: FakeGraph, migrator: Any
) -> None:
    convo = talk(world)
    first, second = outbound(world, convo)[0], outbound(world, convo)[0]
    graph.then("token")
    tasks.deliver(Retrying(0), world.a, first.pk)
    with tenant_scope(ctx(world.a)):
        found = ChannelAccount.objects.get(pk=convo.channel_account_id)
    assert (state(world, first).delivery_status, found.status) == ("FAILED", "ERROR")
    assert found.status_detail == "TOKEN_INVALID"
    tasks.deliver(Retrying(0), world.a, second.pk)  # lo que seguía en cola falla por la política
    assert state(world, second).delivery_status == "FAILED" and len(graph.requests) == 1
    with pytest.raises(ChannelUnavailable):
        outbound(world, convo)  # y no se encola más
    assert [a for a, *_ in audit(migrator) if a.endswith("token_invalid")] == [
        "channel_account.token_invalid"
    ]
    with tenant_scope(ctx(world.a)) as tenant:  # solo una vez, y solo una cuenta viva
        assert not accounts.mark_token_invalid(tenant, channel_account_id=found.pk)


def test_nothing_is_sent_with_a_credential_of_another_account_or_to_a_host_off_the_allowlist(
    world: SimpleNamespace, graph: FakeGraph, settings: Any
) -> None:
    convo = talk(world)
    with tenant_scope(ctx(world.a)):
        mine = ChannelAccount.objects.get(pk=convo.channel_account_id).credential_id
        theirs = tables_credential(world.a)  # una credencial viva que es de otra cuenta
        ChannelAccount.objects.filter(pk=convo.channel_account_id).update(credential_id=theirs)
    assert send(world, convo)[1].delivery_status == "FAILED" and graph.requests == []
    with tenant_scope(ctx(world.a)):
        ChannelAccount.objects.filter(pk=convo.channel_account_id).update(credential_id=mine)
    settings.HTTP_ALLOWED_HOSTS = []  # fuera de la allowlist: ni se conecta, y se reintenta
    message, _ = outbound(world, convo)
    with pytest.raises(RuntimeError, match="retry"):
        tasks.deliver(Retrying(0), world.a, message.pk)
    assert graph.requests == [] and state(world, message).delivery_status == "QUEUED"


def test_the_token_is_opened_only_during_the_call_and_never_stored_in_the_claim(
    world: SimpleNamespace, graph: FakeGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[str] = []
    real = credentials.open_token
    monkeypatch.setattr(
        credentials,
        "open_token",
        lambda c: opened.append("x") or real(c),  # type: ignore[func-returns-value]
    )
    convo = talk(world)
    message, _ = outbound(world, convo)
    with tenant_scope(ctx(world.a)) as tenant:
        claim = commands.claim_outbound(tenant, message_id=message.pk)
    assert claim is not None and opened == []  # reclamar no abre el token
    assert TEST_TOKEN not in repr(claim) and "Hola" not in repr(claim)  # ni el texto
    assert channels.adapter_for("WHATSAPP").send(claim).ok and opened == ["x"]


def test_two_dispatchers_at_once_send_to_meta_once(
    world: SimpleNamespace, graph: FakeGraph
) -> None:
    message, _ = outbound(world, talk(world))
    start = threading.Barrier(2)

    def go(_: int) -> None:
        try:
            start.wait(timeout=10)
            tasks.deliver(Retrying(0), world.a, message.pk)
        finally:
            connection.close()

    with ThreadPoolExecutor(2) as pool:
        list(pool.map(go, range(2)))
    assert len(graph.sent) == 1 and state(world, message).delivery_status == "SENT"


def test_a_failure_before_the_request_is_safe_to_repeat_and_one_after_it_is_not() -> None:
    err, pool = urllib3.exceptions, cast("Any", None)
    assert http.never_connected(http.BlockedDestination("x"))
    assert http.never_connected(err.NewConnectionError(pool, "x"))
    slow = err.ReadTimeoutError(pool, "u", "x")  # el tiempo de lectura es ambiguo, también envuelto
    assert not http.never_connected(slow) and not http.never_connected(err.ProtocolError("x"))
    assert not http.never_connected(err.MaxRetryError(pool, "u", reason=slow))
    retries = http._manager.connection_pool_kw["retries"]
    assert "POST" not in retries.allowed_methods  # una lectura fallida de un POST no se repite


MOVES = [("QUEUED", "SENDING", True), ("SENDING", "QUEUED", True), ("SENDING", "SENT", True),
         ("SENDING", "READ", True), ("SENDING", "FAILED", True), ("SENT", "QUEUED", False),
         ("SENT", "SENDING", False), ("DELIVERED", "SENDING", False)]  # fmt: skip
OUT = {"direction": "OUTBOUND", "sender_type": "USER"}


@pytest.mark.parametrize(("old", "new", "allowed"), MOVES)
def test_the_database_lets_a_claim_be_returned_and_nothing_else_go_back(
    world: SimpleNamespace, old: str, new: str, allowed: bool
) -> None:
    made = tables.message(
        world.a, tables.first(world), delivery_status=old, sender_user_id=world.ana.pk, **OUT
    )
    update = "UPDATE messages SET delivery_status = %s WHERE id = %s"
    if allowed:
        tables.sql(world.a, update, [new, made.pk])
    else:
        with pytest.raises(DatabaseError, match="solo avanza"):
            tables.sql(world.a, update, [new, made.pk])


ANSWERS = [  # (estado HTTP, cuerpo, qué pasa)
    (200, b"{}", AmbiguousChannelError), (200, b'{"messages":[{"id":" "}]}', AmbiguousChannelError),
    (400, b'{"error": {"code": 190}}', "TOKEN_INVALID"), (401, b"", "TOKEN_INVALID"),
    (400, b'{"error": {"code": true}}', "PROVIDER_REJECTED"), (429, b"", TemporaryChannelError),
    (400, b'{"error": {"code": 130429}}', TemporaryChannelError), (503, b"", AmbiguousChannelError),
]  # fmt: skip


@pytest.mark.parametrize(("status", "raw", "outcome"), ANSWERS)
def test_every_shape_of_answer_is_a_closed_outcome_and_an_unclear_success_is_never_resent(
    monkeypatch: pytest.MonkeyPatch, settings: Any, status: int, raw: bytes, outcome: Any
) -> None:
    settings.META_ENABLED = True
    monkeypatch.setattr(http, "request", lambda *a, **k: SimpleNamespace(status=status, data=raw))
    mine = new_id()
    box = sealed.seal(TOKEN_PURPOSE, credentials.owner(mine), TEST_TOKEN.encode())
    claim = channels.Claim(new_id(), mine, "WHATSAPP", PHONE_NUMBER_ID, PHONE, "x",
                           Credential(owner_id=mine, ciphertext=box))  # fmt: skip
    nobody = dataclasses.replace(claim, recipient="")  # un contacto sin teléfono: no hay a quién
    assert whatsapp.WhatsAppAdapter().send(nobody).reason == "RECIPIENT_UNREACHABLE"
    copied = dataclasses.replace(claim, credential=Credential(owner_id=new_id(), ciphertext=box))
    assert whatsapp.WhatsAppAdapter().send(copied).reason == "TOKEN_INVALID"  # no se abre
    if isinstance(outcome, str):
        assert whatsapp.WhatsAppAdapter().send(claim) == channels.SendResult(False, reason=outcome)
    else:
        with pytest.raises(outcome):
            whatsapp.WhatsAppAdapter().send(claim)


def test_only_a_sending_message_can_be_settled_or_returned(world: SimpleNamespace) -> None:
    queued = tables.message(
        world.a, tables.first(world), delivery_status="QUEUED", sender_user_id=world.ana.pk, **OUT
    )
    with tenant_scope(ctx(world.a)) as tenant:
        ok = channels.SendResult(True, "x")
        assert commands.settle_outbound(tenant, message_id=queued.pk, result=ok) is None
        assert not commands.release_outbound(tenant, message_id=queued.pk)


def test_the_migration_goes_back_failing_what_was_sending_and_forward_again(
    world: SimpleNamespace, migrator: Any
) -> None:
    held = tables.message(
        world.a, tables.first(world), delivery_status="SENDING", sender_user_id=world.ana.pk, **OUT
    )
    column = "SELECT count(*) FROM information_schema.columns WHERE column_name = 'sending_at'"
    with as_migrator():
        call_command("migrate", "conversations", "0012", verbosity=0)
    assert migrator.execute(column).fetchone() == (0,)
    status = "SELECT delivery_status FROM messages WHERE id = %s"  # el modelo ya no coincide
    assert migrator.execute(status, [held.pk]).fetchone() == ("FAILED",)  # ningún estado ajeno
    with as_migrator():
        call_command("migrate", verbosity=0)
    grant = "SELECT has_column_privilege('crm_app', 'messages', 'sending_at', 'UPDATE')"
    assert migrator.execute(column).fetchone() == (1,)
    assert migrator.execute(grant).fetchone() == (True,)
