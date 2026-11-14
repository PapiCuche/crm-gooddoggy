"""F7-14: un mensaje entrante de WhatsApp sin teléfono (solo con BSUID) se cuenta como `NO_PHONE`,
sin crear nada y sin registrar el `user_id`, el texto ni el nombre; `BAD_PHONE` queda para un
`from` presente y mal formado."""

import logging
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest

from apps.conversations import ingress
from apps.conversations.meta_webhook import parse
from tests.logs import log_text
from tests.meta_sim import webhooks
from tests.meta_sim.webhooks import BSUID, CUSTOMER
from tests.test_contacts_api import crm
from tests.test_inbound_whatsapp import PRIVATE, run, snap
from tests.test_meta_webhook import channel
from tests.test_webhook_ingress import OTHER_NUMBER, OTHER_WABA, deliver, rows
from tests.test_whatsapp_account import make

__all__ = ["channel", "crm"]  # las fixtures, para pytest
pytestmark = pytest.mark.usefixtures("tenant_db", "no_realtime_broker")
DB = psycopg.Connection[Any]
CONTEXT = {"event", "level", "logger", "timestamp", "request_id", "correlation_id"}
CONTEXT |= {"organization_id", "actor_type", "user_id"}  # los pone el scope (user_id: del CRM)


@pytest.fixture(autouse=True)
def fresh_wake(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ingress, "_last_wake", 0.0)


def no_phone(wamid: str = "wamid.1", **over: Any) -> dict[str, Any]:
    return webhooks.no_phone_message(PRIVATE, wamid=wamid) | over


