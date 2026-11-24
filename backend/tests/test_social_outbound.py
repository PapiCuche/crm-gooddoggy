"""F7b-05a: responder por Messenger e Instagram contra la Graph API falsa de la página
(`tests/meta_sim/page_graph.py`, ADR-035 §6–§7): el destinatario de la identidad, la ventana de
24 h y la etiqueta Human Agent, cada error de Meta por su `code` y `error_subcode` (el oráculo es
`page_graph.ERRORS`), los resultados ambiguos y el tenant. PostgreSQL real con `crm_app`; el
simulador sustituye solo la capa de red de `core.http`."""

import dataclasses
import datetime as dt
import io
import itertools
from types import SimpleNamespace
from typing import Any

import pytest
import urllib3
from django.utils import timezone
from urllib3.exceptions import ReadTimeoutError

from apps.conversations import (
    channels,
    commands,
    credentials,
    limits,
    meta_page,
    policy,
    tasks,
    whatsapp,
)
from apps.conversations import graph as graph_api
from apps.conversations.models import ChannelAccount, Conversation
from apps.conversations.policy import WindowClosed
from core import http
from core.tenancy.scope import require_scope, tenant_scope
from tests import test_conversations_tables as tables
from tests.meta_sim import GRAPH_VERSION, transport
from tests.meta_sim.graph import TEST_TOKEN
from tests.meta_sim.page_graph import ERRORS, FakePageGraph
from tests.meta_sim.social import IG_ACCOUNT_ID, IGSID, PAGE_ID, PSID
from tests.test_conversation_commands import outbound, world, writer
from tests.test_conversations_dispatch import Retrying, state
from tests.test_memberships import ctx
from tests.test_messaging_policy import HOUR, NOW, decide
from tests.test_social_schema import account, identity
from tests.test_whatsapp_outbound import redis

__all__ = ["redis", "world", "writer"]  # los fixtures, para pytest
pytestmark = pytest.mark.usefixtures("tenant_db")
NUMBERS = itertools.count(1)
CHANNELS = {"MESSENGER": (PAGE_ID, PSID), "INSTAGRAM": (IG_ACCOUNT_ID, IGSID)}


@pytest.fixture
def graph(monkeypatch: pytest.MonkeyPatch, settings: Any) -> FakePageGraph:
    settings.META_ENABLED, settings.META_GRAPH_VERSION = True, GRAPH_VERSION
    settings.HTTP_ALLOWED_HOSTS = [transport.GRAPH_HOST]
    fake = FakePageGraph()
    transport.install(monkeypatch, fake)
    return fake


def talk(world: SimpleNamespace, platform: str = "INSTAGRAM", ago: float = 1) -> Conversation:
    """Una conversación conectada de la plataforma, con la identidad del cliente en esa cuenta,
    al que el cliente escribió hace `ago` horas."""
    external, customer = CHANNELS[platform]
    made = account(world.a, platform, external)
    with tenant_scope(ctx(world.a)) as tenant:
        stored = credentials.store(tenant, made.pk, TEST_TOKEN)
        ChannelAccount.objects.filter(pk=made.pk).update(
            status="CONNECTED", credential_id=stored.pk, last_healthcheck_at=timezone.now()
        )
    who = identity(world.a, world.contact_a, made, customer)
    return tables.conversation(  # type: ignore[no-any-return]
        world.a, world.contact_a, made, f"CONV-{next(NUMBERS):05d}", contact_identity_id=who.pk,
        last_inbound_at=timezone.now() - dt.timedelta(hours=ago),
    )  # fmt: skip


def send(world: SimpleNamespace, convo: Conversation, body: str = "Hola, ¿en qué te ayudo?") -> Any:
    message, _ = outbound(world, convo, body=body)
    tasks.deliver(Retrying(0), world.a, message.pk)
    return message, state(world, message)


