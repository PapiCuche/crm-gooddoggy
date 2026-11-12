"""F7-08b: palabras de baja y de alta (`consent_keywords`, `contacts.keywords`), el STOP del
entrante (`inbound`) y la política de consentimiento (ADR-010 §2.7). PostgreSQL real con `crm_app`:
variantes y frases que no cuentan, la entrega repetida, el seguimiento bloqueado y la respuesta no,
el alta que lo restablece, el despachador, los permisos y el aislamiento entre organizaciones."""

import datetime as dt
import logging
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from django.db import IntegrityError, connection
from django.utils import timezone

from apps.access.selectors import AccessDenied
from apps.contacts import keywords
from apps.contacts.consents import revoke_consent
from apps.contacts.models import ConsentKeyword, Contact, ContactConsent, ContactConsentEvent
from apps.conversations import commands, inbound, ingress, policy, selectors
from apps.conversations.models import Conversation, Message
from core.tenancy.scope import tenant_scope
from tests.meta_sim import webhooks
from tests.meta_sim.webhooks import CUSTOMER, PHONE_NUMBER_ID, WABA_ID
from tests.test_conversation_commands import outbound
from tests.test_conversations_dispatch import Counting, dispatch
from tests.test_inbound_whatsapp import broker, channel, contact, crm, fresh_wake, live, real_stack
from tests.test_inventory_stock import race
from tests.test_memberships import ctx
from tests.test_messaging_policy import HOUR, NOW, decide
from tests.test_webhook_ingress import deliver

__all__ = ["broker", "channel", "crm", "fresh_wake", "real_stack"]  # las fixtures, para pytest
pytestmark = pytest.mark.usefixtures("tenant_db", "no_realtime_broker")
STOP, BACK = keywords.OPT_OUT, keywords.OPT_IN


def say(wamid: str, body: str, *, ago: dt.timedelta = dt.timedelta(seconds=5)) -> dict[str, int]:
    """El cliente escribe `body` (hace `ago`) por la cuenta de la organización A."""
    at = int((timezone.now() - ago).timestamp())
    deliver(webhooks.text_message(body, wamid=wamid, at=at))
    return ingress.process()


def seen(org: Any) -> SimpleNamespace:
    with tenant_scope(ctx(org)):
        return SimpleNamespace(
            consents=[
                (c.channel, c.purpose, c.status, c.source) for c in ContactConsent.objects.all()
            ],
            events=list(ContactConsentEvent.objects.order_by("occurred_at", "id")),
            replies=list(Message.objects.filter(sender_type="SYSTEM").order_by("created_at")),
            inbound=list(Message.objects.filter(direction="INBOUND").order_by("created_at")),
            convo=Conversation.objects.first(),
        )


def events(migrator: Any, kind: str) -> int:
    query = "SELECT count(*) FROM outbox_events WHERE event_type = %s"
    return int(migrator.execute(query, [kind]).fetchone()[0])


# ---- Qué es una palabra de baja o de alta ----


@pytest.mark.parametrize(
    ("text", "action"),
    [
        ("STOP", STOP), ("  stop \n", STOP), ("Stop!", STOP), ("¡BAJA!", STOP), ("Cancelár", STOP),
        ("baja 🙏", STOP), ("...stop...", STOP), ("ALTA.", BACK), ("Ａｌｔａ", BACK),
        ("quiero darme de baja", None), ("stop please", None), ("no me des de baja", None),
        ("baja de precio", None), ("s t o p", None), ("stopp", None), ("STOP STOP", None),
        ("st0p", None), ("estop", None), ("bajas", None), ("", None), ("🙏", None),
        ("baja" + " " * 120, None), ("alto", None), ("cancelar pedido", None),
        ("no gracias", None), ("gracias", None), ("si", None), ("S\u200bTOP", None),
    ],
)  # fmt: skip
def test_only_a_message_that_is_exactly_a_keyword_counts(
    orgs: dict[str, Any], text: str, action: str | None
) -> None:
    with tenant_scope(ctx(orgs["A"])) as tenant:
        assert keywords.action_for(tenant, text) == action


# ---- El STOP del entrante ----


