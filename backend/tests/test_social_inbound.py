"""F7b-04b: los webhooks de Instagram y Messenger de punta a punta (ADR-035 §4–§5): firma, ingesta,
enrutado por la cuenta, identidad sin teléfono y mensaje en el Inbox. El catálogo del simulador
(`tests/meta_sim/social.py`) es el oráculo: cada evento tiene su resultado esperado. PostgreSQL real
con `crm_app`; la ruta pública con el cliente de pruebas de Django."""

from types import SimpleNamespace
from typing import Any
from uuid import UUID

import psycopg
import pytest
from django.db import IntegrityError
from django.utils import timezone

from apps.contacts.models import Contact
from apps.conversations import commands, inbound, ingress
from apps.conversations.models import ChannelAccount, ContactIdentity, Conversation, Message
from core.tenancy.scope import tenant_scope
from tests.meta_sim import social, webhooks
from tests.meta_sim.social import CHANNELS, IG_ACCOUNT_ID, MESSENGER, OTHER_ACCOUNT_ID, PAGE_ID
from tests.test_conversations_tables import world
from tests.test_memberships import ctx
from tests.test_meta_webhook import channel, post
from tests.test_social_schema import account
from tests.test_whatsapp_account import credential

__all__ = ["channel", "world"]  # las fixtures, para pytest
pytestmark = pytest.mark.usefixtures("tenant_db")
DB = psycopg.Connection[Any]
OK, TEXT, NONE = ("PROCESSED", None), ["TEXT"], ["UNSUPPORTED"]


def ignored(reason: str) -> tuple[list[tuple[str, str | None]], list[str]]:
    return [("IGNORED", reason)], []


# El resultado de cada evento del catálogo: (estado y motivo de cada ítem, tipos de los mensajes).
EXPECTED: dict[str, tuple[list[tuple[str, str | None]], list[str]]] = {
    "text": ([OK], TEXT), "reply": ([OK], TEXT), "standby": ([OK], TEXT),
    "story_reply": ([OK], TEXT), "batch": ([OK] * 3, TEXT * 3), "grouped": ([OK] * 2, TEXT * 2),
    "is_unsupported": ([OK], NONE), "sticker": ([OK], NONE),
    **{f"attachment_{k}": ([OK], NONE) for c in CHANNELS for k in c.attachments},
    "own_echo": ignored("ECHO"), "foreign_echo": ignored("ECHO"),
    "own_echo_no_app_id": ignored("ECHO"),  # las dos formas del eco (SIN VERIFICAR 11/12)
    "read": ignored("STATUS_LATER"), "delivery": ignored("STATUS_LATER"),
    "delivery_no_mids": ignored("STATUS_LATER"),  # sin `timestamp` y sin `mids`
    "is_deleted": ignored("DELETED"), "reaction": ignored("REACTION"),
    "unreact": ignored("REACTION"), "account_mismatch": ignored("ACCOUNT_MISMATCH"),
    "echo_account_mismatch": ignored("ACCOUNT_MISMATCH"), "unknown_object": ([], []),
}  # fmt: skip
LABELS = {
    "attachment_story_mention": "Te mencionó en una historia",
    "attachment_fallback": "Enlace compartido",  # sin `payload`
    "is_unsupported": "Mensaje no soportado",
    "sticker": "Imagen recibida",
}


@pytest.fixture
def social_world(world: SimpleNamespace) -> SimpleNamespace:
    """A con su página de Messenger y su cuenta de Instagram; B con otra página (la del cruce)."""
    world.page, world.ig = account(world.a), account(world.a, "INSTAGRAM", IG_ACCOUNT_ID)
    world.other = account(world.b, external=OTHER_ACCOUNT_ID)
    return world


def outcome(migrator: DB) -> list[tuple[str, str | None]]:
    found = migrator.execute(
        "SELECT status, ignored_reason FROM webhook_ingress ORDER BY event_key"
    ).fetchall()
    return sorted(found, key=str)


def inbox(org: UUID) -> list[tuple[str, str]]:
    with tenant_scope(ctx(org)):
        return list(Message.objects.order_by("created_at").values_list("message_type", "body"))


def text_to(external: str, mid: str, customer: str = social.PSID) -> webhooks.Delivery:
    event = social.text(MESSENGER, mid=mid, account=external)
    event["sender"]["id"] = customer
    return social.signed(MESSENGER, event, entry_id=external)


