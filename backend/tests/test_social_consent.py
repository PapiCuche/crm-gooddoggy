"""F7b-05b: las palabras de baja y de alta también valen en Instagram y Messenger (ADR-035 §5,
ADR-010 §2.7). La baja se registra contra el canal de la cuenta y la política la consulta con ese
mismo canal; la confirmación nombra el canal y sale por la página solo dentro de la ventana (nunca
con la etiqueta Human Agent); un hilo en `standby` no se responde; un `mid` que no es imprimible se
descarta en el parser en vez de reintentarse hasta `DEAD`. F7b-05d: fuera de la ventana una baja
niega también la respuesta con la etiqueta Human Agent, al encolar y al despachar; una palabra
entregada tarde no revierte otra posterior. PostgreSQL real con `crm_app`; el simulador firma los
webhooks y hace de Graph API."""

import datetime as dt
import logging
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from django.utils import timezone

from apps.contacts import keywords
from apps.contacts.consents import revoke_consent
from apps.contacts.models import ContactConsent
from apps.conversations import inbound, ingest_alerts, ingress, meta_webhook, policy, tasks
from apps.conversations.models import Conversation, Message
from core.observability import reporting
from core.tenancy.scope import tenant_scope
from tests.meta_sim import social
from tests.meta_sim.page_graph import FakePageGraph
from tests.meta_sim.social import IG_ACCOUNT_ID, PAGE_ID
from tests.test_consent_stop import final, spied, two_batches
from tests.test_conversation_commands import outbound, world, writer
from tests.test_conversations_dispatch import Retrying, state
from tests.test_memberships import ctx
from tests.test_messaging_policy import HOUR, NOW, decide
from tests.test_meta_alerts import Spy, skewed
from tests.test_meta_webhook import channel, post
from tests.test_social_outbound import CHANNELS, graph, talk
from tests.test_whatsapp_outbound import redis

__all__ = ["channel", "graph", "redis", "world", "writer"]  # las fixtures, para pytest
pytestmark = pytest.mark.usefixtures("tenant_db", "graph")
SIM = {"MESSENGER": social.MESSENGER, "INSTAGRAM": social.INSTAGRAM}
PLATFORMS = sorted(SIM)
STOP, BACK = keywords.OPT_OUT, keywords.OPT_IN
SECONDS = dt.timedelta(seconds=5)


def event(platform: str, body: str, mid: str, ago: dt.timedelta = SECONDS) -> dict[str, Any]:
    at = int((timezone.now() - ago).timestamp() * 1000)  # milisegundos [S18]
    return social.text(SIM[platform], body, mid=mid, at=at)


def say(platform: str, found: dict[str, Any], field: str = "messaging") -> dict[str, int]:
    """El cliente escribe por la cuenta de la organización A (o en `standby`)."""
    assert post(social.signed(SIM[platform], found, field=field)).status_code == 200
    return ingress.process()


def seen(world: SimpleNamespace) -> SimpleNamespace:
    with tenant_scope(ctx(world.a)):
        said = Message.objects.filter(direction="INBOUND").values_list("body", flat=True)
        return SimpleNamespace(
            consents=[
                (c.channel, c.purpose, c.status, c.source) for c in ContactConsent.objects.all()
            ],
            replies=list(Message.objects.filter(sender_type="SYSTEM").order_by("created_at")),
            inbound=list(said),
        )


@pytest.mark.parametrize("platform", PLATFORMS)
def test_a_stop_and_an_alta_are_recorded_against_the_channel_and_confirmed_in_its_name(
    world: SimpleNamespace, platform: str
) -> None:
    convo = talk(world, platform)
    assert say(platform, event(platform, "BAJA", "m_SIMIN000001", 2 * SECONDS)) == {"PROCESSED": 1}
    found = seen(world)
    assert found.consents == [(platform, "ALL_PROACTIVE", "REVOKED", "KEYWORD_STOP")]
    [reply] = found.replies
    assert reply.body == inbound.CONFIRMATIONS[platform][STOP]
    assert f"por {platform.title()}." in reply.body and "WhatsApp" not in reply.body
    assert reply.policy_decision["mode"] == "FREE_FORM"
    with pytest.raises(policy.ConsentRevoked):  # la política lee la baja de ese canal
        outbound(world, convo, purpose="FOLLOW_UP")
    assert outbound(world, convo)[0].policy_decision["allowed"] is True  # responder sí
    say(platform, event(platform, "Alta!", "m_SIMIN000002"))
    found = seen(world)
    assert found.consents == [(platform, "ALL_PROACTIVE", "GRANTED", "CUSTOMER_MESSAGE")]
    assert [r.body for r in found.replies] == [
        inbound.CONFIRMATIONS[platform][k] for k in (STOP, BACK)
    ]
    assert f"por {platform.title()}." in found.replies[1].body


