"""F7b-04c: el nombre del cliente de Instagram tras su primer mensaje (ADR-035 D-F7b-06). El
contacto nuevo emite `contact_identity.created`; `tasks.fetch_profile` lee `name` y `username` de
la página simulada (`FakePageGraph`, por `core.http`) y solo cambia el nombre fijo. PostgreSQL real
con `crm_app`."""

import logging
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import psycopg
import pytest
from django.db import connection

from apps.contacts.commands import update_contact
from apps.contacts.models import Contact
from apps.conversations import credentials, graph, inbound, profiles, tasks
from apps.conversations.models import ChannelAccount, ContactIdentity
from core.outbox.models import OutboxEvent
from core.tenancy.scope import tenant_scope
from tests.meta_sim import social
from tests.meta_sim.graph import TEST_TOKEN
from tests.meta_sim.page_graph import FakePageGraph
from tests.meta_sim.social import IGSID
from tests.test_channel_accounts import everything
from tests.test_contacts_api import crm
from tests.test_memberships import ctx
from tests.test_social_accounts import connect, meta
from tests.test_social_schema import account

__all__ = ["crm", "meta"]  # los fixtures, para pytest
pytestmark = pytest.mark.usefixtures("tenant_db")
DB = psycopg.Connection[Any]
FIXED = profiles.SOCIAL_NAMES["INSTAGRAM"]


class NotFound(FakePageGraph):
    def handle(self, *args: Any) -> Any:
        return 404, {"Content-Type": "application/json"}, b'{"error": {"code": 100}}'


def first_message(crm: SimpleNamespace, where: ChannelAccount) -> ContactIdentity:
    channel = social.INSTAGRAM if where.platform == "INSTAGRAM" else social.MESSENGER
    with tenant_scope(ctx(crm.a)) as tenant:
        inbound.receive_social(tenant, where, "message", social.text(channel))
        found: ContactIdentity = ContactIdentity.objects.get(channel_account_id=where.pk)
        return found


@pytest.fixture
def ig(crm: SimpleNamespace, meta: FakePageGraph) -> ContactIdentity:
    made = connect(crm, "INSTAGRAM")[0]
    meta.calls.clear()
    return first_message(crm, made)


def run(org: UUID, identity: ContactIdentity) -> None:
    kwargs = {"identity_id": str(identity.pk), "organization_id": str(org)}
    tasks.fetch_profile.apply(kwargs=kwargs)


def name_of(org: UUID, identity: ContactIdentity) -> str:
    with tenant_scope(ctx(org)):
        return str(Contact.objects.get(pk=identity.contact_id).display_name)


def outcome(caplog: pytest.LogCaptureFixture) -> tuple[str, str]:
    found = [r for r in caplog.records if "contact.profile" in r.getMessage()]
    assert len(found) == 1
    return found[0].levelname, found[0].getMessage()


