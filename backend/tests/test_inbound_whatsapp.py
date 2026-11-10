"""F7-04: el mensaje de texto entrante de WhatsApp llega al Inbox (`apps.conversations.inbound`,
migración `0010`, ADR-029 §5). De la entrega firmada del simulador de Meta al webhook, la ingesta
y el procesador, y de ahí al contacto, la conversación y el mensaje que ve la API del Inbox.
PostgreSQL real con `crm_app`, también con hilos a la vez."""

import datetime as dt
import json
import logging
import threading
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from django.core.management import call_command
from django.db import IntegrityError, connection
from django.utils import timezone

from apps.contacts.models import Contact
from apps.conversations import commands, inbound, ingress, meta_webhook
from apps.conversations.models import ChannelAccount, Conversation, Message
from core.outbox.publisher import publish_outbox
from core.tenancy.context import ActorType, TenantContext
from core.tenancy.scope import tenant_scope
from tests.meta_sim import webhooks
from tests.meta_sim.webhooks import CUSTOMER, PHONE_NUMBER_ID, WABA_ID
from tests.test_contacts_api import crm, real_stack
from tests.test_conversations_api import BASE
from tests.test_memberships import ctx
from tests.test_meta_webhook import channel
from tests.test_pipelines_tables import as_migrator
from tests.test_self_context import signed
from tests.test_tenant_task_signature import broker
from tests.test_webhook_ingress import deliver, put, rows
from tests.test_whatsapp_account import credential, make

__all__ = ["broker", "channel", "crm", "real_stack"]  # las fixtures, para pytest
pytestmark = pytest.mark.usefixtures("tenant_db")
DB = psycopg.Connection[Any]
PRIVATE = "Mi DNI es 12345678"  # noqa: S105 — texto de prueba: nunca debe llegar a un log


@pytest.fixture(autouse=True)
def fresh_wake(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ingress, "_last_wake", 0.0)


def snap(org: UUID) -> SimpleNamespace:
    with tenant_scope(ctx(org)):
        return SimpleNamespace(
            contacts=list(  # los de la fixture `crm` no tienen teléfono
                Contact.objects.filter(primary_phone_e164__isnull=False).order_by("created_at")
            ),
            conversations=list(Conversation.objects.order_by("created_at")),
            messages=list(Message.objects.order_by("created_at")),
        )


def run(*messages: dict[str, Any], **kw: Any) -> dict[str, int]:
    deliver(*messages, **kw)
    return ingress.process()


def live(org: UUID, **over: Any) -> ChannelAccount:
    """Una cuenta como la deja F7-02c: credencial y comprobación de salud, `CONNECTED`."""
    fields = {"status": "CONNECTED", "credential_id": credential(org)}
    return make(org, **fields | {"last_healthcheck_at": timezone.now()} | over)


def message(wamid: str, body: str = "Hola", **over: Any) -> dict[str, Any]:
    return webhooks.text_message(body, wamid=wamid) | over


# ---- Funciones puras: el saneo, el teléfono, el instante ----


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ("Hola", "Hola"),
        ("a\r\nb\rc", "a\nb\nc"),
        ("a\tb\nc", "a\tb\nc"),  # el tabulador y el salto de línea son del cliente
        ("a\x00b\x07c\x1bd\x7fe", "abcde"),  # controles
        ("a​b⁠c﻿d­e", "abcde"),  # espacio de ancho cero, unión de palabras…
        ("a‮b⁦c⁩d", "abcd"),  # controles de dirección: spoofing visual
        ("a‎b‏c", "a‎b‏c"),  # LRM y RLM sí: texto árabe y hebreo
        ("ab\U000e0041\U000e007fcd", "abcd"),  # Tags U+E0000–E007F: inyección de instrucciones
        ("a\U000e0000b\U000e0002c\U000e001fd", "abcd"),  # los puntos sin asignar del bloque Tags
        ("a\ud800b", "ab"),  # sustituto suelto
        ("👨‍👩‍👧", "👨‍👩‍👧"),  # emoji de familia intacto
        ("Ñandú, 你好, 😀", "Ñandú, 你好, 😀"),
    ],
)
def test_the_text_of_a_customer_is_cleaned_before_the_commands_that_would_refuse_it(
    raw: str, clean: str
) -> None:
    assert inbound.clean_text(raw) == clean


def test_the_text_is_cut_at_4096_code_points_without_breaking_a_joined_sequence() -> None:
    assert inbound.clean_text("x" * 5000) == "x" * 4096
    family = "👨‍👩‍👧"  # cinco puntos de código
    cut = inbound.clean_text("x" * 4094 + family)
    assert cut == "x" * 4094  # ni el hombre suelto ni un ZWJ colgando
    assert inbound.clean_text("x" * 4090 + family) == "x" * 4090 + family
    assert len(inbound.clean_text("😀" * 5000)) == 4096


