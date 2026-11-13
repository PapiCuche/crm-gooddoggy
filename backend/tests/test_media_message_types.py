"""F7-09a2b: el `message_type` de un medio entrante (`IMAGE`, `AUDIO`, `VIDEO`; migraciones `0024`
y `0025`), que nace con su adjunto en la misma transacción, y la base que lo deja solo para lo
entrante, como `crm_app` con SQL crudo. PostgreSQL real; Graph y almacén del simulador."""

from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from django.core.management import call_command
from django.db import DatabaseError, connection

from apps.contacts.models import Contact
from apps.conversations import commands, inbound, media
from core.tenancy.scope import tenant_scope
from tests.test_memberships import ctx
from tests.test_message_media import MEDIA, attachments, receive, world
from tests.test_pipelines_tables import as_migrator

__all__ = ["world"]  # la fixture, para pytest
pytestmark = pytest.mark.usefixtures("tenant_db")
DB = psycopg.Connection[Any]
ROWS = (
    "SELECT m.message_type, m.body, a.kind FROM messages m "
    "LEFT JOIN message_attachments a ON a.message_id = m.id ORDER BY m.created_at"
)


@pytest.mark.parametrize(("kind", "typed", "label", "attached"), [
    ("image", "IMAGE", "Imagen recibida", "IMAGE"), ("audio", "AUDIO", "Audio recibido", "AUDIO"),
    ("video", "VIDEO", "Video recibido", "VIDEO"),
    ("sticker", "IMAGE", "Sticker recibido", "STICKER"),  # una imagen; el adjunto dice cuál
])  # fmt: skip
def test_each_media_webhook_has_its_type_the_fixed_label_and_one_attachment(
    world: Any, migrator: DB, kind: str, typed: str, label: str, attached: str
) -> None:
    receive(world, kind)
    receive(world, kind)  # Meta lo repite: el mismo mensaje, sin otro adjunto
    assert migrator.execute(ROWS).fetchall() == [(typed, label, attached)]
    assert label == inbound.LABELS[kind]  # el cuerpo es la frase fija, nunca lo que declara Meta


@pytest.mark.parametrize(("kind", "media_id"), [
    ("image", "SIMMEDIA0001"), ("image", "12"), ("image", "../../v1/me"),
    ("audio", "2000000000000011?fields=x"), ("video", ""), ("document", MEDIA),
    ("location", MEDIA),
])  # fmt: skip
def test_without_a_valid_media_id_or_a_media_kind_it_stays_unsupported_without_attachment(
    world: Any, migrator: DB, kind: str, media_id: str
) -> None:
    receive(world, kind, media_id)
    [(typed, body, attached)] = migrator.execute(ROWS).fetchall()
    assert (typed, attached) == ("UNSUPPORTED", None)
    assert body == inbound.LABELS[kind] and "evil" not in body


def test_a_media_payload_that_is_not_an_object_stays_unsupported(world: Any) -> None:
    for broken in ({"image": "x"}, {"image": {"id": 200000000000001}}, {"image": None}, {}):
        assert media.found("image", broken) is None
    assert media.found("text", {"text": {"id": MEDIA}}) is None
    assert media.found("image", {"image": {"id": MEDIA}}) == ("IMAGE", MEDIA)


def record(org: UUID, account: UUID, message_type: str, attachment: Any) -> Any:
    with tenant_scope(ctx(org)) as tenant:
        who = Contact.objects.get()  # el del webhook previo
        return commands.record_inbound(
            tenant, contact_id=who.pk, channel_account_id=account, external_message_id="wamid.X",
            body="Imagen recibida", message_type=message_type, attachment=attachment,
        )  # fmt: skip


@pytest.mark.parametrize(("message_type", "attachment"), [
    ("IMAGE", None), ("AUDIO", None), ("TEXT", ("IMAGE", MEDIA)),
    ("UNSUPPORTED", ("IMAGE", MEDIA)), ("AUDIO", ("IMAGE", MEDIA)), ("VIDEO", ("STICKER", MEDIA)),
    ("IMAGE", ("IMAGE", "12")), ("IMAGE", ("IMAGE", "../me")), ("IMAGE", ("DOCUMENT", MEDIA)),
    ("NOTE", None), ("NOTE", ("IMAGE", MEDIA)), ("STICKER", ("STICKER", MEDIA)),
])  # fmt: skip
def test_record_inbound_takes_a_media_type_only_with_its_own_valid_attachment(
    world: Any, migrator: DB, message_type: str, attachment: Any
) -> None:
    receive(world, "location", wamid="wamid.SEED")  # el contacto y la conversación
    with pytest.raises(ValueError, match="tipo"):
        record(world.a, world.account, message_type, attachment)
    assert migrator.execute("SELECT count(*) FROM messages").fetchone() == (1,)  # nada escrito
    assert attachments(migrator) == []
    saved, created = record(world.a, world.account, "IMAGE", ("STICKER", MEDIA))  # el caso bueno
    assert created and saved.message_type == "IMAGE" and len(attachments(migrator)) == 1