def account_of(world: SimpleNamespace, convo: Conversation) -> ChannelAccount:
    with tenant_scope(ctx(world.a)):
        found: ChannelAccount = ChannelAccount.objects.get(pk=convo.channel_account_id)
        return found


@pytest.mark.parametrize("name", sorted(ERRORS))
def test_every_meta_error_is_classified_by_code_and_subcode_never_by_http_status(
    name: str,
) -> None:
    _, code, subcode, _, expected = ERRORS[name]  # el estado HTTP no entra: `token` y `token_400`
    assert meta_page.classify(code, subcode) == expected


def test_unreadable_codes_are_unknown_and_unlisted_subcodes_are_rejections() -> None:
    assert meta_page.classify(None, None) == "PROVIDER_ERROR"  # no se sabe: ambiguo
    assert meta_page.classify(True, 2018278) == "PROVIDER_ERROR"  # un `bool` no es un código
    assert meta_page.classify("10", 2018278) == "PROVIDER_ERROR"
    assert meta_page.classify(10, "2018278") == "PROVIDER_REJECTED"
    assert meta_page.classify(200, 2018278) == "PROVIDER_REJECTED"
    assert meta_page.classify(10, 2534041) == "PROVIDER_REJECTED"


@pytest.mark.parametrize("platform", sorted(CHANNELS))
def test_a_reply_inside_the_window_goes_to_the_identity_through_the_page(
    world: SimpleNamespace, graph: FakePageGraph, platform: str
) -> None:
    message, saved = send(world, talk(world, platform))
    assert (saved.delivery_status, saved.external_message_id) == ("SENT", "m_SIMOUT000001")
    sent, call = graph.sent[0], graph.calls[0]
    assert sent["recipient"] == {"id": CHANNELS[platform][1]}  # el PSID o el IGSID
    assert (sent["messaging_type"], "tag" in sent) == ("RESPONSE", False)
    assert sent["message"] == {"text": "Hola, ¿en qué te ayudo?", "metadata": saved.echo_token}
    assert call["path"] == f"/{GRAPH_VERSION}/{PAGE_ID}/messages"  # Instagram, por su página
    assert saved.policy_decision["mode"] == "FREE_FORM"


@pytest.mark.parametrize("platform", sorted(CHANNELS))
def test_outside_the_window_a_reply_is_blocked_with_its_reason_and_never_reaches_meta(
    world: SimpleNamespace, graph: FakePageGraph, platform: str
) -> None:
    convo = talk(world, platform, ago=24.01)
    with pytest.raises(WindowClosed):
        outbound(world, convo)
    queued, _ = outbound(world, convo_open(world, convo))
    with tenant_scope(ctx(world.a)):  # la ventana se cierra con el mensaje en cola
        Conversation.objects.filter(pk=convo.pk).update(
            last_inbound_at=timezone.now() - dt.timedelta(hours=25)
        )
    tasks.deliver(Retrying(0), world.a, queued.pk)
    saved = state(world, queued)
    assert (saved.delivery_status, saved.provider_error_code) == ("FAILED", "WINDOW_CLOSED")
    assert graph.calls == [] and graph.sent == []


def convo_open(world: SimpleNamespace, convo: Conversation) -> Conversation:
    """La misma conversación con la ventana abierta otra vez (el cliente volvió a escribir)."""
    with tenant_scope(ctx(world.a)):
        Conversation.objects.filter(pk=convo.pk).update(
            last_inbound_at=timezone.now() - dt.timedelta(hours=1)
        )
        found: Conversation = Conversation.objects.get(pk=convo.pk)
        return found