def test_only_visible_text_counts_and_a_name_is_one_clean_line_of_at_most_100() -> None:
    assert inbound.visible("a") and not inbound.visible(" \n\t 　")
    assert not inbound.visible("")
    assert inbound.profile_name("  Ana \n  Pérez‮  ") == "Ana Pérez"
    assert inbound.profile_name("x" * 300) == "x" * 100
    assert inbound.profile_name(None) == inbound.profile_name(5) == ""


@pytest.mark.parametrize(
    ("wa_id", "phone"),
    [
        ("51900000001", "+51900000001"),
        ("5215512345678", "+525512345678"),  # México: 52 + 1 + 10 dígitos (a confirmar)
        ("525512345678", "+525512345678"),
        ("5491112345678", "+541112345678"),  # Argentina: 54 + 9 + 10 dígitos (a confirmar)
        ("541112345678", "+541112345678"),
        ("5511987654321", "+5511987654321"),  # otro país de 13 dígitos: no se toca
        ("12345678", "+12345678"),
        ("123456789012345", "+123456789012345"),
        ("1234567", None),  # corto
        ("1234567890123456", None),  # largo
        ("0123456789", None),  # empieza en 0
        ("+51900000001", None),
        ("5190000000x", None),
        ("٥١٩٠٠٠٠٠٠٠١", None),  # dígitos que no son ASCII
        ("", None),
        (None, None),
        (51900000001, None),
    ],
)
def test_the_wa_id_becomes_e164_and_the_two_forms_of_a_number_are_one(
    wa_id: Any, phone: str | None
) -> None:
    assert inbound.normalize_phone(wa_id) == phone


# ---- De punta a punta: la entrega firmada hasta lo que ve el Inbox ----


def test_a_signed_text_reaches_the_inbox_as_contact_conversation_and_message(
    crm: SimpleNamespace, migrator: DB
) -> None:
    mine = make(crm.a, status_detail="WEBHOOK_NOT_SEEN")
    make(crm.b, external_account_id="100000000000008", external_parent_id="100000000000007")
    before_b = snap(crm.b)
    stamp = int(timezone.now().timestamp()) - 60
    assert (
        deliver(message("wamid.1", "Hola, ¿tienen stock?", timestamp=str(stamp))).status_code == 200
    )
    assert ingress.process() == {"PROCESSED": 1}
    found = snap(crm.a)
    [contact], [convo], [first] = found.contacts, found.conversations, found.messages
    assert (contact.display_name, contact.primary_phone_e164) == (
        "Cliente de prueba",
        "+51900000001",
    )
    assert contact.source == "INBOUND_MESSAGE" and contact.deleted_at is None
    assert (convo.status, convo.contact_id, convo.channel_account_id) == (
        "PENDING", contact.pk, mine.pk,
    )  # fmt: skip
    assert (first.direction, first.sender_type, first.message_type) == (
        "INBOUND",
        "CONTACT",
        "TEXT",
    )
    assert (first.body, first.external_message_id) == ("Hola, ¿tienen stock?", "wamid.1")
    assert (
        first.provider_timestamp is not None and int(first.provider_timestamp.timestamp()) == stamp
    )
    assert convo.last_inbound_at == first.provider_timestamp  # ancla de la ventana de 24 h
    ana = signed(crm.ana)
    listed = ana.get(BASE).json()["results"]
    assert [r["id"] for r in listed] == [str(convo.pk)]
    assert listed[0]["contact"]["display_name"] == "Cliente de prueba"
    assert listed[0]["last_message_preview"] == "Hola, ¿tienen stock?"
    shown = ana.get(f"{BASE}{convo.pk}/messages/").json()["results"]
    assert [(r["body"], r["message_type"], r["direction"]) for r in shown] == [
        ("Hola, ¿tienen stock?", "TEXT", "INBOUND")
    ]
    after_b = snap(crm.b)
    assert (len(after_b.contacts), len(after_b.conversations), len(after_b.messages)) == (
        len(before_b.contacts), 0, 0,
    )  # fmt: skip
    assert ana.get(BASE.replace("org-a", "org-b")).status_code == 404
    closed = rows(migrator)["wa:msg:wamid.1"]
    assert closed["status"] == "PROCESSED" and closed["payload"] is None


def test_the_same_message_delivered_twice_or_claimed_twice_is_one_message(
    crm: SimpleNamespace, migrator: DB
) -> None:
    mine = make(crm.a)
    assert run(message("wamid.1")) == {"PROCESSED": 1}
    assert run(message("wamid.1")) == {}  # Meta repite la entrega: ya está en la ingesta
    # un ítem reclamado dos veces (el plazo caducó y otro trabajador lo toma): no duplica
    claimed = ingress.Claimed(uuid4(), "message", WABA_ID, PHONE_NUMBER_ID, message("wamid.1"), 2)
    assert ingress.route(claimed) == ("PROCESSED", crm.a, mine.pk, None)
    found = snap(crm.a)
    assert (len(found.contacts), len(found.conversations), len(found.messages)) == (1, 1, 1)
    events = "SELECT count(*) FROM outbox_events WHERE event_type = 'message.received'"
    assert migrator.execute(events).fetchone() == (1,)  # y el evento tampoco