def test_a_failing_attachment_rolls_back_the_message_its_audit_and_its_events(
    world: Any, migrator: DB, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: Any) -> None:
        raise DatabaseError("adjunto")

    monkeypatch.setattr(media, "attach", broken)
    with pytest.raises(DatabaseError, match="adjunto"):
        receive(world)
    for table in ("messages", "message_attachments", "conversations", "outbox_events"):
        assert migrator.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,), table  # noqa: S608
    audit = "SELECT count(*) FROM audit_logs WHERE action = 'message.received'"
    assert migrator.execute(audit).fetchone() == (0,)


INSERT = (
    "INSERT INTO messages (id, organization_id, conversation_id, channel_account_id, direction, "
    "sender_type, sender_user_id, message_type, body, delivery_status, created_at) SELECT "
    "gen_random_uuid(), organization_id, conversation_id, channel_account_id, %s, %s, %s, %s, "
    "'x', %s, now() FROM messages LIMIT 1"
)


def raw(org: UUID, sql: str, params: list[Any]) -> None:
    with tenant_scope(ctx(org)), connection.cursor() as cursor:
        cursor.execute(sql, params)


@pytest.mark.parametrize("typed", ["IMAGE", "AUDIO", "VIDEO", "UNSUPPORTED"])
def test_the_database_keeps_media_types_inbound_for_the_app_role(
    world: Any, migrator: DB, typed: str
) -> None:
    receive(world)
    user = uuid4()  # los CHECK van antes que la FK del remitente
    out = "messages_file_ck" if typed == "IMAGE" else "messages_unsupported_ck"  # sin su archivo
    for direction, sender, by, state, rule in (
        ("OUTBOUND", "USER", user, "QUEUED", out),
        ("OUTBOUND", "SYSTEM", None, "QUEUED", out),
        ("INTERNAL", "USER", user, None, "messages_note_ck"),  # lo interno es una nota, antes
    ):  # fmt: skip
        with pytest.raises(DatabaseError) as caught:
            raw(world.a, INSERT, [direction, sender, by, typed, state])
        assert caught.value.__cause__.diag.constraint_name == rule  # type: ignore[union-attr]
    raw(world.a, INSERT, ["INBOUND", "CONTACT", None, typed, None])  # entrante: sí
    with pytest.raises(DatabaseError) as caught:  # y nadie le cambia el tipo a un mensaje
        raw(world.a, "UPDATE messages SET message_type = 'TEXT'", [])
    assert type(caught.value.__cause__).__name__ == "InsufficientPrivilege"


@pytest.mark.parametrize("typed", ["STICKER", "PDF", "image", ""])
def test_the_database_refuses_any_other_type(world: Any, typed: str) -> None:
    receive(world)
    with pytest.raises(DatabaseError) as caught:
        raw(world.a, INSERT, ["INBOUND", "CONTACT", None, typed, None])
    assert caught.value.__cause__.diag.constraint_name == "messages_type_ck"  # type: ignore[union-attr]


VALID = (
    "SELECT conname, convalidated FROM pg_constraint WHERE conname IN "
    "('messages_type_ck', 'messages_unsupported_ck') ORDER BY conname"
)


def test_the_migrations_validate_both_checks_and_going_back_keeps_every_message(
    world: Any, migrator: DB
) -> None:
    assert migrator.execute(VALID).fetchall() == [
        ("messages_type_ck", True), ("messages_unsupported_ck", True)
    ]  # fmt: skip
    receive(world, "location")  # sin medios aún: atrás y adelante sin perder nada (el ORM ya
    with as_migrator():  # tiene `file_id`, de la `0027`: el mensaje se escribe antes de volver)
        call_command("migrate", "conversations", "0022", verbosity=0)
    assert {v for _, v in migrator.execute(VALID).fetchall()} == {True}  # los de `0010`
    with as_migrator():
        call_command("migrate", "conversations", "0024", verbosity=0)
    assert {v for _, v in migrator.execute(VALID).fetchall()} == {False}  # rigen, sin recorrer
    with as_migrator():
        call_command("migrate", "conversations", verbosity=0)
    receive(world, wamid="wamid.M2")
    with as_migrator(), pytest.raises(DatabaseError):  # un medio: el CHECK viejo no lo admite
        call_command("migrate", "conversations", "0022", verbosity=0)
    assert [r[0] for r in migrator.execute(ROWS).fetchall()] == ["UNSUPPORTED", "IMAGE"]
    with as_migrator():
        call_command("migrate", "conversations", verbosity=0)
        with connection.cursor() as cursor:  # sin `lock_timeout` colgado en la conexión
            cursor.execute("SHOW lock_timeout")
            assert cursor.fetchone() == ("0",)
    assert migrator.execute(VALID).fetchall() == [
        ("messages_type_ck", True), ("messages_unsupported_ck", True)
    ]  # fmt: skip