def test_the_first_instagram_message_queues_the_read_and_names_the_contact(
    crm: SimpleNamespace, meta: FakePageGraph, ig: ContactIdentity, migrator: DB,
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:  # fmt: skip
    assert name_of(crm.a, ig) == FIXED and meta.calls == []  # nunca dentro del webhook
    with tenant_scope(ctx(crm.a)):
        event = OutboxEvent.objects.get(event_type="contact_identity.created")
    assert (event.aggregate_id, event.payload) == (ig.pk, {})  # solo el id
    queued: list[dict[str, str]] = []
    monkeypatch.setattr(tasks.fetch_profile, "apply_async", lambda kwargs: queued.append(kwargs))
    tasks.profile_event.apply(kwargs={"event_id": str(event.pk), "organization_id": str(crm.a)})
    assert queued == [{"identity_id": str(ig.pk), "organization_id": str(crm.a)}]
    with caplog.at_level(logging.DEBUG):
        run(crm.a, ig)
    assert name_of(crm.a, ig) == "Cliente Simulado"
    assert [(c["method"], c["path"], c["query"]) for c in meta.calls] == [
        ("GET", f"/v23.0/{IGSID}", {"fields": ["name,username"]})
    ]
    assert outcome(caplog)[0] == "INFO" and "OK" in outcome(caplog)[1]
    assert TEST_TOKEN not in everything(migrator) + caplog.text
    assert IGSID not in caplog.text and "Cliente Simulado" not in caplog.text
    audited = "SELECT changes::text FROM audit_logs WHERE action = 'contact.updated'"
    assert TEST_TOKEN not in str(migrator.execute(audited).fetchall())


def test_messenger_keeps_its_fixed_name_and_queues_nothing(
    crm: SimpleNamespace, meta: FakePageGraph
) -> None:
    identity = first_message(crm, account(crm.a))
    with tenant_scope(ctx(crm.a)):
        assert not OutboxEvent.objects.filter(event_type="contact_identity.created").exists()
    run(crm.a, identity)  # aunque alguien la encolara: no es de Instagram
    assert name_of(crm.a, identity) == "Cliente de Messenger" and meta.calls == []


@pytest.mark.parametrize(
    ("script", "profile", "expected", "calls"),
    [
        (["token"], None, "TOKEN_INVALID", 1),
        (["token_400"], None, "TOKEN_INVALID", 1),
        ([], {}, "REJECTED", 1),  # el IGSID no existe: el `100` de Meta
        (["rate_limit"] * 4, None, "UNAVAILABLE", 4),  # 1 + `PROFILE_RETRIES`, nunca más
        (["server", "timeout", "cut", "server"], None, "UNAVAILABLE", 4),
        ([], {IGSID: {"name": 7, "username": "con espacio"}}, "BAD_DATA", 1),
        ([], {IGSID: {"name": "‮⁦‏\x00 \n"}}, "BAD_DATA", 1),
        ([], {IGSID: {"name": ["Ana"], "username": "a" * 31}}, "BAD_DATA", 1),
    ],
)
def test_each_failure_keeps_the_fixed_name_alerts_and_never_logs_the_token(
    crm: SimpleNamespace, meta: FakePageGraph, ig: ContactIdentity, migrator: DB,
    caplog: pytest.LogCaptureFixture, script: list[str], profile: Any, expected: str, calls: int,
) -> None:  # fmt: skip
    meta.then(*script)
    if profile is not None:
        meta.profiles = profile
    with caplog.at_level(logging.DEBUG):
        run(crm.a, ig)
    assert name_of(crm.a, ig) == FIXED and len(meta.calls) == calls
    level, message = outcome(caplog)
    assert level == "ERROR" and expected in message
    assert TEST_TOKEN not in everything(migrator) + caplog.text


def test_an_http_404_is_a_rejection(
    crm: SimpleNamespace, ig: ContactIdentity, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:  # fmt: skip
    from tests.meta_sim import transport

    transport.install(monkeypatch, NotFound())
    with caplog.at_level(logging.DEBUG):
        run(crm.a, ig)
    assert name_of(crm.a, ig) == FIXED and "REJECTED" in outcome(caplog)[1]


@pytest.mark.parametrize(
    ("profile", "expected"),
    [
        ({"name": "  Ana‮ ⁦María⁩\n\tPérez\x00\U000e0041 ", "username": "x"},
         "Ana María Pérez"),
        ({"name": "José ‍Luis​﻿"}, "José Luis"),  # NFC y sin formato invisible
        ({"name": "N" * 300}, "N" * 100),
        ({"name": "ㅤᅟ", "username": "cliente.sim"}, "@cliente.sim"),  # rellenos: no
        ({"name": None, "username": "ana_perez.99"}, "@ana_perez.99"),
        ({"name": "Ana" + "\ufe01" * 40, "username": "ana.sim"}, "@ana.sim"),  # F2-131: oculto
        ({"name": "Ana\ufe0f", "username": "ana.sim"}, "@ana.sim"),
        ({"name": "Ana \U0001f468\u200d\U0001f469"}, "Ana \U0001f468\U0001f469"),  # sin el ZWJ
        ({"name": "Caf\u00e9 \u2764\ufe0f"}, "Caf\u00e9 \u2764\ufe0f"),
    ],
)  # fmt: skip
def test_the_profile_is_sanitized_and_capped(
    crm: SimpleNamespace, meta: FakePageGraph, ig: ContactIdentity, profile: Any, expected: str
) -> None:
    meta.profiles = {IGSID: profile}
    run(crm.a, ig)
    assert name_of(crm.a, ig) == expected


def test_a_name_a_person_edited_is_never_overwritten(
    crm: SimpleNamespace, meta: FakePageGraph, ig: ContactIdentity
) -> None:
    with tenant_scope(ctx(crm.a, crm.ana)) as person:
        contact = Contact.objects.get(pk=ig.contact_id)
        update_contact(person, contact_id=contact.pk, version=contact.version, display_name="Rosa")
    run(crm.a, ig)
    assert name_of(crm.a, ig) == "Rosa" and meta.calls == []  # ni se pregunta a Meta


def test_a_name_edited_while_meta_answers_is_kept(
    crm: SimpleNamespace, ig: ContactIdentity, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = profiles.apply_profile

    def edited_meanwhile(tenant: Any, **kwargs: Any) -> str:
        contact = Contact.objects.get(pk=ig.contact_id)
        update_contact(tenant, contact_id=contact.pk, version=contact.version, display_name="Rosa")
        return real(tenant, **kwargs)

    monkeypatch.setattr(profiles, "apply_profile", edited_meanwhile)
    run(crm.a, ig)
    assert name_of(crm.a, ig) == "Rosa"


def test_the_name_never_merges_contacts(
    crm: SimpleNamespace, ig: ContactIdentity, migrator: DB
) -> None:
    with tenant_scope(ctx(crm.a)):
        twin = Contact.objects.create(display_name="Cliente Simulado")  # el mismo nombre
        before = Contact.objects.count()
    run(crm.a, ig)
    with tenant_scope(ctx(crm.a)):
        assert Contact.objects.count() == before
        assert ContactIdentity.objects.get().contact_id == ig.contact_id != twin.pk
    assert name_of(crm.a, ig) == "Cliente Simulado"


def test_the_task_runs_in_the_identity_organization_only(
    crm: SimpleNamespace, meta: FakePageGraph, ig: ContactIdentity,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    run(crm.b, ig)  # el id de A con la organización de B: no existe
    assert name_of(crm.a, ig) == FIXED and meta.calls == []
    seen: list[Any] = []
    real = profiles.apply_profile

    def spy(tenant: Any, **kwargs: Any) -> str:
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_setting('app.tenant_id', true)")
            seen.append((tenant.organization_id, cursor.fetchone()[0]))
        return real(tenant, **kwargs)

    monkeypatch.setattr(profiles, "apply_profile", spy)
    run(crm.a, ig)
    assert seen == [(crm.a, str(crm.a))] and name_of(crm.a, ig) == "Cliente Simulado"


def account_state(org: UUID, identity: ContactIdentity) -> tuple[str, str | None]:
    with tenant_scope(ctx(org)):
        found = ChannelAccount.objects.get(pk=identity.channel_account_id)
        return found.status, found.status_detail


@pytest.mark.parametrize("script", ["token", "token_400"])
def test_a_190_on_the_profile_read_puts_the_account_in_error(
    crm: SimpleNamespace, meta: FakePageGraph, ig: ContactIdentity, script: str
) -> None:
    """F7b-05a (obligación de #778): el `190` de la lectura del perfil deja la cuenta en `ERROR`
    (`TOKEN_INVALID`), como el del envío; la otra organización no se toca."""
    assert account_state(crm.a, ig)[0] != "ERROR"
    other = account(crm.b, "INSTAGRAM", "100000000000103", external_parent_id="100000000000998")
    meta.then(script)
    run(crm.a, ig)
    assert account_state(crm.a, ig) == ("ERROR", "TOKEN_INVALID")
    with tenant_scope(ctx(crm.b)):
        assert ChannelAccount.objects.get(pk=other.pk).status == "PENDING"


def test_a_190_for_a_token_already_rotated_leaves_the_account_alone(
    crm: SimpleNamespace, meta: FakePageGraph, ig: ContactIdentity, monkeypatch: pytest.MonkeyPatch
) -> None:
    """El `190` es del token que se leyó: si mientras tanto se rotó, la cuenta no pasa a `ERROR`."""
    before = account_state(crm.a, ig)

    def rotated(token: str, customer: str) -> Any:
        with tenant_scope(ctx(crm.a)) as tenant:  # como `rotate_token`: revocar y guardar otro
            old = ChannelAccount.objects.get(pk=ig.channel_account_id).credential_id
            credentials.revoke(tenant, old)
            fresh = credentials.store(tenant, ig.channel_account_id, "otro-token-de-prueba")
            ChannelAccount.objects.filter(pk=ig.channel_account_id).update(credential_id=fresh.pk)
        raise graph.TokenRejected

    monkeypatch.setattr(graph, "profile", rotated)
    run(crm.a, ig)
    assert account_state(crm.a, ig) == before
