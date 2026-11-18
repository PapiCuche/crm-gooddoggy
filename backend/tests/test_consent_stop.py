"""F7-08b: palabras de baja y de alta (`consent_keywords`, `contacts.keywords`), el STOP del
entrante (`inbound`) y la política de consentimiento (ADR-010 §2.7). PostgreSQL real con `crm_app`:
variantes y frases que no cuentan, la entrega repetida, el seguimiento bloqueado y la respuesta no,
el alta que lo restablece, el despachador, los permisos y el aislamiento entre organizaciones.
F7b-05d: las palabras se aplican por la hora del proveedor (`keyword_at`), no por la llegada."""

import datetime as dt
import itertools
import logging
import threading
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from django.core.management import call_command
from django.db import IntegrityError, connection
from django.utils import timezone

from apps.access.selectors import AccessDenied
from apps.contacts import keywords
from apps.contacts.consents import grant_consent, revoke_consent
from apps.contacts.models import ConsentKeyword, ContactConsent, ContactConsentEvent
from apps.contacts.selectors import is_revoked
from apps.conversations import commands, inbound, ingress, policy, selectors
from apps.conversations.models import Conversation, Message
from core.tenancy.scope import tenant_scope
from tests.meta_sim import webhooks
from tests.meta_sim.webhooks import CUSTOMER, PHONE_NUMBER_ID, WABA_ID
from tests.test_contact_consents import on_a_contact
from tests.test_conversation_commands import outbound
from tests.test_conversations_dispatch import Counting, dispatch
from tests.test_inbound_whatsapp import broker, channel, contact, crm, fresh_wake, live, real_stack
from tests.test_inventory_stock import race
from tests.test_memberships import ctx
from tests.test_messaging_policy import HOUR, NOW, decide
from tests.test_pipelines_tables import as_migrator
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