def test_four_workers_with_messages_of_a_new_customer_make_one_contact_and_one_conversation(
    crm: SimpleNamespace,
) -> None:
    make(crm.a)
    deliver(*[message(f"wamid.{n}", f"m{n}") for n in range(4)])
    start, outcome = threading.Barrier(4), []

    def work() -> None:
        try:
            start.wait(timeout=10)
            outcome.append(ingress.process(batch_size=1, rounds=1))
        except Exception as error:  # noqa: BLE001 — lo comprueba el test
            outcome.append({type(error).__name__: 1})
        finally:
            connection.close()

    threads = [threading.Thread(target=work) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert sorted(k for o in outcome for k in o) == ["PROCESSED"] * 4, outcome
    found = snap(crm.a)
    assert (len(found.contacts), len(found.conversations), len(found.messages)) == (1, 1, 4)


def test_two_deliveries_of_the_same_message_at_once_are_one_message(crm: SimpleNamespace) -> None:
    mine = make(crm.a)
    start, outcome = threading.Barrier(2), []

    def work() -> None:
        try:
            context = TenantContext(crm.a, "webhook", actor_type=ActorType.INTEGRATION)
            with tenant_scope(context):
                account = ChannelAccount.objects.get(pk=mine.pk)
                start.wait(timeout=10)
                outcome.append(inbound.receive(context, account, message("wamid.1")))
        except Exception as error:  # noqa: BLE001
            outcome.append(type(error).__name__)
        finally:
            connection.close()

    threads = [threading.Thread(target=work) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert outcome == [None, None], outcome
    found = snap(crm.a)
    assert (len(found.contacts), len(found.conversations), len(found.messages)) == (1, 1, 1)


# ---- La cuenta PENDING, DISCONNECTED y ERROR ----


def detail(org: UUID, pk: UUID) -> tuple[str, str | None, int]:
    with tenant_scope(ctx(org)):
        found = ChannelAccount.objects.get(pk=pk)
        return found.status, found.status_detail, found.version


def test_the_first_event_of_a_pending_account_is_the_webhook_seen_signal(
    crm: SimpleNamespace, migrator: DB
) -> None:
    fields = {"credential_id": credential(crm.a), "last_healthcheck_at": timezone.now()}
    ready = make(crm.a, status_detail="WEBHOOK_NOT_SEEN", **fields)
    assert detail(crm.a, ready.pk) == ("PENDING", "WEBHOOK_NOT_SEEN", 1)
    run(message("wamid.1"))
    assert detail(crm.a, ready.pk) == ("CONNECTED", None, 2)  # cumple `connected_ck`
    assert len(snap(crm.a).messages) == 1  # y no pierde el primer mensaje
    run(message("wamid.2"))
    assert detail(crm.a, ready.pk) == ("CONNECTED", None, 2)  # solo la primera vez
    audited = migrator.execute(
        "SELECT actor_type, changes::text FROM audit_logs WHERE action = 'channel_account.seen'"
    ).fetchall()
    assert len(audited) == 1 and audited[0][0] == "INTEGRATION"
    assert json.loads(audited[0][1])["status"] == ["PENDING", "CONNECTED"]


def test_a_pending_account_without_credential_or_health_check_stays_pending_but_keeps_the_message(
    crm: SimpleNamespace,
) -> None:
    bare = make(crm.a, status_detail="WEBHOOK_NOT_SEEN")  # sin credencial ni salud (F7-06)
    run(message("wamid.1"))
    assert detail(crm.a, bare.pk) == ("PENDING", None, 2)  # visto, pero no conectada
    run(message("wamid.2"))
    assert detail(crm.a, bare.pk) == ("PENDING", None, 2)  # sin más cambios ni auditoría


def test_a_pending_account_with_another_detail_keeps_it(crm: SimpleNamespace) -> None:
    fields = {"credential_id": credential(crm.a), "last_healthcheck_at": timezone.now()}
    odd = make(crm.a, status_detail="TOKEN_INVALID", **fields)
    run(message("wamid.1"))
    assert detail(crm.a, odd.pk) == ("PENDING", "TOKEN_INVALID", 1)


def test_an_account_in_error_or_disconnected_registers_the_message_and_a_deleted_one_does_not(
    crm: SimpleNamespace,
) -> None:
    broken = live(crm.a, status="ERROR", status_detail="PROVIDER_ERROR")
    assert run(message("wamid.1")) == {"PROCESSED": 1}
    assert detail(crm.a, broken.pk)[:2] == ("ERROR", "PROVIDER_ERROR")  # no se toca
    with tenant_scope(ctx(crm.a)):
        ChannelAccount.objects.filter(pk=broken.pk).update(status="DISCONNECTED")
    assert run(message("wamid.2")) == {"PROCESSED": 1}  # lo que el cliente escribió no se pierde
    assert detail(crm.a, broken.pk)[0] == "DISCONNECTED"
    assert len(snap(crm.a).messages) == 2
    with tenant_scope(ctx(crm.a)):
        ChannelAccount.objects.filter(pk=broken.pk).update(deleted_at=timezone.now())
    assert run(message("wamid.3")) == {"IGNORED": 1}  # una cuenta borrada ya no se enruta
    assert len(snap(crm.a).messages) == 2


# ---- El contacto ----


def contact(org: UUID, name: str, phone: str | None, **over: Any) -> Contact:
    with tenant_scope(ctx(org)):
        made: Contact = Contact.objects.create(display_name=name, primary_phone_e164=phone, **over)
        return made


def test_an_existing_contact_with_that_phone_is_reused_and_its_name_is_not_overwritten(
    crm: SimpleNamespace,
) -> None:
    make(crm.a)
    mine = contact(crm.a, "Ana de siempre", "+51900000001")
    run(message("wamid.1"))
    found = snap(crm.a)
    assert [c.pk for c in found.contacts if c.primary_phone_e164 == "+51900000001"] == [mine.pk]
    assert found.conversations[0].contact_id == mine.pk
    with tenant_scope(ctx(crm.a)):
        assert Contact.objects.get(pk=mine.pk).display_name == "Ana de siempre"


def test_with_homonyms_the_contact_with_an_open_conversation_here_wins_else_the_oldest(
    crm: SimpleNamespace,
) -> None:
    mine = make(crm.a)
    old, new = (contact(crm.a, n, "+51900000001") for n in ("Más antiguo", "Más nuevo"))
    run(message("wamid.1"))
    assert snap(crm.a).conversations[0].contact_id == old.pk  # sin conversación: el más antiguo
    older, newer = (contact(crm.a, n, "+51900000002") for n in ("Antiguo 2", "Nuevo 2"))
    other = make(crm.a, external_account_id="100000000000010", name="Otro número")
    with tenant_scope(ctx(crm.a)):  # el nuevo habla aquí; el antiguo, en otra cuenta
        Conversation.objects.create(
            number="CONV-90001", contact_id=newer.pk, channel_account_id=mine.pk,
            last_message_at=timezone.now(),
        )  # fmt: skip
        Conversation.objects.create(
            number="CONV-90002", contact_id=older.pk, channel_account_id=other.pk,
            last_message_at=timezone.now() + dt.timedelta(minutes=5),
        )  # fmt: skip
    run(webhooks.text_message("Hola", wamid="wamid.2", sender="51900000002"))
    talking = [c.contact_id for c in snap(crm.a).conversations if c.number == "CONV-90001"]
    assert talking == [newer.pk]  # gana el de la conversación abierta, aunque es el más nuevo
    assert len(snap(crm.a).conversations) == 3 and len(snap(crm.a).contacts) == 4
    assert older.pk != newer.pk and new.pk != old.pk  # nunca se fusiona ni se crea otro


def test_a_deleted_contact_is_not_reused_and_a_phone_of_another_organization_is_not_seen(
    crm: SimpleNamespace,
) -> None:
    make(crm.a)
    contact(crm.a, "Borrado", "+51900000001", deleted_at=timezone.now())
    elsewhere = contact(crm.b, "De B", "+51900000001")
    run(message("wamid.1"))
    found = snap(crm.a)
    assert len(found.contacts) == 2 and found.conversations[0].contact_id not in {elsewhere.pk}
    assert [c.source for c in found.contacts if c.deleted_at is None] == ["INBOUND_MESSAGE"]
    assert len(snap(crm.b).contacts) == 1  # el de B, sin tocar


@pytest.mark.parametrize(("first", "second"), [
    ("5215512345678", "525512345678"), ("525512345678", "5215512345678"),
    ("5491112345678", "541112345678"), ("541112345678", "5491112345678"),
])  # fmt: skip
def test_two_forms_of_the_same_mexican_or_argentine_number_are_one_contact(
    crm: SimpleNamespace, first: str, second: str
) -> None:
    make(crm.a)
    run(webhooks.text_message("Uno", wamid="wamid.1", sender=first))
    run(webhooks.text_message("Dos", wamid="wamid.2", sender=second))
    found = snap(crm.a)
    assert (len(found.contacts), len(found.conversations), len(found.messages)) == (1, 1, 2)
    assert found.contacts[0].primary_phone_e164 in ("+525512345678", "+541112345678")


def test_a_contact_saved_with_the_one_in_the_middle_form_is_found_by_the_other(
    crm: SimpleNamespace,
) -> None:
    make(crm.a)
    mine = contact(crm.a, "Con el 1", "+5215512345678")  # cargado a mano con la forma larga
    run(webhooks.text_message("Hola", wamid="wamid.1", sender="525512345678"))
    assert [c.contact_id for c in snap(crm.a).conversations] == [mine.pk]
    assert len(snap(crm.a).contacts) == 1


def test_the_profile_name_is_cleaned_and_a_missing_one_falls_back_to_the_phone(
    crm: SimpleNamespace,
) -> None:
    make(crm.a)
    value = webhooks.value(messages=[message("wamid.1")], name="  Ana ‮\u2066Pérez  " + "z" * 200)
    ingress_delivery = webhooks.deliver(webhooks.encode(webhooks.envelope(value)))
    from tests.test_meta_webhook import post

    assert post(ingress_delivery).status_code == 200
    value = webhooks.value(messages=[message("wamid.2", **{"from": "51900000002"})], name=None)
    value.pop("contacts")
    post(webhooks.deliver(webhooks.encode(webhooks.envelope(value))))
    assert ingress.process() == {"PROCESSED": 2}
    names = {c.primary_phone_e164: c.display_name for c in snap(crm.a).contacts}
    assert names["+51900000001"].startswith("Ana Pérez") and len(names["+51900000001"]) == 100
    assert names["+51900000002"] == "+51900000002"


def test_a_bad_sender_is_ignored_with_a_code_and_creates_nothing(
    crm: SimpleNamespace, migrator: DB
) -> None:
    make(crm.a)
    for wamid, sender in (("wamid.1", "abc"), ("wamid.2", "12"), ("wamid.3", "+5190")):
        assert run(webhooks.text_message("Hola", wamid=wamid, sender=sender)) == {"IGNORED": 1}
    assert (
        snap(crm.a).contacts == []
        and rows(migrator)["wa:msg:wamid.1"]["ignored_reason"] == "BAD_PHONE"
    )


def test_the_contact_is_created_by_the_integration_actor_in_the_audit(
    crm: SimpleNamespace, migrator: DB
) -> None:
    make(crm.a)
    run(message("wamid.1"))
    found = migrator.execute(
        "SELECT action, actor_type, actor_id FROM audit_logs WHERE action IN"
        " ('contact.created', 'conversation.created', 'message.received') ORDER BY action"
    ).fetchall()
    assert [(a, t, u) for a, t, u in found] == [
        ("contact.created", "INTEGRATION", None),
        ("conversation.created", "INTEGRATION", None),
        ("message.received", "INTEGRATION", None),
    ]


# ---- El tope de contactos nuevos, el evento y los registros ----


def test_the_event_for_the_outbox_carries_only_ids_never_the_text_or_the_phone(
    crm: SimpleNamespace, migrator: DB
) -> None:
    mine = make(crm.a)
    run(message("wamid.1", PRIVATE))
    [event] = migrator.execute(
        "SELECT aggregate_type, aggregate_id, payload::text FROM outbox_events"
        " WHERE event_type = 'message.received'"
    ).fetchall()
    found = snap(crm.a)
    assert event[0] == "message" and event[1] == found.messages[0].pk
    payload = json.loads(event[2])
    assert payload == {
        "conversation_id": str(found.conversations[0].pk),
        "channel_account_id": str(mine.pk),
        "contact_id": str(found.contacts[0].pk),
    }
    assert PRIVATE not in event[2] and "+519" not in event[2]
    assert publish_outbox() == 1  # sin suscriptores aún: se publica y nada más


def test_no_log_of_the_flow_carries_the_phone_or_the_text(
    crm: SimpleNamespace, caplog: pytest.LogCaptureFixture
) -> None:
    make(crm.a)
    caplog.set_level(logging.DEBUG)
    run(message("wamid.1", PRIVATE), webhooks.other_message("image", wamid="wamid.2"))
    run(webhooks.text_message("x", wamid="wamid.3", sender="abc"))
    for secret in (PRIVATE, CUSTOMER, "+51900000001", "Cliente de prueba", "wamid.1"):
        assert secret not in caplog.text, secret


def test_an_error_in_the_commands_fails_the_item_and_rolls_back_everything(
    crm: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, migrator: DB
) -> None:
    make(crm.a)

    def explode(*args: Any, **kw: Any) -> None:
        raise RuntimeError(PRIVATE)

    monkeypatch.setattr(commands, "record_inbound", explode)
    assert run(message("wamid.1")) == {"FAILED": 1}
    found = snap(crm.a)  # ni el contacto quedó a medias
    assert (found.contacts, found.conversations, found.messages) == ([], [], [])
    assert rows(migrator)["wa:msg:wamid.1"]["payload"] is not None
    monkeypatch.undo()
    migrator.execute("UPDATE webhook_ingress SET next_attempt_at = now() - interval '1 second'")
    assert ingress.process() == {"PROCESSED": 1} and len(snap(crm.a).messages) == 1


def test_a_status_item_of_a_pending_account_is_the_seen_signal_too_but_creates_no_message(
    crm: SimpleNamespace,
) -> None:
    mine = make(crm.a, status_detail="WEBHOOK_NOT_SEEN")
    value = webhooks.value(statuses=[webhooks.status("wamid.OUT1", "delivered")])
    from tests.test_meta_webhook import post

    post(webhooks.deliver(webhooks.encode(webhooks.envelope(value))))
    assert ingress.process() == {"PROCESSED": 1}
    assert detail(crm.a, mine.pk)[:2] == ("PENDING", None)
    found = snap(crm.a)
    assert (found.contacts, found.messages) == ([], [])


def test_six_first_events_at_once_change_the_pending_account_and_audit_it_exactly_once(
    crm: SimpleNamespace, migrator: DB
) -> None:
    mine = make(crm.a)
    with tenant_scope(ctx(crm.a)):
        ChannelAccount.objects.filter(pk=mine.pk).update(
            status_detail="WEBHOOK_NOT_SEEN",
            credential_id=credential(crm.a),
            last_healthcheck_at=timezone.now(),
        )
    before = detail(crm.a, mine.pk)[2]
    deliver(*[message(f"wamid.{n}", f"m{n}", **{"from": f"5190000000{n}"}) for n in range(6)])
    start, outcome = threading.Barrier(6), []

    def work() -> None:
        try:
            start.wait(timeout=10)
            outcome.append(ingress.process(batch_size=1, rounds=1))
        except Exception as error:  # noqa: BLE001 — lo comprueba el test
            outcome.append({type(error).__name__: 1})
        finally:
            connection.close()

    threads = [threading.Thread(target=work) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)
    assert sorted(k for o in outcome for k in o) == ["PROCESSED"] * 6, outcome
    assert detail(crm.a, mine.pk) == ("CONNECTED", None, before + 1)
    seen = migrator.execute("SELECT count(*) FROM audit_logs WHERE action = 'channel_account.seen'")
    assert seen.fetchone() == (1,)


def test_the_profile_name_travels_in_the_item_capped_at_200_characters() -> None:
    value = webhooks.value(messages=[message("wamid.1")], name="n" * 300)
    [item] = meta_webhook.parse(webhooks.encode(webhooks.envelope(value))).items
    assert item.payload["profile_name"] == "n" * 200  # `inbound.profile_name` lo recorta a 100


@pytest.mark.parametrize(
    ("raw", "found"),
    [
        ("1700000000", dt.datetime(2023, 11, 14, 22, 13, 20, tzinfo=dt.UTC)),
        ("999999999", None),  # antes de 2001
        ("4102444801", None),  # después de 2100
        ("-5", None), ("1.5", None), ("", None), (1700000000, None), (None, None),
    ],
)  # fmt: skip
def test_the_provider_timestamp_is_a_unix_second_in_text_or_nothing(
    raw: Any, found: dt.datetime | None
) -> None:
    assert inbound.instant(raw) == found


@pytest.mark.parametrize("kind", webhooks.UNSUPPORTED_KINDS)
def test_a_type_not_supported_yet_is_stored_as_unsupported_with_a_fixed_label(
    crm: SimpleNamespace, kind: str
) -> None:
    make(crm.a)
    assert run(webhooks.other_message(kind)) == {"PROCESSED": 1}
    [first] = snap(crm.a).messages
    assert first.message_type == "UNSUPPORTED" and first.body == inbound.LABELS[kind]
    assert first.direction == "INBOUND" and "SIMMEDIA" not in first.body
    shown = signed(crm.ana).get(f"{BASE}{snap(crm.a).conversations[0].pk}/messages/").json()
    assert shown["results"][0]["message_type"] == "UNSUPPORTED"


def test_an_unknown_type_and_a_text_with_nothing_visible_are_unsupported_with_the_generic_label(
    crm: SimpleNamespace,
) -> None:
    make(crm.a)
    odd = webhooks.other_message("order", wamid="wamid.1") | {"order": {"text": PRIVATE}}
    empty = message("wamid.2", " ​‮ \n")
    assert run(odd, empty) == {"PROCESSED": 2}
    found = snap(crm.a)
    assert {m.body for m in found.messages} == {inbound.GENERIC}
    assert {m.message_type for m in found.messages} == {"UNSUPPORTED"}


def test_a_reaction_is_ignored_without_creating_anything_and_counted_without_personal_data(
    crm: SimpleNamespace, migrator: DB, caplog: pytest.LogCaptureFixture
) -> None:
    make(crm.a)
    caplog.set_level(logging.DEBUG)
    assert run(webhooks.reaction("wamid.OUT1", "🎉")) == {"IGNORED": 1}
    found = snap(crm.a)
    assert (found.contacts, found.conversations, found.messages) == ([], [], [])
    assert rows(migrator)["wa:msg:wamid.SIMIN000003"]["ignored_reason"] == "REACTION"
    assert "meta.webhook.reaction_ignored" in caplog.text
    for secret in ("🎉", CUSTOMER, "wamid.OUT1"):
        assert secret not in caplog.text, secret


def test_the_database_refuses_an_outbound_unsupported_message(crm: SimpleNamespace) -> None:
    mine = make(crm.a)
    run(message("wamid.1"))
    convo = snap(crm.a).conversations[0]
    with tenant_scope(ctx(crm.a)), pytest.raises(IntegrityError, match="messages_unsupported_ck"):
        Message.objects.create(
            conversation_id=convo.pk, channel_account_id=mine.pk, direction="OUTBOUND",
            sender_type="USER", sender_user_id=crm.ana.pk, body="x", message_type="UNSUPPORTED",
            client_message_id=uuid4(), delivery_status="QUEUED",
        )  # fmt: skip


def seed(crm: SimpleNamespace, mine: ChannelAccount, **over: Any) -> tuple[Contact, Conversation]:
    who = contact(crm.a, "Ana", "+51900000001")
    with tenant_scope(ctx(crm.a)):
        made = Conversation.objects.create(
            number="CONV-90001", contact_id=who.pk, channel_account_id=mine.pk,
            last_message_at=timezone.now(), **over,
        )  # fmt: skip
    return who, made


def reload(org: UUID, pk: UUID) -> Conversation:
    with tenant_scope(ctx(org)):
        found: Conversation = Conversation.objects.get(pk=pk)
        return found


def ago(**delta: float) -> str:
    return str(int((timezone.now() - dt.timedelta(**delta)).timestamp()))


def test_the_window_anchor_is_the_provider_instant_bounded_to_now_and_never_goes_back(
    crm: SimpleNamespace,
) -> None:
    mine = make(crm.a)
    _, convo = seed(crm, mine)
    run(message("wamid.1", timestamp=ago(hours=2)))
    first = reload(crm.a, convo.pk).last_inbound_at
    assert first is not None and abs((timezone.now() - first).total_seconds() - 7200) < 5
    run(message("wamid.2", timestamp=ago(hours=5)))  # llega después, pero es más viejo
    assert reload(crm.a, convo.pk).last_inbound_at == first  # no retrocede
    run(message("wamid.3", timestamp=str(int(timezone.now().timestamp()) + 86_400)))
    after = reload(crm.a, convo.pk).last_inbound_at
    assert after is not None and after <= timezone.now()  # un instante del futuro se acota
    assert all(m.provider_timestamp <= timezone.now() for m in snap(crm.a).messages)


def test_a_message_without_a_credible_timestamp_still_arrives_and_anchors_at_arrival(
    crm: SimpleNamespace,
) -> None:
    make(crm.a)
    run(message("wamid.1", timestamp="no-es-un-numero"))
    [first] = snap(crm.a).messages
    convo = snap(crm.a).conversations[0]
    assert first.provider_timestamp is None and convo.last_inbound_at is not None
    assert abs((timezone.now() - convo.last_inbound_at).total_seconds()) < 60


def test_the_chat_order_is_the_arrival_order_even_if_the_provider_instant_is_older(
    crm: SimpleNamespace,
) -> None:
    make(crm.a)
    run(message("wamid.1", "Segundo en el tiempo", timestamp=ago(minutes=1)))
    run(message("wamid.2", "Primero en el tiempo", timestamp=ago(hours=3)))
    convo = snap(crm.a).conversations[0]
    assert [m.body for m in snap(crm.a).messages] == [
        "Segundo en el tiempo", "Primero en el tiempo",
    ]  # fmt: skip
    assert convo.last_message_preview == "Primero en el tiempo"  # llegó último


@pytest.mark.parametrize("status", ["WAITING_CUSTOMER", "RESOLVED"])
def test_a_recent_message_reopens_and_an_old_one_does_not_reopen(
    crm: SimpleNamespace, status: str
) -> None:
    mine = make(crm.a)
    _, convo = seed(crm, mine, status=status, resolved_at=timezone.now() - dt.timedelta(hours=1))
    run(message("wamid.1", timestamp=ago(hours=30)))  # un evento de hace más de 24 h
    assert reload(crm.a, convo.pk).status == status  # la anota sin reabrirla
    assert [m.conversation_id for m in snap(crm.a).messages] == [convo.pk]
    run(message("wamid.2", timestamp=ago(minutes=1)))
    assert reload(crm.a, convo.pk).status == "OPEN"


def test_an_old_event_never_reopens_a_closed_conversation_it_opens_a_new_pending_one(
    crm: SimpleNamespace,
) -> None:
    mine = make(crm.a)
    _, closed = seed(crm, mine, status="CLOSED", closed_at=timezone.now())
    run(message("wamid.1", timestamp=ago(days=3)))
    assert reload(crm.a, closed.pk).status == "CLOSED"
    found = snap(crm.a)
    assert [c.status for c in found.conversations] == ["CLOSED", "PENDING"]
    assert found.messages[0].conversation_id == found.conversations[1].pk


def test_a_resolved_conversation_past_the_reopen_limit_is_closed_by_an_old_event_too(
    crm: SimpleNamespace,
) -> None:
    mine = make(crm.a)
    _, convo = seed(crm, mine, status="RESOLVED", resolved_at=timezone.now() - dt.timedelta(days=4))
    run(message("wamid.1", timestamp=ago(days=2)))
    assert reload(crm.a, convo.pk).status == "CLOSED"  # D-INB-2: el plazo de 72 h
    assert [c.status for c in snap(crm.a).conversations] == ["CLOSED", "PENDING"]


def test_past_the_new_contacts_per_minute_cap_the_message_waits_and_is_never_lost(
    crm: SimpleNamespace, migrator: DB, settings: Any, caplog: pytest.LogCaptureFixture
) -> None:
    make(crm.a)
    settings.INBOUND_NEW_CONTACTS_PER_MINUTE = 2
    caplog.set_level(logging.DEBUG)
    senders = [f"5190000010{n}" for n in range(3)]
    sent = [
        webhooks.text_message("Hola", wamid=f"wamid.{n}", sender=s) for n, s in enumerate(senders)
    ]
    assert run(*sent) == {"PROCESSED": 2, "FAILED": 1}
    assert len(snap(crm.a).contacts) == 2 and "meta.webhook.contact_cap" in caplog.text
    waiting = rows(migrator)["wa:msg:wamid.2"]
    assert waiting["status"] == "FAILED" and waiting["payload"] is not None  # sigue entero
    failure = migrator.execute("SELECT reason FROM webhook_failures").fetchone()
    assert failure == ("RATE_LIMITED",)
    # un cliente que ya existe no cuenta contra el tope
    assert run(webhooks.text_message("Otra vez", wamid="wamid.9", sender=senders[0])) == {
        "PROCESSED": 1
    }
    # pasa el minuto: el mensaje retenido entra y nada se perdió
    migrator.execute(
        "UPDATE webhook_ingress SET next_attempt_at = now() - interval '1 second',"
        " claimed_until = NULL WHERE event_key = 'wa:msg:wamid.2'"
    )
    migrator.execute("UPDATE contacts SET created_at = created_at - interval '2 minutes'")
    assert ingress.process() == {"PROCESSED": 1}
    found = snap(crm.a)
    assert (len(found.contacts), len(found.messages)) == (3, 4)
    assert "12345" not in caplog.text and not any(s in caplog.text for s in senders)


def test_the_cap_is_per_account_and_per_minute_not_per_organization(
    crm: SimpleNamespace, settings: Any
) -> None:
    make(crm.a)
    settings.INBOUND_NEW_CONTACTS_PER_MINUTE = 1
    other = make(
        crm.a, external_account_id="100000000000010", external_parent_id=WABA_ID, name="Otro"
    )
    assert other.pk is not None
    run(webhooks.text_message("Hola", wamid="wamid.1", sender="51900000101"))
    deliver(
        webhooks.text_message("Hola", wamid="wamid.2", sender="51900000102"),
        number="100000000000010",
    )
    assert ingress.process() == {"PROCESSED": 1}  # el tope es de esta cuenta: la otra tiene el suyo
    assert len(snap(crm.a).contacts) == 2


def test_the_migration_goes_back_and_forward_keeping_the_messages(
    crm: SimpleNamespace, migrator: DB
) -> None:
    make(crm.a)
    run(message("wamid.1"))
    with as_migrator():
        call_command("migrate", "conversations", "0009", verbosity=0)
    columns = migrator.execute(
        "SELECT column_name FROM information_schema.columns"
        " WHERE table_name = 'messages' AND column_name = 'provider_timestamp'"
    ).fetchall()
    assert columns == []
    assert migrator.execute("SELECT count(*) FROM messages").fetchone() == (1,)
    with as_migrator():
        call_command("migrate", "conversations", verbosity=0)
    assert migrator.execute(
        "SELECT count(*) FROM pg_constraint WHERE conname IN"
        " ('messages_type_ck', 'messages_unsupported_ck')"
    ).fetchone() == (2,)
    assert migrator.execute(
        "SELECT message_type, provider_timestamp IS NULL FROM messages"
    ).fetchone() == ("TEXT", True)
    assert put("wa:msg:again") is True  # y la ingesta sigue funcionando