def test_a_stop_revokes_once_confirms_once_and_blocks_a_follow_up_but_not_a_reply(
    crm: SimpleNamespace, migrator: Any
) -> None:
    live(crm.a)
    assert say("wamid.1", "Stop") == {"PROCESSED": 1}
    assert say("wamid.1", "Stop") == {}  # Meta repite la entrega: ya está en la ingesta
    again = webhooks.text_message("Stop", wamid="wamid.1")  # el mismo ítem, reclamado dos veces
    assert ingress.route(ingress.Claimed(uuid4(), "message", WABA_ID, PHONE_NUMBER_ID, again, 2))
    found = seen(crm.a)
    assert found.consents == [("WHATSAPP", "ALL_PROACTIVE", "REVOKED", "KEYWORD_STOP")]
    [event], [reply], [stop] = found.events, found.replies, found.inbound
    assert event.evidence == {"message_id": str(stop.pk)} and event.recorded_by_user_id is None
    assert (reply.direction, reply.delivery_status, reply.body) == (
        "OUTBOUND", "QUEUED", inbound.CONFIRMATIONS[STOP],
    )  # fmt: skip
    assert (reply.sender_user_id, reply.policy_decision["purpose"]) == (None, "CUSTOMER_REPLY")
    assert (events(migrator, "consent.revoked"), events(migrator, "message.queued")) == (1, 1)
    with pytest.raises(policy.ConsentRevoked):
        outbound(crm, found.convo, purpose="FOLLOW_UP")
    message, created = outbound(crm, found.convo)  # responder dentro de la ventana sí
    assert created and message.policy_decision["allowed"] is True


def test_an_alta_restores_and_the_history_keeps_both_changes_untouched(
    crm: SimpleNamespace,
) -> None:
    live(crm.a)
    say("wamid.1", "BAJA")
    first = seen(crm.a).events[0]
    say("wamid.2", "alta")
    say("wamid.3", "ALTA!")  # ya estaba de alta: ni evento ni otra confirmación
    late = webhooks.text_message("BAJA", wamid="wamid.1")  # la baja, reclamada otra vez después
    ingress.route(ingress.Claimed(uuid4(), "message", WABA_ID, PHONE_NUMBER_ID, late, 2))
    found = seen(crm.a)
    assert found.consents == [("WHATSAPP", "ALL_PROACTIVE", "GRANTED", "CUSTOMER_MESSAGE")]
    assert [(e.status, e.source) for e in found.events] == [
        ("REVOKED", "KEYWORD_STOP"), ("GRANTED", "CUSTOMER_MESSAGE"),
    ]  # fmt: skip
    assert (found.events[0].pk, found.events[0].evidence) == (first.pk, first.evidence)
    assert [r.body for r in found.replies] == [inbound.CONFIRMATIONS[k] for k in (STOP, BACK)]
    message, created = outbound(crm, found.convo, purpose="FOLLOW_UP")
    assert created and message.policy_decision["purpose"] == "FOLLOW_UP"


def test_a_keyword_inside_a_sentence_changes_nothing(crm: SimpleNamespace, migrator: Any) -> None:
    live(crm.a)
    say("wamid.1", "No me den de baja, solo quiero saber el precio")
    say("wamid.2", "stop por favor")
    found = seen(crm.a)
    assert (found.consents, found.events, found.replies) == ([], [], [])
    assert events(migrator, "consent.revoked") == 0


@pytest.mark.parametrize("case", ["blocked", "old"])
def test_a_blocked_contact_or_a_stop_outside_the_window_is_recorded_without_confirmation(
    crm: SimpleNamespace, case: str
) -> None:
    live(crm.a)
    contact(crm.a, "Ana", "+" + CUSTOMER, is_blocked=case == "blocked")
    say("wamid.1", "stop", ago=dt.timedelta(hours=25 if case == "old" else 0))
    found = seen(crm.a)
    assert found.consents == [("WHATSAPP", "ALL_PROACTIVE", "REVOKED", "KEYWORD_STOP")]
    assert found.replies == []  # el bloqueado no recibe nada; fuera de la ventana no se puede