def test_each_channel_has_its_own_words_and_whatsapp_keeps_its_own() -> None:
    texts = {p: set(inbound.CONFIRMATIONS[p].values()) for p in ("WHATSAPP", *PLATFORMS)}
    assert all(len(t) == 2 for t in texts.values())
    assert not set.intersection(*texts.values())  # ninguna frase compartida entre canales
    assert all("WhatsApp" in t for t in texts["WHATSAPP"])
    assert all(len(t.encode()) <= 1000 for t in texts["INSTAGRAM"])  # el tope de Instagram


@pytest.mark.parametrize("platform", PLATFORMS)
def test_the_policy_reads_the_consent_of_the_conversation_channel_not_another(
    world: SimpleNamespace, platform: str
) -> None:
    convo = talk(world, platform)
    other = next(p for p in PLATFORMS if p != platform)
    for revoked in ("WHATSAPP", other):  # la baja de otro canal no alcanza a este
        with tenant_scope(ctx(world.a)) as tenant:
            revoke_consent(
                tenant, contact_id=world.contact_a.pk, channel=revoked, purpose="ALL_PROACTIVE",
                source="AGENT_RECORDED",
            )  # fmt: skip
    assert outbound(world, convo, purpose="FOLLOW_UP")[0].policy_decision["allowed"] is True
    with tenant_scope(ctx(world.a)) as tenant:
        revoke_consent(
            tenant, contact_id=world.contact_a.pk, channel=platform, purpose="FOLLOW_UP",
            source="AGENT_RECORDED",
        )  # fmt: skip
    with pytest.raises(policy.ConsentRevoked):
        outbound(world, convo, purpose="FOLLOW_UP")


@pytest.mark.parametrize("platform", PLATFORMS)
def test_the_confirmation_goes_out_through_the_page_as_a_response_without_a_tag(
    world: SimpleNamespace, graph: FakePageGraph, platform: str
) -> None:
    talk(world, platform)
    say(platform, event(platform, "stop", "m_SIMIN000001"))
    [reply] = seen(world).replies
    tasks.deliver(Retrying(0), world.a, reply.pk)
    saved = state(world, reply)
    assert saved.delivery_status == "SENT"
    [sent] = graph.sent
    assert sent["recipient"] == {"id": CHANNELS[platform][1]}  # el PSID o el IGSID
    assert (sent["messaging_type"], "tag" in sent) == ("RESPONSE", False)
    assert sent["message"] == {  # con su ficha del eco, como todo envío social (F7b-05c)
        "text": inbound.CONFIRMATIONS[platform][STOP],
        "metadata": saved.echo_token,
    }


@pytest.mark.parametrize("platform", PLATFORMS)
def test_outside_the_window_the_stop_is_kept_but_never_confirmed_even_with_human_agent(
    world: SimpleNamespace, graph: FakePageGraph, settings: Any, platform: str
) -> None:
    settings.META_HUMAN_AGENT_APPROVED = True  # una persona podría escribir; el sistema, no
    late = dt.timedelta(hours=30)  # Meta reintenta 36 h: un STOP puede llegar tarde
    talk(world, platform, ago=30)
    assert say(platform, event(platform, "BAJA", "m_SIMIN000001", ago=late)) == {"PROCESSED": 1}
    found = seen(world)
    assert found.consents == [(platform, "ALL_PROACTIVE", "REVOKED", "KEYWORD_STOP")]
    assert found.replies == [] and graph.sent == []


