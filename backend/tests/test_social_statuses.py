"""F7b-05c: los ecos, las entregas y los leídos de Instagram y Messenger (ADR-035 §5–§6), de punta a
punta contra el simulador: el envío lleva su ficha en `message.metadata`, el eco de Meta la devuelve
(`FakePageGraph.echo`) y la ruta pública lo enruta a `delivery.social`. PostgreSQL real con
`crm_app`; los webhooks firmados por la ruta, con el cliente de pruebas de Django."""

import datetime as dt
import re
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from django.db import IntegrityError
from django.utils import timezone

from apps.contacts.models import Contact
from apps.conversations import commands, ingress, tasks
from apps.conversations.channels import SendResult
from apps.conversations.models import Message
from core.tenancy.scope import tenant_scope
from tests import test_conversations_tables as tables
from tests.meta_sim import social
from tests.meta_sim.page_graph import FakePageGraph
from tests.meta_sim.social import APP_ID, INSTAGRAM, MESSENGER, OTHER_APP_ID
from tests.test_conversation_commands import outbound, world, writer
from tests.test_conversations_dispatch import Retrying, state
from tests.test_memberships import ctx
from tests.test_meta_webhook import channel, post
from tests.test_social_outbound import account_of, graph, send, talk
from tests.test_social_schema import identity
from tests.test_whatsapp_outbound import redis

__all__ = ["channel", "graph", "redis", "world", "writer"]  # los fixtures, para pytest
pytestmark = pytest.mark.usefixtures("tenant_db")
DB = psycopg.Connection[Any]
AT = dt.datetime.fromtimestamp(social.AT / 1000, dt.UTC)  # el instante de las marcas de agua


@pytest.fixture(autouse=True)
def our_app(settings: Any) -> None:
    settings.META_APP_ID = APP_ID


def hook(net: social.Channel, *events: dict[str, Any]) -> dict[str, int]:
    assert post(social.signed(net, *events)).status_code == 200
    return ingress.process()


def items(migrator: DB) -> list[tuple[str, str | None, bool]]:
    return migrator.execute(
        "SELECT status, ignored_reason, payload IS NULL FROM webhook_ingress ORDER BY received_at"
    ).fetchall()


def lost(world: SimpleNamespace, graph: FakePageGraph, platform: str) -> Message:
    """Un envío que Meta aceptó y cuya respuesta se perdió: `SENDING`, sin `mid` (§6)."""
    graph.then("timeout_after")
    message, saved = send(world, talk(world, platform))
    assert (saved.delivery_status, saved.external_message_id) == ("SENDING", None)
    return message  # type: ignore[no-any-return]


def test_each_social_send_carries_its_own_random_token_and_a_sandbox_send_none(
    world: SimpleNamespace, graph: FakePageGraph
) -> None:
    convo = talk(world, "MESSENGER")
    first, second = (send(world, convo)[1] for _ in range(2))
    assert re.fullmatch(r"[0-9a-f]{32}", first.echo_token) and first.echo_token != second.echo_token
    assert [s["message"]["metadata"] for s in graph.sent] == [first.echo_token, second.echo_token]
    sandbox = tables.conversation(world.a, world.contact_a, world.account_a, "CONV-09999")
    message, _ = outbound(world, sandbox)
    tasks.deliver(Retrying(0), world.a, message.pk)
    assert state(world, message).echo_token is None


def test_the_database_keeps_one_token_per_account_and_only_on_outbound_messages(
    world: SimpleNamespace, graph: FakePageGraph
) -> None:
    convo = talk(world, "MESSENGER")
    token = send(world, convo)[1].echo_token
    sandbox = tables.conversation(world.a, world.contact_a, world.account_a, "CONV-09998")
    out = {"direction": "OUTBOUND", "sender_type": "USER", "sender_user_id": world.ana.pk,
           "delivery_status": "QUEUED"}  # fmt: skip
    tables.message(world.a, sandbox, echo_token=token, **out)  # otra cuenta: vale
    for target, fields, constraint in (
        (convo, out | {"echo_token": token}, "messages_echo_token_uq"),  # la misma cuenta
        (sandbox, out | {"echo_token": "A" * 32}, "messages_echo_token_ck"),  # no es `token_hex`
        (sandbox, {"echo_token": "a" * 32}, "messages_echo_token_ck"),  # un entrante
    ):
        with pytest.raises(IntegrityError, match=constraint):
            tables.message(world.a, target, **fields)