@pytest.mark.parametrize(
    ("net", "name"), [(c.object, n) for c in CHANNELS for n in social.catalogue(c)]
)
def test_every_catalogue_event_has_its_expected_outcome(
    social_world: SimpleNamespace, migrator: DB, net: str, name: str
) -> None:
    delivery = social.catalogue(MESSENGER if net == "page" else social.INSTAGRAM)[name]
    assert post(delivery).status_code == 200
    stored = str(migrator.execute("SELECT payload::text FROM webhook_ingress").fetchall())
    assert social.STORY_URL not in stored  # quitada antes de guardar (ADR-035 §4)
    ingress.process()
    statuses, types = EXPECTED[name]
    assert outcome(migrator) == sorted(statuses, key=str)
    found = inbox(social_world.a)
    assert [t for t, _ in found] == types and inbox(social_world.b) == []
    if name in LABELS:
        assert found[0][1] == LABELS[name]
    if name == "story_reply":
        assert found[0][1].startswith("Respondió a tu historia: ")


def test_the_oracle_covers_the_whole_catalogue() -> None:
    names = {n for c in CHANNELS for n in social.catalogue(c)}
    assert names == set(EXPECTED)


def test_one_identity_per_account_one_contact_per_identity_and_never_a_merge(
    social_world: SimpleNamespace,
) -> None:
    with tenant_scope(ctx(social_world.a)):
        Contact.objects.create(display_name="Cliente de Messenger")  # homónimo: no se reutiliza
    post(text_to(PAGE_ID, "m_SIMIN000001"))
    post(text_to(PAGE_ID, "m_SIMIN000002"))
    ig = social.text(social.INSTAGRAM, mid="m_SIMIN000003")
    ig["sender"]["id"] = social.PSID  # el mismo número en otra cuenta: otra persona
    post(social.signed(social.INSTAGRAM, ig))
    assert ingress.process() == {"PROCESSED": 3}
    with tenant_scope(ctx(social_world.a)):
        identities = list(ContactIdentity.objects.order_by("platform").values_list(
            "platform", "contact_id", "external_id"))  # fmt: skip
        assert [(p, e) for p, _, e in identities] == [
            ("INSTAGRAM", social.PSID), ("MESSENGER", social.PSID)
        ]  # fmt: skip
        assert identities[0][1] != identities[1][1]  # dos contactos, sin fusión (D-F7b-05)
        born = Contact.objects.filter(pk__in=[c for _, c, _ in identities])
        assert sorted(born.values_list("display_name", "primary_phone_e164", "source")) == [
            ("Cliente de Instagram", None, "INBOUND_MESSAGE"),
            ("Cliente de Messenger", None, "INBOUND_MESSAGE"),
        ]
        threads = Conversation.objects.filter(channel_account_id=social_world.page.pk)
        assert list(threads.values_list("contact_identity_id", flat=True)) == [
            ContactIdentity.objects.get(platform="MESSENGER").pk
        ]
        assert Message.objects.filter(channel_account_id=social_world.page.pk).count() == 2


def test_the_tenant_is_the_account_owner_and_unknown_or_deleted_accounts_write_nothing(
    social_world: SimpleNamespace, migrator: DB
) -> None:
    post(text_to(OTHER_ACCOUNT_ID, "m_SIMIN000001"))  # la página de B
    post(text_to("100000000000888", "m_SIMIN000002"))  # nadie la conectó
    with tenant_scope(ctx(social_world.a)):
        ChannelAccount.objects.filter(pk=social_world.page.pk).update(deleted_at=timezone.now())
    post(text_to(PAGE_ID, "m_SIMIN000003"))  # desconectada
    ingress.process()
    routed = migrator.execute(
        "SELECT event_key, status, ignored_reason, routed_organization_id FROM webhook_ingress"
        " ORDER BY event_key"
    ).fetchall()
    assert routed == [
        ("fb:msg:m_SIMIN000001", "PROCESSED", None, social_world.b),
        ("fb:msg:m_SIMIN000002", "IGNORED", "UNKNOWN_ACCOUNT", None),
        ("fb:msg:m_SIMIN000003", "IGNORED", "UNKNOWN_ACCOUNT", None),
    ]
    assert inbox(social_world.a) == [] and [t for t, _ in inbox(social_world.b)] == TEXT


def test_a_repeated_delivery_is_one_item_and_one_message(
    social_world: SimpleNamespace, migrator: DB
) -> None:
    for _ in range(2):
        assert post(text_to(PAGE_ID, "m_SIMIN000001")).status_code == 200
        ingress.process()
    assert outcome(migrator) == [OK] and len(inbox(social_world.a)) == 1