def test_human_agent_tag_goes_out_only_when_meta_approved_it_and_within_seven_days(
    world: SimpleNamespace, graph: FakePageGraph, settings: Any
) -> None:
    convo = talk(world, "MESSENGER", ago=30)
    with pytest.raises(WindowClosed):  # apagado por omisión: la ventana de 24 h manda
        outbound(world, convo)
    settings.META_HUMAN_AGENT_APPROVED = True
    message, saved = send(world, convo)
    assert saved.delivery_status == "SENT"
    assert saved.policy_decision["mode"] == "HUMAN_AGENT_TAG"
    assert (graph.sent[0]["messaging_type"], graph.sent[0]["tag"]) == ("MESSAGE_TAG", "HUMAN_AGENT")
    with tenant_scope(ctx(world.a)) as tenant:  # el sistema (una confirmación) nunca la usa
        assert commands.queue_confirmation(tenant, conversation_id=convo.pk, body="Listo") is None
    late = talk(world, "INSTAGRAM", ago=168)  # a las 168 h exactas ya no
    with pytest.raises(WindowClosed):
        outbound(world, late)


def test_a_tagged_message_waiting_in_the_queue_loses_the_tag_if_the_approval_is_withdrawn(
    world: SimpleNamespace, graph: FakePageGraph, settings: Any
) -> None:
    settings.META_HUMAN_AGENT_APPROVED = True
    message, _ = outbound(world, talk(world, "MESSENGER", ago=30))
    settings.META_HUMAN_AGENT_APPROVED = False  # el despachador re-evalúa justo antes de enviar
    tasks.deliver(Retrying(0), world.a, message.pk)
    saved = state(world, message)
    assert (saved.delivery_status, saved.provider_error_code) == ("FAILED", "WINDOW_CLOSED")
    assert graph.sent == []


def test_a_system_message_waiting_in_the_queue_never_takes_the_tag_at_dispatch(
    world: SimpleNamespace, graph: FakePageGraph, settings: Any
) -> None:
    settings.META_HUMAN_AGENT_APPROVED = True
    convo = talk(world, "INSTAGRAM")
    with tenant_scope(ctx(world.a)) as tenant:  # una confirmación del sistema, dentro de la ventana
        queued = commands.queue_confirmation(tenant, conversation_id=convo.pk, body="Listo")
        Conversation.objects.filter(pk=convo.pk).update(
            last_inbound_at=timezone.now() - dt.timedelta(hours=30)
        )
    assert queued is not None
    tasks.deliver(Retrying(0), world.a, queued.pk)
    saved = state(world, queued)
    assert (saved.delivery_status, saved.provider_error_code) == ("FAILED", "WINDOW_CLOSED")
    assert graph.sent == []


def test_only_a_person_answering_uses_the_tag_and_whatsapp_never_does() -> None:
    late = NOW - 30 * HOUR
    tagged = decide(platform="MESSENGER", last_inbound_at=late, sender="USER",
                    human_agent_approved=True)  # fmt: skip
    assert (tagged.allowed, tagged.mode, tagged.window_hours) == (True, "HUMAN_AGENT_TAG", 168)
    assert tagged.window_expires_at == late + 168 * HOUR
    assert tagged.as_json()["mode"] == "HUMAN_AGENT_TAG"
    for over in (
        {"sender": "AI_AGENT"}, {"sender": "SYSTEM"}, {"human_agent_approved": False},
        {"purpose": "FOLLOW_UP"}, {"platform": "WHATSAPP"}, {"last_inbound_at": None},
        {"last_inbound_at": NOW - 168 * HOUR},
    ):  # fmt: skip
        facts = {"platform": "MESSENGER", "last_inbound_at": late, "sender": "USER",
                 "human_agent_approved": True} | over  # fmt: skip
        denied = decide(**facts)
        assert (denied.allowed, denied.reason, denied.mode) == (False, "WINDOW_CLOSED", "BLOCKED")
    inside = decide(platform="INSTAGRAM", last_inbound_at=NOW - HOUR, sender="USER",
                    human_agent_approved=True)  # fmt: skip
    assert (inside.mode, inside.window_hours) == ("FREE_FORM", 24)  # dentro, sin etiqueta
    assert decide(platform="INSTAGRAM", last_inbound_at=NOW - 24 * HOUR).reason == "WINDOW_CLOSED"