def test_a_stop_revokes_once_confirms_once_and_blocks_a_follow_up_but_not_a_reply(
    crm: SimpleNamespace, migrator: Any
) -> None:
    live(crm.a)
    assert say("wamid.1", "S\u200btop") == {"PROCESSED": 1}  # `clean_text` quita el invisible
    assert say("wamid.1", "Stop") == {}  # Meta repite la entrega: ya está en la ingesta
    again = webhooks.text_message("Stop", wamid="wamid.1")  # el mismo ítem, reclamado dos veces
    assert ingress.route(ingress.Claimed(uuid4(), "message", WABA_ID, PHONE_NUMBER_ID, again, 2))
    found = seen(crm.a)
    assert found.consents == [("WHATSAPP", "ALL_PROACTIVE", "REVOKED", "KEYWORD_STOP")]
    [event], [reply], [stop] = found.events, found.replies, found.inbound
    assert event.evidence == {"message_id": str(stop.pk)} and event.recorded_by_user_id is None
    assert (reply.direction, reply.delivery_status, reply.body) == (
        "OUTBOUND", "QUEUED", inbound.CONFIRMATIONS["WHATSAPP"][STOP],
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
    say("wamid.1", "BAJA", ago=dt.timedelta(seconds=30))
    first = seen(crm.a).events[0]
    say("wamid.2", "alta", ago=dt.timedelta(seconds=20))
    say("wamid.3", "ALTA!")  # ya estaba de alta: ni evento ni otra confirmación
    late = webhooks.text_message("BAJA", wamid="wamid.1")  # la baja, reclamada otra vez después
    ingress.route(ingress.Claimed(uuid4(), "message", WABA_ID, PHONE_NUMBER_ID, late, 2))
    found = seen(crm.a)
    assert found.consents == [("WHATSAPP", "ALL_PROACTIVE", "GRANTED", "CUSTOMER_MESSAGE")]
    assert [(e.status, e.source) for e in found.events] == [
        ("REVOKED", "KEYWORD_STOP"), ("GRANTED", "CUSTOMER_MESSAGE"),
    ]  # fmt: skip
    assert (found.events[0].pk, found.events[0].evidence) == (first.pk, first.evidence)
    assert [r.body for r in found.replies] == [
        inbound.CONFIRMATIONS["WHATSAPP"][k] for k in (STOP, BACK)
    ]
    message, created = outbound(crm, found.convo, purpose="FOLLOW_UP")
    assert created and message.policy_decision["purpose"] == "FOLLOW_UP"


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


def test_no_log_of_a_stop_carries_the_phone_or_the_text(
    crm: SimpleNamespace, caplog: pytest.LogCaptureFixture
) -> None:
    live(crm.a)
    caplog.set_level(logging.DEBUG)
    say("wamid.1", "Baja")
    for secret in (CUSTOMER, "+" + CUSTOMER, "Cliente de prueba", "Baja", "wamid.1"):
        assert secret not in caplog.text, secret


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


@pytest.mark.parametrize(("via", "blocks"), [("WHATSAPP", True), ("ANY", True), ("EMAIL", False)])
def test_the_decision_reads_the_revocation_of_this_channel(
    crm: SimpleNamespace, via: str, blocks: bool
) -> None:
    live(crm.a)
    say("wamid.1", "Hola")
    convo = seen(crm.a).convo
    with tenant_scope(ctx(crm.a)) as tenant:
        revoke_consent(
            tenant, contact_id=convo.contact_id, channel=via, purpose="ALL_PROACTIVE",
            source="AGENT_RECORDED",
        )  # fmt: skip
        decision = selectors.messaging_decision(convo, purpose="FOLLOW_UP")
        assert selectors.messaging_decision(convo).allowed  # la respuesta, siempre
    assert decision.reason == ("CONSENT_REVOKED" if blocks else None)


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


def test_the_keywords_of_one_organization_never_reach_another(
    crm: SimpleNamespace, migrator: Any
) -> None:
    live(crm.a)
    contact(crm.b, "Ajena", "+" + CUSTOMER)  # el mismo teléfono en la otra organización
    with tenant_scope(ctx(crm.b)):
        ConsentKeyword.objects.create(keyword="salir", action=STOP)
    with tenant_scope(ctx(crm.a)), connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM consent_keywords")  # RLS, no solo el manager
        assert cursor.fetchone() == (0,)
    say("wamid.1", "salir")  # para A no es una palabra: valen sus palabras por defecto
    say("wamid.2", "stop")
    assert seen(crm.a).consents == [("WHATSAPP", "ALL_PROACTIVE", "REVOKED", "KEYWORD_STOP")]
    assert seen(crm.b).consents == [] and seen(crm.b).inbound == []


@pytest.mark.parametrize(("word", "check"), [("STOP", "keyword"), ("ok", "length")])
def test_the_table_only_holds_normalized_keywords_of_four_characters(
    crm: SimpleNamespace, word: str, check: str
) -> None:
    with pytest.raises(IntegrityError, match=f"consent_keywords_{check}_ck"):
        with tenant_scope(ctx(crm.a)):
            ConsentKeyword.objects.create(keyword=word, action=STOP)


def test_a_stored_keyword_that_breaks_the_rules_is_ignored(
    crm: SimpleNamespace, migrator: Any
) -> None:
    """Filas de antes de las reglas (la base de desarrollo del primer corte): no valen."""
    migrator.execute("ALTER TABLE consent_keywords DROP CONSTRAINT consent_keywords_length_ck")
    try:
        with tenant_scope(ctx(crm.a)) as tenant:
            for word in ("ok", "gracias", "salir"):
                ConsentKeyword.objects.create(keyword=word, action=STOP)
            assert [keywords.action_for(tenant, w) for w in ("ok", "gracias", "salir")] == [
                None, None, STOP,
            ]  # fmt: skip
    finally:
        migrator.execute("DELETE FROM consent_keywords")
        migrator.execute(
            "ALTER TABLE consent_keywords ADD CONSTRAINT consent_keywords_length_ck "
            "CHECK (char_length(keyword) >= 4)"
        )


@pytest.mark.parametrize("body", ["No me den de baja, solo quiero el precio", "stop por favor"])
def test_a_keyword_inside_a_sentence_does_nothing_at_all(
    crm: SimpleNamespace, migrator: Any, body: str
) -> None:
    live(crm.a)
    assert say("wamid.1", body) == {"PROCESSED": 1}  # y el ítem se cierra bien
    found = seen(crm.a)
    assert (found.consents, found.events, found.replies) == ([], [], [])
    assert [events(migrator, k) for k in ("consent.granted", "message.queued")] == [0, 0]


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
    with tenant_scope(ctx(crm.a)) as tenant:  # pero su hora cuenta (F7b-05e)
        assert ContactConsent.objects.get().keyword_at == found.inbound[1].provider_timestamp
        grant_consent(
            tenant, contact_id=convo.contact_id, channel="WHATSAPP", purpose="ALL_PROACTIVE",
            source="AGENT_RECORDED",
        )  # fmt: skip
    say("wamid.0", "BAJA", ago=dt.timedelta(seconds=60))  # más vieja que el ALTA: no revoca
    assert seen(crm.a).consents == [("WHATSAPP", "ALL_PROACTIVE", "GRANTED", "AGENT_RECORDED")]


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


@pytest.mark.parametrize("words", [("ALTA", "BAJA"), ("BAJA", "ALTA")])
def test_keywords_delivered_out_of_order_follow_the_time_the_customer_wrote_them(
    crm: SimpleNamespace, words: tuple[str, str]
) -> None:
    live(crm.a)
    say("wamid.1", "BAJA", ago=dt.timedelta(seconds=60))  # ya estaba de baja
    first, last = words  # el cliente escribe `first` y luego `last`; Meta los entrega al revés
    say("wamid.3", last, ago=dt.timedelta(seconds=10))
    say("wamid.2", first, ago=dt.timedelta(seconds=20))
    found = seen(crm.a)
    final = ("REVOKED", "KEYWORD_STOP") if last == "BAJA" else ("GRANTED", "CUSTOMER_MESSAGE")
    assert found.consents == [("WHATSAPP", "ALL_PROACTIVE", *final)]
    assert len(found.events) == (1 if last == "BAJA" else 2)  # la vieja no deja evento
    with tenant_scope(ctx(crm.a)):
        row = ContactConsent.objects.get()
    assert row.keyword_at == found.inbound[1].provider_timestamp  # la de `last`


def test_an_older_baja_never_moves_the_keyword_time_back(crm: SimpleNamespace) -> None:
    live(crm.a)  # BAJA t1, ALTA t2 y BAJA t3, entregados t3, t1, t2: queda la de t3
    say("wamid.3", "BAJA", ago=dt.timedelta(seconds=10))
    say("wamid.1", "BAJA", ago=dt.timedelta(seconds=30))
    say("wamid.2", "ALTA", ago=dt.timedelta(seconds=20))
    found = seen(crm.a)
    assert found.consents == [("WHATSAPP", "ALL_PROACTIVE", "REVOKED", "KEYWORD_STOP")]
    with tenant_scope(ctx(crm.a)):
        row = ContactConsent.objects.get()
    assert row.keyword_at == found.inbound[0].provider_timestamp  # la de t3


def test_an_alta_that_changes_nothing_still_keeps_an_older_baja_out(crm: SimpleNamespace) -> None:
    live(crm.a)
    say("wamid.1", "Hola", ago=dt.timedelta(seconds=60))
    with tenant_scope(ctx(crm.a)) as tenant:  # ya está de alta, por otra vía
        grant_consent(
            tenant, contact_id=seen(crm.a).convo.contact_id, channel="WHATSAPP",
            purpose="ALL_PROACTIVE", source="AGENT_RECORDED",
        )  # fmt: skip
    say("wamid.3", "ALTA", ago=dt.timedelta(seconds=10))  # t4: no cambia nada, pero cuenta
    say("wamid.2", "BAJA", ago=dt.timedelta(seconds=20))  # t3, entregada tarde
    found = seen(crm.a)
    assert found.consents == [("WHATSAPP", "ALL_PROACTIVE", "GRANTED", "AGENT_RECORDED")]
    assert found.replies == []


def ago(seconds: int) -> int:
    return int((timezone.now() - dt.timedelta(seconds=seconds)).timestamp())


def two_batches(migrator: Any, monkeypatch: pytest.MonkeyPatch, first: str) -> list[str]:
    """Dos workers (`--concurrency 2`) reclaman cada uno su ítem antes de que ninguno lo procese.
    El del ítem cuyo payload lleva `first` escribe su palabra y retiene la transacción hasta que el
    otro espera la fila del contacto; entonces confirma y el otro sigue (F7b-05e). F7b-05f: solo en
    esta prueba se quita el bloqueo de aviso por cliente, que de otro modo lo frenaría antes."""
    claimed = threading.Barrier(2, timeout=10)
    wrote, go, release = threading.Event(), threading.Event(), threading.Event()
    leader: list[threading.Thread] = []
    outcome: list[str] = []
    real = {name: getattr(inbound, name) for name in ("keyword_stop", "lift_keyword_stop")}

    def holding(name: str) -> Any:
        def run(*args: Any, **kwargs: Any) -> bool:
            done = bool(real[name](*args, **kwargs))
            if threading.current_thread() in leader:
                wrote.set()
                release.wait(timeout=10)
            return done

        return run

    for name in real:
        monkeypatch.setattr(inbound, name, holding(name))
    monkeypatch.setattr(inbound, "_lock", lambda alias, key: None)  # solo aquí: ver el docstring

    def worker() -> None:
        try:
            [item] = ingress.claim(1)
            if first in str(item.payload):  # su id de Meta (`wamid` o `mid`)
                leader.append(threading.current_thread())
            claimed.wait()  # los dos lotes ya están reclamados
            if threading.current_thread() not in leader:
                assert go.wait(timeout=10)
            outcome.append(ingress.handle(item))
        except Exception as error:  # noqa: BLE001 — lo comprueba quien llama
            outcome.append(type(error).__name__)
        finally:
            wrote.set()
            connection.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    assert wrote.wait(timeout=10)
    go.set()
    try:
        waited = on_a_contact(migrator)  # se observa la espera en la fila del contacto
    finally:
        release.set()
        for thread in threads:
            thread.join(timeout=10)
    assert not any(t.is_alive() for t in threads) and len(leader) == 1, outcome
    assert waited, "el segundo lote no esperó al primero en la fila del contacto"
    return outcome


TIMES = [  # (más vieja, más nueva, la que se confirma antes, segundos entre las dos)
    ("BAJA", "ALTA", "new", 10), ("BAJA", "ALTA", "old", 10), ("ALTA", "BAJA", "new", 10),
    ("ALTA", "BAJA", "old", 10), ("BAJA", "ALTA", "new", 0), ("BAJA", "ALTA", "old", 0),
]  # fmt: skip


def final(newer: str, gap: int) -> tuple[str, str]:
    """La última palabra por la hora de Meta; en un empate, la baja (F7b-05e)."""
    if newer == "BAJA" or gap == 0:
        return ("REVOKED", "KEYWORD_STOP")
    return ("GRANTED", "CUSTOMER_MESSAGE")


@pytest.mark.parametrize(("older", "newer", "first", "gap"), TIMES)
def test_two_batches_at_once_follow_the_time_the_customer_wrote_them(
    crm: SimpleNamespace, migrator: Any, monkeypatch: pytest.MonkeyPatch,
    older: str, newer: str, first: str, gap: int,
) -> None:  # fmt: skip
    live(crm.a)
    say("wamid.0", "Hola", ago=dt.timedelta(seconds=60))
    base = ago(20)  # una sola lectura del reloj: el empate es exacto
    deliver(webhooks.text_message(older, wamid="wamid.old", at=base))
    deliver(webhooks.text_message(newer, wamid="wamid.new", at=base + gap))
    assert two_batches(migrator, monkeypatch, f"wamid.{first}") == ["PROCESSED", "PROCESSED"]
    found = seen(crm.a)
    assert found.consents == [("WHATSAPP", "ALL_PROACTIVE", *final(newer, gap))]
    with tenant_scope(ctx(crm.a)):
        revoked = is_revoked(found.convo.contact_id, "WHATSAPP", "FOLLOW_UP")
    assert revoked is (final(newer, gap)[0] == "REVOKED")


@pytest.mark.parametrize("skew", [30, -30])  # nuestro reloj detrás del de Meta, o delante
@pytest.mark.parametrize(("newer", "gap"), [("BAJA", 10), ("ALTA", 10), ("BAJA", 0)])
def test_with_our_clock_off_meta_the_newest_word_by_meta_still_wins(
    crm: SimpleNamespace, newer: str, gap: int, skew: int
) -> None:
    """F7b-05f: la más nueva se procesa antes; la vieja, después, con nuestro reloj más adelante."""
    live(crm.a)
    say("wamid.0", "Hola", ago=dt.timedelta(seconds=60))
    older, base = ("BAJA" if newer == "ALTA" else "ALTA"), ago(-skew)  # la hora de la más nueva
    for said, wamid, at in ((newer, "wamid.new", base), (older, "wamid.old", base - gap)):
        deliver(webhooks.text_message(said, wamid=wamid, at=at))
        assert ingress.process() == {"PROCESSED": 1}
    assert seen(crm.a).consents == [("WHATSAPP", "ALL_PROACTIVE", *final(newer, gap))]


def test_a_meta_time_ahead_of_our_clock_orders_the_words_but_never_moves_the_window(
    crm: SimpleNamespace,
) -> None:
    live(crm.a)
    at = ago(-30)  # Meta va 30 s por delante
    deliver(webhooks.text_message("BAJA", wamid="wamid.1", at=at))
    ingress.process()
    found, now = seen(crm.a), timezone.now()
    with tenant_scope(ctx(crm.a)):
        row = ContactConsent.objects.get()
    assert row.keyword_at == dt.datetime.fromtimestamp(at, dt.UTC)  # la de Meta, sin acotar
    assert found.convo.last_inbound_at <= now  # la ventana de 24 h, acotada a «ahora» como antes
    assert found.inbound[0].provider_timestamp <= now


@pytest.mark.parametrize("newer", ["ALTA", "BAJA"])
def test_one_delivery_out_of_order_follows_the_time_of_each_word(
    crm: SimpleNamespace, newer: str
) -> None:
    live(crm.a)
    older = "BAJA" if newer == "ALTA" else "ALTA"
    deliver(  # `store` guarda por clave: la más nueva (`wamid.A`) se procesa primero
        webhooks.text_message(newer, wamid="wamid.A", at=ago(10)),
        webhooks.text_message(older, wamid="wamid.B", at=ago(20)),
    )
    assert ingress.process() == {"PROCESSED": 2}
    found = seen(crm.a)
    assert [m.body for m in found.inbound] == [newer, older]  # de verdad, al revés
    assert found.consents == [("WHATSAPP", "ALL_PROACTIVE", *final(newer, 10))]
    assert len(found.events) == len(found.replies) == 1  # la vieja no cambia ni confirma


def test_a_lone_alta_grants_with_its_time_and_an_older_baja_delivered_later_stays_out(
    crm: SimpleNamespace, migrator: Any
) -> None:
    live(crm.a)
    say("wamid.2", "ALTA", ago=dt.timedelta(seconds=10))
    found = seen(crm.a)
    assert found.consents == [("WHATSAPP", "ALL_PROACTIVE", "GRANTED", "CUSTOMER_MESSAGE")]
    assert [r.body for r in found.replies] == [inbound.CONFIRMATIONS["WHATSAPP"][BACK]]
    assert events(migrator, "consent.granted") == 1
    with tenant_scope(ctx(crm.a)):
        assert ContactConsent.objects.get().keyword_at == found.inbound[0].provider_timestamp
    say("wamid.1", "BAJA", ago=dt.timedelta(seconds=20))
    found = seen(crm.a)
    assert found.consents == [("WHATSAPP", "ALL_PROACTIVE", "GRANTED", "CUSTOMER_MESSAGE")]
    assert (len(found.events), len(found.replies)) == (1, 1)
    assert events(migrator, "consent.revoked") == 0


def test_a_lone_alta_permits_nothing_that_no_row_did_not_already_permit(
    crm: SimpleNamespace, settings: Any
) -> None:
    live(crm.a)
    say("wamid.1", "Hola", ago=dt.timedelta(seconds=60))

    def grid() -> list[Any]:
        convo, now = seen(crm.a).convo, timezone.now()
        found = []
        with tenant_scope(ctx(crm.a)):
            for approved, last, purpose, sender in itertools.product(
                (False, True), (now - HOUR, now - 30 * HOUR), policy.PURPOSES, ("SYSTEM", "USER")
            ):
                settings.META_HUMAN_AGENT_APPROVED = approved
                convo.last_inbound_at = last
                decision = selectors.messaging_decision(convo, purpose=purpose, sender=sender)
                found.append((decision.allowed, decision.reason, decision.tagged, decision.purpose))
        return found

    before = grid()
    say("wamid.2", "ALTA", ago=dt.timedelta(seconds=30))
    assert seen(crm.a).consents == [("WHATSAPP", "ALL_PROACTIVE", "GRANTED", "CUSTOMER_MESSAGE")]
    assert grid() == before  # GRANTED no abre nada que sin fila estuviera cerrado


def spied(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, ...]]:
    """Cada consulta de la baja que hace `selectors.messaging_decision`."""
    calls: list[tuple[Any, ...]] = []

    def spy(*args: Any, **kwargs: Any) -> bool:
        calls.append(args)
        return is_revoked(*args, **kwargs)

    monkeypatch.setattr("apps.conversations.selectors.is_revoked", spy)
    return calls