def test_a_failure_is_retried_and_a_dead_item_keeps_no_story_media(
    social_world: SimpleNamespace, migrator: DB, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("caída")

    real = inbound.create_contact  # type: ignore[attr-defined]
    monkeypatch.setattr(inbound, "create_contact", broken)
    post(text_to(PAGE_ID, "m_SIMIN000001"))
    post(social.signed(social.INSTAGRAM, social.story_mention()))
    migrator.execute("UPDATE webhook_ingress SET attempts = 7 WHERE event_key LIKE 'ig:%%'")
    assert ingress.process() == {"FAILED": 1, "DEAD": 1}
    assert social.STORY_URL not in str(
        migrator.execute("SELECT payload::text FROM webhook_ingress")
    )
    monkeypatch.setattr(inbound, "create_contact", real)
    migrator.execute("UPDATE webhook_ingress SET next_attempt_at = now() WHERE status = 'FAILED'")
    assert ingress.process() == {"PROCESSED": 1} and len(inbox(social_world.a)) == 1


def test_the_new_contacts_cap_defers_without_losing_the_message(
    social_world: SimpleNamespace, migrator: DB, settings: Any
) -> None:
    settings.INBOUND_NEW_CONTACTS_PER_MINUTE = 1
    post(text_to(PAGE_ID, "m_SIMIN000001"))
    post(text_to(PAGE_ID, "m_SIMIN000002", customer="100000000000203"))
    assert ingress.process() == {"PROCESSED": 1, "FAILED": 1} and len(inbox(social_world.a)) == 1
    failures = migrator.execute("SELECT count(*) FROM webhook_failures").fetchone()
    assert failures == (0,)  # `RATE_LIMITED` espera al minuto siguiente sin gastar un intento


def test_signature_and_entry_id_are_checked_before_anything_is_stored(
    social_world: SimpleNamespace, migrator: DB
) -> None:
    good = text_to(PAGE_ID, "m_SIMIN000001")
    forged = webhooks.deliver(good.body, secret="otro-secreto-de-prueba")  # noqa: S106
    assert post(forged).status_code == 401
    odd = social.signed(MESSENGER, social.text(MESSENGER, account="pagina"), entry_id="pagina")
    assert post(odd).status_code == 200  # firmado y raro: contado, nunca llega al resolver
    assert migrator.execute("SELECT count(*) FROM webhook_ingress").fetchone() == (0,)


def test_the_first_signed_event_connects_a_ready_account_but_a_mismatch_does_not(
    social_world: SimpleNamespace,
) -> None:
    ready = {"credential_id": credential(social_world.b), "last_healthcheck_at": timezone.now()}
    with tenant_scope(ctx(social_world.b)):
        ChannelAccount.objects.filter(pk=social_world.other.pk).update(
            status_detail="WEBHOOK_NOT_SEEN", **ready
        )
    post(social.signed(MESSENGER, social.text(MESSENGER), entry_id=OTHER_ACCOUNT_ID))  # cruce
    ingress.process()
    with tenant_scope(ctx(social_world.b)):
        assert ChannelAccount.objects.get(pk=social_world.other.pk).status == "PENDING"
    post(text_to(OTHER_ACCOUNT_ID, "m_SIMIN000002"))
    ingress.process()
    with tenant_scope(ctx(social_world.b)):
        assert ChannelAccount.objects.get(pk=social_world.other.pk).status == "CONNECTED"


def test_the_database_and_the_command_each_refuse_what_the_trigger_alone_would_miss(
    social_world: SimpleNamespace,
) -> None:
    with tenant_scope(ctx(social_world.a)) as tenant, pytest.raises(commands.BadReference):
        commands.record_inbound(  # un hilo de Messenger sin identidad: no solo el disparador
            tenant, contact_id=social_world.contact_a.pk, channel_account_id=social_world.page.pk,
            external_message_id="m_SIMIN000001", body="Hola",
        )  # fmt: skip
    with tenant_scope(ctx(social_world.a)):
        ChannelAccount.objects.filter(pk=social_world.ig.pk).update(deleted_at=timezone.now())
    with tenant_scope(ctx(social_world.a)), pytest.raises(IntegrityError, match="live_account"):
        ContactIdentity.objects.create(
            contact_id=social_world.contact_a.pk, channel_account_id=social_world.ig.pk,
            platform="INSTAGRAM", external_id=social.IGSID,
        )  # fmt: skip