def test_a_follow_up_queued_before_the_stop_fails_at_dispatch_and_a_reply_still_goes(
    crm: SimpleNamespace,
) -> None:
    live(crm.a)
    say("wamid.1", "Hola")
    convo = seen(crm.a).convo
    follow_up, _ = outbound(crm, convo, purpose="FOLLOW_UP")
    reply, _ = outbound(crm, convo)
    say("wamid.2", "STOP")  # mientras esperaban en la cola
    adapter = Counting()
    failed = dispatch(crm, follow_up, adapter)
    assert (failed.delivery_status, failed.provider_error_code) == ("FAILED", "CONSENT_REVOKED")
    assert adapter.calls == 0 and failed.policy_decision["reason"] == "CONSENT_REVOKED"
    assert dispatch(crm, reply, adapter).delivery_status == "DELIVERED" and adapter.calls == 1


def test_no_log_of_a_stop_carries_the_phone_or_the_text(
    crm: SimpleNamespace, caplog: pytest.LogCaptureFixture
) -> None:
    live(crm.a)
    caplog.set_level(logging.DEBUG)
    say("wamid.1", "Baja")
    for secret in (CUSTOMER, "+" + CUSTOMER, "Cliente de prueba", "Baja", "wamid.1"):
        assert secret not in caplog.text, secret


# ---- La política ----


def test_a_revocation_denies_a_follow_up_and_never_a_reply_inside_the_window() -> None:
    assert decide(revoked=True).allowed  # la respuesta (por omisión) dentro de la ventana
    denied = decide(revoked=True, purpose="FOLLOW_UP")
    assert (denied.reason, denied.as_json()["purpose"]) == ("CONSENT_REVOKED", "FOLLOW_UP")
    assert decide(purpose="FOLLOW_UP").allowed
    assert decide(revoked=True, last_inbound_at=NOW - 24 * HOUR).reason == "WINDOW_CLOSED"
    assert decide(revoked=True, purpose="FOLLOW_UP", contact_blocked=True).reason == (
        "CONTACT_BLOCKED"
    )
    assert decide(platform="SANDBOX", revoked=True, purpose="FOLLOW_UP").reason == (
        "CONSENT_REVOKED"
    )
    with pytest.raises(ValueError, match="propósito"):
        decide(purpose="MARKETING")


@pytest.mark.parametrize(
    ("via", "purpose", "blocks"),
    [("WHATSAPP", "ALL_PROACTIVE", True), ("ANY", "FOLLOW_UP", True),
     ("EMAIL", "ALL_PROACTIVE", False), ("WHATSAPP", "MARKETING", False)],
)  # fmt: skip
def test_the_decision_reads_the_revocation_of_this_channel_and_purpose(
    crm: SimpleNamespace, via: str, purpose: str, blocks: bool
) -> None:
    live(crm.a)
    say("wamid.1", "Hola")
    convo = seen(crm.a).convo
    with tenant_scope(ctx(crm.a)) as tenant:
        revoke_consent(
            tenant, contact_id=convo.contact_id, channel=via, purpose=purpose,
            source="AGENT_RECORDED",
        )  # fmt: skip
        decision = selectors.messaging_decision(convo, purpose="FOLLOW_UP")
        assert selectors.messaging_decision(convo).allowed  # la respuesta, siempre
    assert decision.reason == ("CONSENT_REVOKED" if blocks else None)
    with pytest.raises(ValueError, match="propósito"):
        outbound(crm, convo, purpose="MARKETING")


# ---- Configurar las palabras ----


def configure(org: Any, who: Any, opt_out: Any, opt_in: Any) -> dict[str, str]:
    with tenant_scope(ctx(org, who)) as tenant:
        return keywords.set_keywords(tenant, opt_out=opt_out, opt_in=opt_in)


def test_the_owner_replaces_the_keywords_and_the_defaults_stop_counting(
    crm: SimpleNamespace, migrator: Any
) -> None:
    wanted = configure(crm.a, crm.ana, [" Salir! ", "BAJA"], ["Volvér"])
    assert wanted == {"salir": STOP, "baja": STOP, "volver": BACK}
    assert configure(crm.a, crm.ana, ["salir", "baja"], ["volver"]) == wanted  # nada cambia
    with tenant_scope(ctx(crm.a)) as tenant:
        assert keywords.keywords(tenant) == wanted
        assert [keywords.action_for(tenant, t) for t in ("STOP", "salir", "Volver")] == [
            None, STOP, BACK,
        ]  # fmt: skip
    [(changes,)] = migrator.execute(
        "SELECT changes FROM audit_logs WHERE action = 'consent_keywords.updated'"
    ).fetchall()
    assert changes == {"keywords": [keywords.DEFAULTS, wanted]}