@pytest.mark.parametrize("platform", PLATFORMS)
def test_a_thread_in_standby_keeps_the_stop_but_is_never_answered(
    world: SimpleNamespace, platform: str
) -> None:
    talk(world, platform)
    say(platform, event(platform, "BAJA", "m_SIMIN000001"), field="standby")
    found = seen(world)
    assert found.consents == [(platform, "ALL_PROACTIVE", "REVOKED", "KEYWORD_STOP")]
    assert found.replies == []


def test_a_stop_answering_a_story_counts_without_its_fixed_phrase(world: SimpleNamespace) -> None:
    talk(world, "INSTAGRAM")
    at = int(timezone.now().timestamp() * 1000)
    say("INSTAGRAM", social.story_reply("BAJA", mid="m_SIMIN000001", at=at))
    found = seen(world)
    assert found.inbound == ["Respondió a tu historia: BAJA"]
    assert found.consents == [("INSTAGRAM", "ALL_PROACTIVE", "REVOKED", "KEYWORD_STOP")]


@pytest.mark.parametrize("platform", PLATFORMS)
def test_a_stop_claimed_again_after_an_alta_does_not_act_twice(
    world: SimpleNamespace, platform: str
) -> None:
    talk(world, platform)
    stop = event(platform, "BAJA", "m_SIMIN000001", 2 * SECONDS)
    say(platform, stop)
    say(platform, event(platform, "ALTA", "m_SIMIN000002"))  # un empate lo dejaría de baja
    again = ingress.Claimed(  # el mismo ítem, reclamado otra vez después
        uuid4(), "message", None, None, stop | {"standby": False}, 2, platform,
        PAGE_ID if platform == "MESSENGER" else IG_ACCOUNT_ID,
    )  # fmt: skip
    assert ingress.route(again)[0] == "PROCESSED"
    found = seen(world)
    assert found.consents == [(platform, "ALL_PROACTIVE", "GRANTED", "CUSTOMER_MESSAGE")]
    assert len(found.replies) == 2


@pytest.mark.parametrize(("newer", "first", "gap"), [("ALTA", "new", 10), ("ALTA", "old", 0)])
@pytest.mark.parametrize("platform", PLATFORMS)
def test_two_batches_at_once_follow_the_time_of_meta_on_each_channel(
    world: SimpleNamespace, migrator: Any, monkeypatch: pytest.MonkeyPatch, platform: str,
    newer: str, first: str, gap: int,
) -> None:  # fmt: skip
    """F7b-05e: la ALTA más nueva se confirma antes que la BAJA vieja; o empatan y gana la baja."""
    talk(world, platform)
    older = "BAJA" if newer == "ALTA" else "ALTA"
    base = int(timezone.now().timestamp()) * 1000 - 20_000  # una sola lectura: empate exacto
    for word, at, mid in (
        (older, base, "m_SIMIN000010"),
        (newer, base + gap * 1000, "m_SIMIN000020"),
    ):
        found = social.text(SIM[platform], word, mid=mid, at=at)
        assert post(social.signed(SIM[platform], found)).status_code == 200
    leader = "m_SIMIN000020" if first == "new" else "m_SIMIN000010"
    assert two_batches(migrator, monkeypatch, leader) == ["PROCESSED", "PROCESSED"]
    assert seen(world).consents == [(platform, "ALL_PROACTIVE", *final(newer, gap))]


@pytest.mark.parametrize("newer", ["ALTA", "BAJA"])
@pytest.mark.parametrize("platform", PLATFORMS)
def test_one_delivery_out_of_order_follows_the_time_of_meta_on_each_channel(
    world: SimpleNamespace, platform: str, newer: str
) -> None:
    talk(world, platform)
    older = "BAJA" if newer == "ALTA" else "ALTA"
    both = (  # `store` guarda por clave: el `mid` menor, el más nuevo, se procesa primero
        event(platform, newer, "m_SIMIN000001", 2 * SECONDS),
        event(platform, older, "m_SIMIN000002", 4 * SECONDS),
    )
    assert post(social.signed(SIM[platform], *both)).status_code == 200
    assert ingress.process() == {"PROCESSED": 2}
    with tenant_scope(ctx(world.a)):
        said = Message.objects.filter(direction="INBOUND").order_by("-created_at")
        assert list(said.values_list("body", flat=True)[:2]) == [older, newer]  # al revés
    found = seen(world)
    assert found.consents == [(platform, "ALL_PROACTIVE", *final(newer, 10))]
    assert len(found.replies) == 1  # la vieja ni cambia ni confirma