@pytest.mark.parametrize(
    ("platform", "app_id"), [("MESSENGER", APP_ID), ("INSTAGRAM", APP_ID), ("INSTAGRAM", None)]
)
def test_our_echo_reconciles_a_lost_send_with_its_mid(
    world: SimpleNamespace, graph: FakePageGraph, migrator: DB, platform: str, app_id: str | None
) -> None:
    message = lost(world, graph, platform)
    net = MESSENGER if platform == "MESSENGER" else INSTAGRAM
    assert hook(net, graph.echo(net, app_id=app_id)) == {"PROCESSED": 1}
    saved = state(world, message)
    assert (saved.delivery_status, saved.external_message_id) == ("SENT", "m_SIMOUT000001")
    assert saved.sent_at == AT  # el instante del eco


def test_a_late_echo_reconciles_a_send_the_sweeper_closed_as_unknown(
    world: SimpleNamespace, graph: FakePageGraph
) -> None:
    message = lost(world, graph, "MESSENGER")
    with tenant_scope(ctx(world.a)) as tenant:  # venció su plazo sin saber si salió
        commands.fail_sending(tenant, message_id=message.pk, reason="PROVIDER_UNKNOWN")
    assert hook(MESSENGER, graph.echo(MESSENGER)) == {"PROCESSED": 1}
    saved = state(world, message)
    assert (saved.delivery_status, saved.provider_error_code) == ("SENT", None)
    assert saved.external_message_id == "m_SIMOUT000001"


def test_an_echo_never_overwrites_a_mid_nor_moves_a_message_back(
    world: SimpleNamespace, graph: FakePageGraph, migrator: DB
) -> None:
    message, _ = send(world, talk(world, "INSTAGRAM"))
    assert hook(INSTAGRAM, social.read(INSTAGRAM, mid="m_SIMOUT000001")) == {"PROCESSED": 1}
    assert hook(INSTAGRAM, graph.echo(INSTAGRAM)) == {"PROCESSED": 1}  # el mismo `mid`
    other = graph.echo(INSTAGRAM)  # la misma ficha con otro `mid`
    other["message"]["mid"] = "m_SIMOTRO00001"
    assert hook(INSTAGRAM, other) == {"IGNORED": 1}
    saved = state(world, message)
    assert (saved.delivery_status, saved.external_message_id) == ("READ", "m_SIMOUT000001")
    assert items(migrator)[-1] == ("IGNORED", "ECHO_MID_CONFLICT", True)


def test_an_echo_of_another_app_or_without_app_id_in_messenger_is_foreign(
    world: SimpleNamespace, graph: FakePageGraph, migrator: DB, settings: Any
) -> None:
    message = lost(world, graph, "MESSENGER")
    echo = graph.echo(MESSENGER, app_id=OTHER_APP_ID)
    hook(MESSENGER, echo)
    no_app = graph.echo(MESSENGER, app_id=None)
    no_app["message"]["mid"] = "m_SIMOUT000002"
    hook(MESSENGER, no_app)
    settings.META_APP_ID = ""  # sin el id de nuestra app, ningún eco con `app_id` es nuestro
    ours = graph.echo(MESSENGER)
    ours["message"]["mid"] = "m_SIMOUT000003"
    hook(MESSENGER, ours)
    assert items(migrator) == [("IGNORED", "FOREIGN_ECHO", True)] * 3
    assert state(world, message).delivery_status == "SENDING"