@pytest.mark.parametrize(
    ("opt_out", "opt_in"),
    [([], ["alta"]), (["baja"], ["Baja"]), (["baja", "BAJA"], []), (["a-b"], []),
     (["x" * 31], []), (["uno dos tres cuatro"], []), ([1], []), ("baja", []),
     ([f"w{i}" for i in range(11)], []), (["🙏"], [])],
)  # fmt: skip
def test_bad_keywords_are_refused_without_writing(
    crm: SimpleNamespace, opt_out: Any, opt_in: Any
) -> None:
    with pytest.raises(ValueError):
        configure(crm.a, crm.ana, opt_out, opt_in)
    with tenant_scope(ctx(crm.a)):
        assert not ConsentKeyword.objects.exists()


def test_without_channels_manage_nothing_is_configured(crm: SimpleNamespace) -> None:
    for who in (crm.bea, crm.vera, None):  # sin rol, vendedora y el sistema
        with pytest.raises(AccessDenied):
            configure(crm.a, who, ["salir"], [])
    with tenant_scope(ctx(crm.a)):
        assert not ConsentKeyword.objects.exists()


def test_the_keywords_of_one_organization_never_reach_another(
    crm: SimpleNamespace, migrator: Any
) -> None:
    live(crm.a)
    other = contact(crm.b, "Ajena", "+" + CUSTOMER)  # el mismo teléfono en la otra organización
    with tenant_scope(ctx(crm.b)):
        ConsentKeyword.objects.create(keyword="salir", action=STOP)
    with tenant_scope(ctx(crm.a)), connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM consent_keywords")  # RLS, no solo el manager
        assert cursor.fetchone() == (0,)
    say("wamid.1", "salir")  # para A no es una palabra: valen sus palabras por defecto
    say("wamid.2", "stop")
    assert seen(crm.a).consents == [("WHATSAPP", "ALL_PROACTIVE", "REVOKED", "KEYWORD_STOP")]
    assert seen(crm.b).consents == [] and seen(crm.b).inbound == []
    with tenant_scope(ctx(crm.b)):
        assert Contact.objects.filter(pk=other.pk, is_blocked=False).exists()


def test_the_table_only_holds_normalized_keywords(crm: SimpleNamespace) -> None:
    with pytest.raises(IntegrityError, match="consent_keywords_keyword_ck"):
        with tenant_scope(ctx(crm.a)):
            ConsentKeyword.objects.create(keyword="STOP", action=STOP)


# ---- Revisión de #691: el alta solo levanta una baja de palabra ----


def test_an_alta_without_a_prior_baja_does_nothing_at_all(
    crm: SimpleNamespace, migrator: Any
) -> None:
    live(crm.a)
    assert say("wamid.1", "ALTA") == {"PROCESSED": 1}  # y el ítem se cierra bien
    found = seen(crm.a)
    assert (found.consents, found.events, found.replies) == ([], [], [])
    assert (events(migrator, "consent.granted"), events(migrator, "message.queued")) == (0, 0)


def test_an_alta_never_lifts_a_revocation_that_was_not_a_keyword(
    crm: SimpleNamespace, migrator: Any
) -> None:
    live(crm.a)
    say("wamid.1", "Hola")
    convo = seen(crm.a).convo
    with tenant_scope(ctx(crm.a)) as tenant:
        revoke_consent(
            tenant, contact_id=convo.contact_id, channel="WHATSAPP", purpose="ALL_PROACTIVE",
            source="AGENT_RECORDED",
        )  # fmt: skip
    say("wamid.2", "alta")
    found = seen(crm.a)
    assert found.consents == [("WHATSAPP", "ALL_PROACTIVE", "REVOKED", "AGENT_RECORDED")]
    assert len(found.events) == 1 and found.replies == []
    assert events(migrator, "consent.granted") == 0