@pytest.mark.parametrize("name", sorted(ERRORS))
def test_each_meta_error_ends_where_its_code_says(
    world: SimpleNamespace, graph: FakePageGraph, name: str
) -> None:
    convo = talk(world, "INSTAGRAM")
    graph.then(name)
    message, _ = outbound(world, convo)
    task, expected = Retrying(0), ERRORS[name][4]
    if expected == "RATE_LIMITED":  # pasajero: Meta no lo aceptó, vuelve a la cola
        with pytest.raises(RuntimeError, match="retry"):
            tasks.deliver(task, world.a, message.pk)
        assert state(world, message).delivery_status == "QUEUED" and task.asked
        return
    tasks.deliver(task, world.a, message.pk)
    saved, found = state(world, message), account_of(world, convo)
    if expected == "PROVIDER_ERROR":  # pudo haberse enviado: no se reenvía a ciegas
        assert (saved.delivery_status, found.status) == ("SENDING", "CONNECTED")
        return
    assert (saved.delivery_status, saved.provider_error_code) == ("FAILED", expected)
    _, code, subcode, *_ = ERRORS[name]  # F7b-13: el error de Meta en números, junto al código
    assert saved.provider_error_detail == (f"{code}/{subcode}" if subcode else str(code))
    demoted = expected in ("TOKEN_INVALID", "MESSAGES_ACCESS_OFF")
    assert (found.status, found.status_detail) == (
        ("ERROR", expected) if demoted else ("CONNECTED", None)
    )


@pytest.mark.parametrize("name", ["throttle_messenger", "throttle_pages", "throttle_instagram"])
def test_a_messenger_reply_throttled_by_meta_goes_back_to_the_queue_and_never_fails(
    world: SimpleNamespace, graph: FakePageGraph, name: str
) -> None:
    """F7b-06i: los límites de uso del negocio (`80001`, `80002`, `80006`) eran `PROVIDER_REJECTED`,
    un fallo final: el mensaje se perdía por ir rápido. Ahora vuelve a la cola con espera."""
    convo = talk(world, "MESSENGER")
    graph.then(name)
    message, _ = outbound(world, convo)
    task = Retrying(0)
    with pytest.raises(RuntimeError, match="retry"):
        tasks.deliver(task, world.a, message.pk)
    saved = state(world, message)
    assert (saved.delivery_status, saved.provider_error_code, task.asked) == ("QUEUED", None, [30])
    assert (account_of(world, convo).status, len(graph.sent)) == ("CONNECTED", 0)


def test_every_code_an_outbound_adapter_retries_is_also_a_throttle_for_the_graph_reads() -> None:
    assert {80001, 80002, 80006} <= set(meta_page.TOO_FAST)
    assert set(meta_page.TOO_FAST) | set(whatsapp.TOO_FAST) <= set(graph_api.THROTTLED)


@pytest.mark.parametrize("outcome", ["cut", "timeout", "cut_after", "timeout_after"])
def test_an_ambiguous_result_is_never_resent(
    world: SimpleNamespace, graph: FakePageGraph, outcome: str
) -> None:
    graph.then(outcome)
    message, saved = send(world, talk(world, "MESSENGER"))
    assert saved.delivery_status == "SENDING"
    tasks.deliver(Retrying(0), world.a, message.pk)  # otro despachador o el barredor
    assert state(world, message).delivery_status == "SENDING" and len(graph.calls) == 1


@pytest.mark.parametrize("outcome", ["no_mid", "bad_mid"])
def test_an_accepted_send_without_a_valid_message_id_stays_sending_and_is_never_resent(
    world: SimpleNamespace, graph: FakePageGraph, outcome: str
) -> None:
    graph.then(outcome)
    message, saved = send(world, talk(world, "INSTAGRAM"))
    assert (saved.delivery_status, saved.external_message_id) == ("SENDING", None)
    tasks.deliver(Retrying(0), world.a, message.pk)
    assert state(world, message).delivery_status == "SENDING" and len(graph.sent) == 1