@pytest.mark.parametrize(
    "mid", [" m_SIMIN000001", "m_SIMIN000001\n", "m_\x00SIMIN", "m_ SIMIN", "m_​SIMIN"]
)
@pytest.mark.parametrize("platform", PLATFORMS)
def test_a_mid_that_is_not_printable_is_refused_by_the_parser_and_never_retried(
    world: SimpleNamespace, migrator: Any, platform: str, mid: str
) -> None:
    talk(world, platform)
    bad, good = event(platform, "Hola", mid), event(platform, "Hola", "m_SIMIN000002")
    parsed = meta_webhook.parse(social.signed(SIM[platform], bad, good).body)
    assert parsed.rejected == ["mid"] and len(parsed.items) == 1
    assert post(social.signed(SIM[platform], bad, good)).status_code == 200
    prefix = "fb" if platform == "MESSENGER" else "ig"
    keys = migrator.execute("SELECT event_key FROM webhook_ingress").fetchall()
    where = SIM[platform].account  # la clave lleva la cuenta (F7b-04d)
    assert keys == [(f"{prefix}:msg:{where}:m_SIMIN000002",)]  # el raro, ni guardado ni reintentado
    assert ingress.process() == {"PROCESSED": 1}
    with tenant_scope(ctx(world.a)):
        assert Conversation.objects.count() == 1


def revoke(world: SimpleNamespace, channel: str) -> None:
    with tenant_scope(ctx(world.a)) as tenant:
        revoke_consent(
            tenant, contact_id=world.contact_a.pk, channel=channel, purpose="ALL_PROACTIVE",
            source="KEYWORD_STOP",
        )  # fmt: skip


def test_outside_the_window_a_revocation_denies_the_tag_and_inside_nothing_changes() -> None:
    late, approved = NOW - 30 * HOUR, {"sender": "USER", "human_agent_approved": True}
    for platform in PLATFORMS:
        denied = decide(platform=platform, last_inbound_at=late, revoked=True, **approved)
        assert (denied.allowed, denied.reason, denied.mode) == (False, "CONSENT_REVOKED", "BLOCKED")
        tagged = decide(platform=platform, last_inbound_at=late, revoked=False, **approved)
        assert tagged.mode == "HUMAN_AGENT_TAG"
        inside = decide(platform=platform, last_inbound_at=NOW - HOUR, revoked=True, **approved)
        assert (inside.allowed, inside.mode) == (True, "FREE_FORM")  # la regla 7 de ADR-010
    whatsapp = decide(platform="WHATSAPP", last_inbound_at=late, revoked=True, **approved)
    assert whatsapp.reason == "WINDOW_CLOSED"  # sin etiqueta: lo mismo que antes


@pytest.mark.parametrize("platform", PLATFORMS)
@pytest.mark.parametrize("revoked", ["SAME", "ANY"])
def test_a_revocation_blocks_a_tagged_reply_when_queued_and_when_dispatched(
    world: SimpleNamespace, graph: FakePageGraph, settings: Any, platform: str, revoked: str
) -> None:
    settings.META_HUMAN_AGENT_APPROVED = True
    convo = talk(world, platform, ago=30)
    waiting, _ = outbound(world, convo)  # encolada con la etiqueta, antes de la baja
    assert waiting.policy_decision["mode"] == "HUMAN_AGENT_TAG"
    revoke(world, platform if revoked == "SAME" else "ANY")
    with pytest.raises(policy.ConsentRevoked):  # al encolar
        outbound(world, convo)
    tasks.deliver(Retrying(0), world.a, waiting.pk)  # al despachar, con la baja ya registrada
    saved = state(world, waiting)
    assert (saved.delivery_status, saved.provider_error_code) == ("FAILED", "CONSENT_REVOKED")
    assert graph.sent == []