def test_a_whatsapp_reply_never_reads_the_consent(
    crm: SimpleNamespace, settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = spied(monkeypatch)
    settings.META_HUMAN_AGENT_APPROVED = True  # WhatsApp no tiene la etiqueta
    live(crm.a)
    say("wamid.1", "Hola", ago=dt.timedelta(hours=30))
    with pytest.raises(policy.WindowClosed):
        outbound(crm, seen(crm.a).convo)
    say("wamid.2", "Hola")
    assert outbound(crm, seen(crm.a).convo)[1] is True
    assert calls == []


def test_a_keyword_without_the_provider_time_keeps_the_order_of_arrival(
    crm: SimpleNamespace,
) -> None:
    live(crm.a)
    say("wamid.1", "BAJA", ago=dt.timedelta(seconds=10))
    alta = webhooks.text_message("ALTA", wamid="wamid.2")
    del alta["timestamp"]  # sin la hora del proveedor: cuenta la llegada, como antes
    deliver(alta)
    ingress.process()
    found = seen(crm.a)
    assert found.consents == [("WHATSAPP", "ALL_PROACTIVE", "GRANTED", "CUSTOMER_MESSAGE")]


def test_the_keyword_time_migration_goes_back_and_forward(
    crm: SimpleNamespace, migrator: Any
) -> None:
    live(crm.a)
    say("wamid.1", "BAJA")
    column = (
        "SELECT has_column_privilege('crm_app', 'contact_consents', 'keyword_at', 'UPDATE')"
        " FROM information_schema.columns WHERE table_name = 'contact_consents'"
        " AND column_name = 'keyword_at'"
    )
    assert migrator.execute(column).fetchall() == [(True,)]
    with as_migrator():
        call_command("migrate", "contacts", "0008", verbosity=0)
    assert migrator.execute(column).fetchall() == []
    kept = "SELECT status, source FROM contact_consents"
    assert migrator.execute(kept).fetchall() == [("REVOKED", "KEYWORD_STOP")]  # la baja se queda
    with as_migrator():
        call_command("migrate", verbosity=0)
    assert migrator.execute(column).fetchall() == [(True,)]
    say("wamid.2", "ALTA")  # una fila sin `keyword_at` (anterior) se levanta como siempre
    assert seen(crm.a).consents == [("WHATSAPP", "ALL_PROACTIVE", "GRANTED", "CUSTOMER_MESSAGE")]


def test_the_migrations_go_back_and_forward(migrator: Any) -> None:
    table = "SELECT to_regclass('public.consent_keywords') IS NOT NULL"
    with as_migrator():
        call_command("migrate", "contacts", "0007", verbosity=0)
        call_command("migrate", "conversations", "0017", verbosity=0)
    assert migrator.execute(table).fetchone() == (False,)
    with as_migrator():
        call_command("migrate", verbosity=0)
    assert migrator.execute(table).fetchone() == (True,)
    update = "SELECT has_table_privilege('crm_app', 'consent_keywords', 'UPDATE')"
    assert migrator.execute(update).fetchone() == (False,)