def test_with_the_meta_channel_off_nothing_reaches_the_graph_api(
    world: SimpleNamespace, graph: FakePageGraph, settings: Any
) -> None:
    convo = talk(world, "MESSENGER")
    settings.META_ENABLED = False
    saved = send(world, convo)[1]
    assert (saved.delivery_status, saved.provider_error_code) == ("FAILED", "CHANNEL_UNAVAILABLE")
    assert graph.calls == []


@pytest.mark.parametrize("recipient", ["", "a b", "../me", "x" * 101, "1\n2", "１２３"])
def test_a_malformed_recipient_is_refused_before_any_url_is_built(
    world: SimpleNamespace, graph: FakePageGraph, recipient: str
) -> None:
    message, _ = outbound(world, talk(world, "INSTAGRAM"))
    with tenant_scope(ctx(world.a)) as tenant:
        claim = commands.claim_outbound(tenant, message_id=message.pk)
    assert claim is not None
    found = channels.adapter_for("INSTAGRAM").send(dataclasses.replace(claim, recipient=recipient))
    assert (found.ok, found.reason, graph.calls) == (False, "RECIPIENT_UNREACHABLE", [])


@pytest.mark.parametrize(
    ("platform", "page"),
    [("MESSENGER", "me"), ("MESSENGER", "1234/../5678"), ("INSTAGRAM", None),
     ("INSTAGRAM", "12345?x=1"), ("MESSENGER", "１２３４５")],
)  # fmt: skip
def test_a_malformed_page_id_is_refused_before_any_url_is_built(
    world: SimpleNamespace, graph: FakePageGraph, platform: str, page: str | None
) -> None:
    message, _ = outbound(world, talk(world, platform))
    with tenant_scope(ctx(world.a)) as tenant:
        claim = commands.claim_outbound(tenant, message_id=message.pk)
    assert claim is not None
    bad = (
        dataclasses.replace(claim, parent_id=page)
        if platform == "INSTAGRAM"
        else dataclasses.replace(claim, phone_number_id=str(page))
    )
    with pytest.raises(ValueError, match="identificador de Meta"):
        channels.adapter_for(platform).send(bad)
    assert graph.calls == []


@pytest.mark.parametrize(
    ("platform", "body"),
    [("INSTAGRAM", "ñ" * 501), ("MESSENGER", "a" * 2001)],  # 1002 bytes; 2001 caracteres
)
def test_a_text_too_long_for_the_channel_is_rejected_before_meta(
    world: SimpleNamespace, graph: FakePageGraph, platform: str, body: str
) -> None:  # F7b-16: el comando ya lo rechaza al encolar; el adaptador sigue sin enviarlo
    message, _ = outbound(world, talk(world, platform))
    with tenant_scope(ctx(world.a)) as tenant:
        claim = commands.claim_outbound(tenant, message_id=message.pk)
    assert claim is not None
    sent = channels.adapter_for(platform).send(dataclasses.replace(claim, body=body))
    assert (sent.ok, sent.reason) == (False, "ADAPTER_REJECTED")
    assert graph.calls == []
    assert limits.fits("INSTAGRAM", "ñ" * 500) and limits.fits("MESSENGER", "ñ" * 2000)