def test_a_zero_width_character_is_cleaned_before_the_match_so_it_still_counts(
    crm: SimpleNamespace,
) -> None:
    live(crm.a)
    say("wamid.1", "S\u200bTOP")  # `clean_text` lo quita antes: «STOP»
    assert seen(crm.a).consents == [("WHATSAPP", "ALL_PROACTIVE", "REVOKED", "KEYWORD_STOP")]


# ---- Revisión de #691: lo que no puede ser una palabra ----


@pytest.mark.parametrize(
    "word",
    ["si", "sí", "no", "ok", "okay", "hola", "gracias", "vale", "listo", "bueno", "yes", "hello",
     "thanks", "Thank You", "no gracias", "dale", "ya", "abc", "Muchas Gracias", "buenas tardes"],
)  # fmt: skip
@pytest.mark.parametrize("kind", [0, 1])
def test_an_ordinary_reply_or_a_too_short_word_is_refused_for_baja_and_alta(
    crm: SimpleNamespace, word: str, kind: int
) -> None:
    opt_out, opt_in = (["salir"], [word]) if kind else ([word, "salir"], [])
    with pytest.raises(ValueError, match="demasiado"):
        configure(crm.a, crm.ana, opt_out, opt_in)


def test_the_default_words_and_a_four_letter_word_pass_the_validation(
    crm: SimpleNamespace,
) -> None:
    defaults = configure(crm.a, crm.ana, ["STOP", "baja", "cancelar"], ["alta"])
    assert defaults == keywords.DEFAULTS
    assert configure(crm.a, crm.ana, ["abcd"], ["wxyz"]) == {"abcd": STOP, "wxyz": BACK}


# ---- Revisión de #691: fallar cerrado y serializar con el STOP ----


def test_a_send_must_say_what_it_is_for(crm: SimpleNamespace) -> None:
    live(crm.a)
    say("wamid.1", "Hola")
    convo = seen(crm.a).convo
    with pytest.raises(TypeError, match="purpose"), tenant_scope(ctx(crm.a, crm.ana)) as tenant:
        commands.record_outbound(  # type: ignore[call-arg]
            tenant, conversation_id=convo.pk, client_message_id=uuid4(), body="x"
        )


@pytest.mark.parametrize("stored", [None, {"allowed": True, "reason": None}])
def test_a_queued_message_without_a_purpose_is_checked_as_the_strictest(
    crm: SimpleNamespace, stored: Any
) -> None:
    live(crm.a)
    say("wamid.1", "Hola")
    convo = seen(crm.a).convo
    queued, _ = outbound(crm, convo)  # una respuesta de antes de F7-08b, sin propósito guardado
    with tenant_scope(ctx(crm.a)):
        Message.objects.filter(pk=queued.pk).update(policy_decision=stored)
    say("wamid.2", "STOP")
    adapter = Counting()
    failed = dispatch(crm, queued, adapter)
    assert (failed.delivery_status, failed.provider_error_code) == ("FAILED", "CONSENT_REVOKED")
    assert adapter.calls == 0


def test_claiming_waits_for_a_stop_that_is_being_registered(crm: SimpleNamespace) -> None:
    live(crm.a)
    say("wamid.1", "Hola")
    convo = seen(crm.a).convo
    queued, _ = outbound(crm, convo, purpose="FOLLOW_UP")

    def stop(tenant: Any) -> Any:
        return revoke_consent(
            tenant, contact_id=convo.contact_id, channel="WHATSAPP", purpose="ALL_PROACTIVE",
            source="KEYWORD_STOP",
        )  # fmt: skip

    def claim(tenant: Any) -> Any:
        return commands.claim_outbound(tenant, message_id=queued.pk)

    first, second = race(crm.a, crm.ana, [stop, claim])
    assert first[1] is True and second is None  # esperó, vio la baja y no reclamó
    with tenant_scope(ctx(crm.a)):
        found = Message.objects.get(pk=queued.pk)
    assert (found.delivery_status, found.provider_error_code) == ("FAILED", "CONSENT_REVOKED")