@pytest.mark.parametrize("platform", PLATFORMS)
def test_a_revocation_of_another_channel_keeps_the_tag_and_inside_the_window_the_reply_goes(
    world: SimpleNamespace, graph: FakePageGraph, settings: Any, platform: str
) -> None:
    settings.META_HUMAN_AGENT_APPROVED = True
    revoke(world, "WHATSAPP")
    convo = talk(world, platform, ago=30)
    message, _ = outbound(world, convo)
    tasks.deliver(Retrying(0), world.a, message.pk)
    assert state(world, message).delivery_status == "SENT"
    assert graph.sent[0]["tag"] == "HUMAN_AGENT"
    revoke(world, platform)
    with tenant_scope(ctx(world.a)):  # el cliente vuelve a escribir: dentro, la respuesta sale
        Conversation.objects.filter(pk=convo.pk).update(last_inbound_at=timezone.now() - HOUR)
    inside, _ = outbound(world, convo)
    tasks.deliver(Retrying(0), world.a, inside.pk)
    assert state(world, inside).delivery_status == "SENT"
    assert (graph.sent[1]["messaging_type"], "tag" in graph.sent[1]) == ("RESPONSE", False)


@pytest.mark.parametrize("platform", PLATFORMS)
def test_an_alta_delivered_after_a_later_baja_leaves_the_customer_out(
    world: SimpleNamespace, platform: str
) -> None:
    talk(world, platform)
    say(platform, event(platform, "BAJA", "m_SIMIN000001", ago=dt.timedelta(seconds=60)))
    later = event(platform, "BAJA", "m_SIMIN000003", ago=dt.timedelta(seconds=10))
    earlier = event(platform, "ALTA", "m_SIMIN000002", ago=dt.timedelta(seconds=20))
    say(platform, later)  # Meta entrega al revés: la baja de después llega primero
    say(platform, earlier)
    found = seen(world)
    assert found.consents == [(platform, "ALL_PROACTIVE", "REVOKED", "KEYWORD_STOP")]
    assert [r.body for r in found.replies] == [inbound.CONFIRMATIONS[platform][STOP]]


@pytest.mark.parametrize("platform", PLATFORMS)
def test_a_baja_delivered_after_a_later_alta_does_not_revert_it(
    world: SimpleNamespace, platform: str
) -> None:
    talk(world, platform)
    say(platform, event(platform, "BAJA", "m_SIMIN000001", ago=dt.timedelta(seconds=60)))
    later = event(platform, "ALTA", "m_SIMIN000003", ago=dt.timedelta(seconds=10))
    earlier = event(platform, "BAJA", "m_SIMIN000002", ago=dt.timedelta(seconds=20))
    say(platform, later)
    say(platform, earlier)
    found = seen(world)
    assert found.consents == [(platform, "ALL_PROACTIVE", "GRANTED", "CUSTOMER_MESSAGE")]
    assert len(found.replies) == 2  # la baja vieja no confirma nada


@pytest.mark.parametrize(
    ("first", "second", "ms", "status"),
    [("BAJA", "ALTA", (10_000, 0), "REVOKED"), ("BAJA", "ALTA", (100, 900), "REVOKED"),
     ("ALTA", "BAJA", (10_000, 0), "GRANTED")],
)  # fmt: skip
@pytest.mark.parametrize("platform", PLATFORMS)
def test_with_our_clock_behind_meta_each_word_keeps_its_meta_time(
    world: SimpleNamespace, platform: str, first: str, second: str, ms: tuple[int, int],
    status: str,
) -> None:  # fmt: skip
    """F7b-05f: Meta va 30 s por delante y `first` llega antes. Una palabra 10 s más vieja no
    revierte la nueva; dos del mismo segundo (unos milisegundos aparte) empatan: gana la baja."""
    talk(world, platform)
    base = (int(timezone.now().timestamp()) + 30) * 1000
    for said, at, mid in ((first, ms[0], "m_SIMIN000001"), (second, ms[1], "m_SIMIN000002")):
        say(platform, social.text(SIM[platform], said, mid=mid, at=base + at))
    source = "KEYWORD_STOP" if status == "REVOKED" else "CUSTOMER_MESSAGE"
    assert seen(world).consents == [(platform, "ALL_PROACTIVE", status, source)]