def test_the_claim_carries_the_identity_of_this_account_and_never_shows_it(
    world: SimpleNamespace, graph: FakePageGraph
) -> None:
    convo = talk(world, "INSTAGRAM")
    message, _ = outbound(world, convo)
    with tenant_scope(ctx(world.a)) as tenant:
        claim = commands.claim_outbound(tenant, message_id=message.pk)
    assert claim is not None and (claim.recipient, claim.parent_id) == (IGSID, PAGE_ID)
    assert IGSID not in repr(claim) and TEST_TOKEN not in repr(claim) and "Hola" not in repr(claim)
    assert claim.echo_token is not None and claim.echo_token not in repr(claim)  # F7b-05c
    other = account(world.a, "INSTAGRAM", "100000000000103")
    with tenant_scope(ctx(world.a)) as tenant:  # la identidad de otra cuenta no es destinataria
        assert commands._recipient(require_scope(tenant), convo, other) == ""
    adapter = channels.adapter_for("INSTAGRAM")
    nobody = adapter.send(dataclasses.replace(claim, recipient=""))
    photo = channels.Media("IMAGE", "k", "image/png", "0" * 64, "a.png")
    assert (nobody.reason, adapter.send(dataclasses.replace(claim, media=photo)).reason) == (
        "RECIPIENT_UNREACHABLE", "ADAPTER_REJECTED",
    )  # fmt: skip
    assert graph.calls == [] and adapter.send(claim).ok


def test_another_organization_cannot_dispatch_the_message(
    world: SimpleNamespace, graph: FakePageGraph
) -> None:
    message, _ = outbound(world, talk(world, "MESSENGER"))
    tasks.deliver(Retrying(0), world.b, message.pk)  # la organización la pone quien encola
    assert graph.calls == [] and state(world, message).delivery_status == "QUEUED"


def test_off_the_allowlist_nothing_leaves_and_the_message_goes_back_to_the_queue(
    world: SimpleNamespace, graph: FakePageGraph, settings: Any
) -> None:
    settings.HTTP_ALLOWED_HOSTS = []
    message, _ = outbound(world, talk(world, "MESSENGER"))
    with pytest.raises(RuntimeError, match="retry"):
        tasks.deliver(Retrying(0), world.a, message.pk)
    assert graph.calls == [] and state(world, message).delivery_status == "QUEUED"


def test_the_send_post_never_repeats_a_read_nor_sleeps_on_retry_after(
    monkeypatch: pytest.MonkeyPatch, settings: Any
) -> None:
    """Contra el `PoolManager` real de `core.http` (sin el simulador): un POST con un tiempo de
    lectura agotado sale una sola vez, y un 503 o un 429 con `Retry-After` no espera ni repite. Un
    GET con lo mismo sí repite y espera: la prueba distingue."""
    settings.HTTP_ALLOWED_HOSTS = [transport.GRAPH_HOST]
    calls: list[str] = []
    slept: list[float] = []
    plan: list[Any] = []

    def fake(self: Any, conn: Any, method: str, url: str, **kwargs: Any) -> Any:
        calls.append(method)
        found = plan[0]
        if isinstance(found, Exception):
            raise found
        return urllib3.HTTPResponse(
            body=io.BytesIO(b"{}"), headers={"Retry-After": "7"}, status=found,
            preload_content=False, request_method=method,
        )  # fmt: skip

    monkeypatch.setattr(urllib3.connectionpool.HTTPConnectionPool, "_make_request", fake)
    monkeypatch.setattr("urllib3.util.retry.time.sleep", slept.append)
    url = f"https://{transport.GRAPH_HOST}/{GRAPH_VERSION}/{PAGE_ID}/messages"
    plan[:] = [ReadTimeoutError(None, url, "Read timed out.")]  # type: ignore[arg-type]
    with pytest.raises(urllib3.exceptions.ReadTimeoutError):
        http.request("POST", url, json={})
    assert calls == ["POST"]
    for status in (503, 429):
        calls.clear()
        plan[:] = [status]
        assert http.request("POST", url, json={}).status == status
        assert (calls, slept) == (["POST"], [])
    calls.clear()
    assert http.request("GET", url).status == 429  # el arnés de verdad reintenta un GET
    assert calls == ["GET"] * 3 and slept == [7.0, 7.0]


def test_the_page_adapter_is_registered_for_both_channels() -> None:
    assert isinstance(channels.adapter_for("MESSENGER"), meta_page.PageAdapter)
    assert isinstance(channels.adapter_for("INSTAGRAM"), meta_page.PageAdapter)
    assert policy.CAPABILITIES["INSTAGRAM"].window_hours == 24