def logged(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    return [r.msg for r in caplog.records if "meta.webhook.no_phone" in str(r.msg)]  # type: ignore[misc]


def private_free(caplog: pytest.LogCaptureFixture) -> None:
    text = log_text(caplog)
    for secret in (BSUID, PRIVATE, "Cliente de prueba", "wamid.1", CUSTOMER):
        assert secret not in text, secret


@pytest.mark.parametrize(
    "sender", ["absent", None, ""], ids=["absent", "null", "empty"]
)  # fmt: skip
def test_a_message_without_a_phone_is_no_phone_creates_nothing_and_empties_the_payload(
    crm: SimpleNamespace, migrator: DB, sender: str | None
) -> None:
    make(crm.a)
    found = no_phone() if sender == "absent" else no_phone() | {"from": sender}
    assert run(found) == {"IGNORED": 1}
    assert run(found) == {}  # la misma entrega dos veces: una sola fila
    row = rows(migrator)["wa:msg:wamid.1"]
    assert (row["status"], row["ignored_reason"], row["payload"]) == ("IGNORED", "NO_PHONE", None)
    assert snap(crm.a) == SimpleNamespace(contacts=[], conversations=[], messages=[])
    audit = migrator.execute(
        "SELECT count(*) FROM audit_logs WHERE organization_id = %s"
        " AND action IN ('contact.created', 'conversation.created', 'message.received')",
        [crm.a],
    ).fetchone()
    outbox = migrator.execute("SELECT count(*) FROM outbox_events").fetchone()
    assert audit == (0,) and outbox == (0,)


@pytest.mark.parametrize("sender", ["abc", "12", "+5190", " ", 51900000001, ["x"], {"a": 1}])
def test_a_present_but_malformed_phone_is_still_bad_phone(
    crm: SimpleNamespace, migrator: DB, sender: Any, caplog: pytest.LogCaptureFixture
) -> None:
    make(crm.a)
    caplog.set_level(logging.DEBUG)
    assert run(webhooks.text_message("Hola", wamid="wamid.1") | {"from": sender}) == {"IGNORED": 1}
    assert rows(migrator)["wa:msg:wamid.1"]["ignored_reason"] == "BAD_PHONE"
    assert snap(crm.a).contacts == [] and logged(caplog) == []


def test_a_reaction_without_a_phone_is_still_a_reaction(crm: SimpleNamespace, migrator: DB) -> None:
    make(crm.a)
    found = webhooks.reaction("wamid.OUT1") | {"from_user_id": BSUID}
    del found["from"]
    assert run(found) == {"IGNORED": 1}
    assert rows(migrator)["wa:msg:wamid.SIMIN000003"]["ignored_reason"] == "REACTION"


def test_the_log_line_has_the_account_type_and_a_flag_never_the_user_id_text_or_name(
    crm: SimpleNamespace, caplog: pytest.LogCaptureFixture
) -> None:
    mine = make(crm.a)
    caplog.set_level(logging.DEBUG)
    bare = webhooks.no_phone_message("x", wamid="wamid.3")
    del bare["from_user_id"]
    run(no_phone(), webhooks.no_phone_message("x", wamid="wamid.2") | {"type": "Mi DNI 123"})
    run(bare)  # en otro cambio: el indicador es del cambio entero
    first, second, third = logged(caplog)
    assert set(first) - CONTEXT == {"channel_account_id", "type", "user_id_seen"}
    assert (first["channel_account_id"], first["type"], first["user_id_seen"]) == (
        str(mine.pk), "text", True,
    )  # fmt: skip
    assert first["organization_id"] == str(crm.a) and first["user_id"] is None
    assert second["type"] == "other"  # un tipo desconocido no viaja tal cual al registro
    assert third["user_id_seen"] is False  # ni `from_user_id` ni `contacts[].user_id`
    private_free(caplog)
    assert "Mi DNI" not in caplog.text


def test_the_flag_says_whether_the_change_carried_any_user_id_and_cannot_be_forged() -> None:
    def seen(message: dict[str, Any], contacts: list[Any] | None = None) -> Any:
        found = webhooks.value(messages=[message])
        if contacts is not None:
            found["contacts"] = contacts
        [item] = parse(webhooks.encode(webhooks.envelope(found))).items
        return item.payload.get("user_id_seen", "unset")

    bare = webhooks.no_phone_message("x")
    del bare["from_user_id"]
    assert seen(no_phone()) is True  # en el mensaje y en `contacts[]`
    assert seen(no_phone(), contacts=[]) is True  # solo `from_user_id`
    assert seen(bare, contacts=[{"user_id": BSUID}]) is True  # solo `contacts[].user_id`
    assert seen(bare, contacts=[]) is False
    assert (
        seen((no_phone() | {"from": ""}), contacts=[]) is seen(no_phone() | {"from": None}) is True
    )
    assert seen(bare | {"from_user_id": ""}, contacts=[{"user_id": ""}, {"user_id": 5}]) is False
    assert seen(bare, contacts="x") is False  # type: ignore[arg-type]
    assert seen(bare | {"user_id_seen": "forjado"}, contacts=[]) is False  # se sobrescribe
    with_phone = webhooks.text_message("x") | {"user_id_seen": True}
    assert seen(webhooks.text_message("x")) == "unset" and seen(with_phone) is True  # sin uso
    [item] = parse(webhooks.encode(webhooks.envelope(webhooks.value(messages=[no_phone()])))).items
    assert "profile_name" not in item.payload  # el nombre va por `wa_id`; sin él, no viaja
    [state] = parse(webhooks.status_delivery("wamid.OUT1", "delivered").body).items
    assert "user_id_seen" not in state.payload  # un estado no es un mensaje sin teléfono


def test_the_number_of_a_writes_only_in_a_and_never_in_b(
    crm: SimpleNamespace, migrator: DB, caplog: pytest.LogCaptureFixture
) -> None:
    mine = make(crm.a)
    theirs = make(crm.b, external_account_id=OTHER_NUMBER, external_parent_id=OTHER_WABA)
    caplog.set_level(logging.DEBUG)
    deliver(no_phone("wamid.1"))  # el número de A
    deliver(no_phone("wamid.2"), waba=OTHER_WABA, number=OTHER_NUMBER)  # el de B
    deliver(no_phone("wamid.3"), number=OTHER_NUMBER)  # el de B con el WABA de A
    assert ingress.process() == {"IGNORED": 3}
    found = rows(migrator)
    routed = {k: (r["routed_organization_id"], r["routed_channel_account_id"], r["ignored_reason"])
              for k, r in found.items()}  # fmt: skip
    assert routed == {
        "wa:msg:wamid.1": (crm.a, mine.pk, "NO_PHONE"),
        "wa:msg:wamid.2": (crm.b, theirs.pk, "NO_PHONE"),
        "wa:msg:wamid.3": (None, None, "WABA_MISMATCH"),
    }
    assert [(m["organization_id"], m["channel_account_id"]) for m in logged(caplog)] == [
        (str(crm.a), str(mine.pk)), (str(crm.b), str(theirs.pk)),
    ]  # fmt: skip
    for org in (crm.a, crm.b):
        assert snap(org) == SimpleNamespace(contacts=[], conversations=[], messages=[])