@pytest.mark.parametrize("stamp", ["1700000000000", -5, True, 10**15])
@pytest.mark.parametrize("platform", PLATFORMS)
def test_a_provider_time_that_does_not_parse_never_lets_an_alta_lift_a_baja(
    world: SimpleNamespace, platform: str, stamp: Any
) -> None:
    """F7b-05f: con una hora que no vale (texto, negativa, booleana, más allá de 2100) la BAJA
    revoca por llegada y el ALTA no cuenta; solo sin hora cuenta la llegada."""
    talk(world, platform)
    for word, mid in (("BAJA", "m_SIMIN000001"), ("ALTA", "m_SIMIN000002")):
        say(platform, event(platform, word, mid) | {"timestamp": stamp})
    assert seen(world).consents == [(platform, "ALL_PROACTIVE", "REVOKED", "KEYWORD_STOP")]


@pytest.mark.parametrize("missing", [True, False])  # sin `timestamp`, o con `null`
@pytest.mark.parametrize("platform", PLATFORMS)
def test_an_alta_without_the_provider_time_counts_for_nothing_on_each_channel(
    world: SimpleNamespace, platform: str, missing: bool
) -> None:
    """Decisión del propietario (#937): un ALTA sin hora no cuenta, sola o tras una BAJA."""
    talk(world, platform)

    def untimed(mid: str) -> dict[str, Any]:
        found = event(platform, "ALTA", mid)
        return {k: v for k, v in found.items() if k != "timestamp"} if missing else (
            found | {"timestamp": None}
        )  # fmt: skip

    say(platform, untimed("m_SIMIN000001"))
    assert (seen(world).consents, seen(world).replies) == ([], [])
    say(platform, event(platform, "BAJA", "m_SIMIN000002"))
    say(platform, untimed("m_SIMIN000003"))
    found = seen(world)
    assert found.consents == [(platform, "ALL_PROACTIVE", "REVOKED", "KEYWORD_STOP")]
    assert [r.body for r in found.replies] == [inbound.CONFIRMATIONS[platform][STOP]]


@pytest.mark.parametrize("platform", PLATFORMS)
def test_a_reply_reads_the_consent_only_when_it_would_go_out_with_the_tag(
    world: SimpleNamespace, settings: Any, monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    calls = spied(monkeypatch)
    settings.META_HUMAN_AGENT_APPROVED = True
    convo = talk(world, platform)
    outbound(world, convo)  # dentro de la ventana la baja no cambia nada: ni se consulta
    assert calls == []
    with tenant_scope(ctx(world.a)):
        Conversation.objects.filter(pk=convo.pk).update(last_inbound_at=timezone.now() - 30 * HOUR)
    assert outbound(world, convo)[0].policy_decision["mode"] == "HUMAN_AGENT_TAG"
    assert len(calls) == 1
    settings.META_HUMAN_AGENT_APPROVED = False
    with pytest.raises(policy.WindowClosed):
        outbound(world, convo)
    assert len(calls) == 1


@pytest.mark.parametrize("platform", PLATFORMS)
def test_social_messages_ahead_of_our_clock_raise_the_skew_alert_with_figures_only(
    world: SimpleNamespace, platform: str, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:  # fmt: skip
    """F7b-05g: el reloj del worker 45 s detrás del real; cinco mensajes nuevos avisan una vez."""
    talk(world, platform)
    skewed(monkeypatch, 45)
    spy = Spy()
    monkeypatch.setattr(reporting, "reporter", lambda: spy)
    caplog.set_level(logging.DEBUG)
    for n in range(ingest_alerts.SKEW_SAMPLES):
        assert spy.messages == []
        say(platform, event(platform, "Hola", f"m_SKEW{n:08d}", dt.timedelta(seconds=1)))
    [(found, context, _)] = spy.messages
    assert found == ingest_alerts.SKEW_EVENT and 40 <= context.pop("ahead_seconds") <= 45
    assert context == {"samples": 5, "threshold": 30}
    customer = SIM[platform].customer
    for secret in (customer, "Hola", "m_SKEW"):
        assert secret not in caplog.text, secret