@pytest.mark.parametrize(
    ("app_id", "closing"), [(APP_ID, "UNKNOWN_MESSAGE"), (None, "FOREIGN_ECHO")]
)
def test_an_echo_without_its_message_waits_and_then_closes_without_its_payload(
    world: SimpleNamespace, graph: FakePageGraph, migrator: DB, app_id: str | None, closing: str
) -> None:
    send(world, talk(world, "INSTAGRAM"))
    echo = graph.echo(INSTAGRAM, app_id=app_id)
    echo["recipient"]["id"] = "100000000000299"  # la ficha es de otro cliente de la cuenta
    outcomes = []
    for _ in range(ingress.UNKNOWN_AFTER):
        migrator.execute("UPDATE webhook_ingress SET next_attempt_at = now()")
        outcomes.append(hook(INSTAGRAM, echo))
    assert outcomes == [{"FAILED": 1}, {"FAILED": 1}, {"IGNORED": 1}]
    assert items(migrator) == [("IGNORED", closing, True)]


def test_messenger_marks_delivered_and_read_by_watermark_on_the_identity_thread(
    world: SimpleNamespace, graph: FakePageGraph, migrator: DB
) -> None:
    convo = talk(world, "MESSENGER")
    early, late = (send(world, convo)[0] for _ in range(2))
    pending, _ = outbound(world, convo)
    with tenant_scope(ctx(world.a)) as tenant:  # reclamado y aún sin respuesta de Meta
        assert commands.claim_outbound(tenant, message_id=pending.pk) is not None
    with tenant_scope(ctx(world.a)):
        other = Contact.objects.create(display_name="Otro cliente")
    who = identity(world.a, other, account_of(world, convo), "100000000000299")
    elsewhere = tables.conversation(
        world.a, other, account_of(world, convo), "CONV-09997", contact_identity_id=who.pk,
        last_inbound_at=timezone.now(),
    )  # fmt: skip
    stranger = send(world, elsewhere)[0]  # otra identidad de la misma página
    when = {early: AT - dt.timedelta(seconds=5), late: AT + dt.timedelta(seconds=5)}
    with tenant_scope(ctx(world.a)):
        for message, at in (*when.items(), (stranger, AT - dt.timedelta(seconds=9))):
            Message.objects.filter(pk=message.pk).update(sending_at=at)
        Message.objects.filter(pk=pending.pk).update(sending_at=AT - dt.timedelta(seconds=1))

    def states() -> list[str | None]:
        return [state(world, m).delivery_status for m in (early, late, pending, stranger)]

    # Lo reclamado antes de la marca avanza; lo que sigue `SENDING` hace esperar al ítem.
    assert hook(MESSENGER, social.delivery()) == {"FAILED": 1}
    assert states() == ["DELIVERED", "SENT", "SENDING", "SENT"]
    assert state(world, early).delivered_at == AT
    with tenant_scope(ctx(world.a)) as tenant:  # el envío confirmó: la entrega se reintenta
        commands.settle_outbound(tenant, message_id=pending.pk, result=SendResult(True, "m_X"))
    migrator.execute("UPDATE webhook_ingress SET next_attempt_at = now()")
    assert ingress.process() == {"PROCESSED": 1}
    assert states() == ["DELIVERED", "SENT", "DELIVERED", "SENT"]
    assert hook(MESSENGER, social.read(MESSENGER, watermark=social.AT + 10_000)) == {"PROCESSED": 1}
    assert states() == ["READ", "READ", "READ", "SENT"]
    assert hook(MESSENGER, social.delivery(watermark=social.AT + 20_000)) == {"PROCESSED": 1}
    assert states() == ["READ", "READ", "READ", "SENT"]  # nunca retrocede


def test_instagram_marks_read_by_mid_and_waits_for_an_unknown_one(
    world: SimpleNamespace, graph: FakePageGraph, migrator: DB
) -> None:
    message, _ = send(world, talk(world, "INSTAGRAM"))
    assert hook(INSTAGRAM, social.read(INSTAGRAM, mid="m_SIMOUT000009")) == {"FAILED": 1}
    assert state(world, message).delivery_status == "SENT"
    assert hook(INSTAGRAM, social.read(INSTAGRAM, mid="m_SIMOUT000001")) == {"PROCESSED": 1}
    saved = state(world, message)
    assert (saved.delivery_status, saved.read_at) == ("READ", AT)
