"""F7-09b: imagen y documento salientes por el adaptador de WhatsApp, contra la Graph API falsa
(`tests/meta_sim`): el tipo real y el tope, sin antivirus nada, el archivo de la organización, la
misma política que el texto al encolar y al enviar, la subida a Meta que se repite sin llegar al
cliente y el envío que no se reenvía. PostgreSQL real con el rol `crm_app`."""

import dataclasses
import datetime as dt
import hashlib
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from django.db import DatabaseError, connection
from django.utils import timezone

from apps.contacts.consents import revoke_consent
from apps.conversations import commands, media, outbound_media, tasks
from apps.conversations.models import Conversation, Message
from apps.conversations.policy import ConsentRevoked, WindowClosed
from apps.files.models import File
from core import storage
from core.storage.memory import InMemoryStorage
from core.tenancy.scope import tenant_scope
from tests.meta_sim.graph import TEST_TOKEN, FakeGraph
from tests.test_conversation_commands import run
from tests.test_conversations_dispatch import Retrying, state
from tests.test_inventory_stock import race
from tests.test_memberships import ctx
from tests.test_message_media import a_file
from tests.test_whatsapp_outbound import graph, redis, talk, world, writer

__all__ = ["graph", "redis", "world", "writer"]  # los fixtures, para pytest
pytestmark = pytest.mark.usefixtures("tenant_db")
JPEG, PNG = b"\xff\xd8\xff\xe0" + b"j" * 40, b"\x89PNG\r\n\x1a\n" + b"p" * 40
PDF = b"%PDF-1.7\n" + b"d" * 40
FILES = "SELECT storage_key, mime_type, size_bytes, sha256, purpose, scan_status FROM files"


@pytest.fixture(autouse=True)
def blobs(monkeypatch: pytest.MonkeyPatch, settings: Any) -> InMemoryStorage:
    settings.MEDIA_SEND_UNSCANNED = True  # sin antivirus aún; su prueba lo apaga
    kept = InMemoryStorage()
    monkeypatch.setattr(storage, "storage", lambda: kept)
    return kept


def prepare(world: SimpleNamespace, data: bytes = JPEG, name: str | None = None) -> Any:
    sink = media.Sink()
    sink.write(data)
    return outbound_media.prepare(ctx(world.a), sink, name)


def queue(world: SimpleNamespace, convo: Any, data: bytes = JPEG, **over: Any) -> Any:
    made = over.pop("media", None) or prepare(world, data, over.pop("name", None))
    wanted = {
        "conversation_id": convo.pk, "client_message_id": uuid4(), "purpose": "CUSTOMER_REPLY",
        "body": over.pop("caption", None) or outbound_media.LABELS[made.kind], "media": made,
    } | over  # fmt: skip
    return run(world, commands.record_outbound, **wanted)


def window(world: SimpleNamespace, convo: Any, hours: int) -> None:
    with tenant_scope(ctx(world.a)):
        when = timezone.now() - dt.timedelta(hours=hours)
        Conversation.objects.filter(pk=convo.pk).update(last_inbound_at=when)


def deliver(world: SimpleNamespace, message: Any) -> Message:
    tasks.deliver(Retrying(0), world.a, message.pk)
    return state(world, message)


def test_without_antivirus_nothing_is_stored_and_a_type_or_size_not_allowed_never_is(
    world: SimpleNamespace, blobs: InMemoryStorage, settings: Any
) -> None:
    settings.MEDIA_SEND_UNSCANNED = False  # D-F7-04 por omisión: solo `CLEAN`, y no hay antivirus
    with pytest.raises(outbound_media.Refused, match="MEDIA_NOT_SCANNED"):
        prepare(world)
    settings.MEDIA_SEND_UNSCANNED = True
    for data in (b"GIF89a" + b"g" * 40, b"<html>", b"", b"\xff\xd8", b"%PDF", PNG[:7] + b"x" * 40):
        with pytest.raises(outbound_media.Refused, match="MEDIA_TYPE_NOT_ALLOWED"):
            prepare(world, data)  # el tipo sale del contenido, nunca del nombre
    big = b"x" * (outbound_media.CAPS["IMAGE"] - len(JPEG) + 1)
    with pytest.raises(outbound_media.Refused, match="MEDIA_TOO_LARGE"):
        prepare(world, JPEG + big, "foto.pdf")
    assert blobs.objects == {}
    made = prepare(world, PDF + big)  # el tope es del tipo: un documento admite más
    assert made.key.startswith(f"org/{world.a}/message-media/") and list(blobs.objects) == [
        made.key
    ]


@pytest.mark.parametrize(
    ("name", "shown"),
    [
        ("../a\\b‮evil.exe", "bevil.exe.pdf"), (None, "documento.pdf"),
        ("Cotización.PDF", "Cotización.pdf"), ("\t .pdf", "documento.pdf"),
        ("x" * 300 + ".pdf", "x" * 96 + ".pdf"),
    ],
)  # fmt: skip
def test_the_document_name_has_no_path_no_invisible_characters_and_is_always_a_pdf(
    name: str | None, shown: str
) -> None:
    assert outbound_media.filename(name, "DOCUMENT", "application/pdf") == shown
    assert outbound_media.filename(name, "IMAGE", "image/png") == "imagen.png"


def test_the_command_takes_only_a_file_prepared_for_its_organization(
    world: SimpleNamespace, migrator: Any
) -> None:
    convo, made = talk(world), prepare(world)
    sink = media.Sink()
    sink.write(JPEG)
    foreign = outbound_media.prepare(ctx(world.b), sink, None)  # el blob de otra organización
    forged = (
        foreign, dataclasses.replace(made, kind="DOCUMENT"), dataclasses.replace(made, size=0),
        dataclasses.replace(made, size=outbound_media.CAPS["IMAGE"] + 1),
        dataclasses.replace(made, file_id=uuid4()), SimpleNamespace(**dataclasses.asdict(made)),
    )  # fmt: skip
    for bad in forged:
        with pytest.raises(ValueError, match="medio"):
            queue(world, convo, media=bad)
    assert migrator.execute("SELECT count(*) FROM files").fetchone() == (0,)


def test_a_caption_is_at_most_what_whatsapp_takes(world: SimpleNamespace, migrator: Any) -> None:
    convo = talk(world)
    with pytest.raises(ValueError, match="pie de foto"):
        queue(world, convo, caption="x" * (outbound_media.CAPTION_MAX + 1))
    assert migrator.execute("SELECT count(*) FROM files").fetchone() == (0,)
    message, _ = queue(world, convo, caption="x" * outbound_media.CAPTION_MAX)
    assert len(message.body) == outbound_media.CAPTION_MAX


def test_an_image_is_queued_with_its_file_once_and_a_replay_needs_the_same_file(
    world: SimpleNamespace, migrator: Any
) -> None:
    convo, key = talk(world), uuid4()
    message, created = queue(world, convo, caption="Mira esto", client_message_id=key)
    assert created and (message.message_type, message.delivery_status) == ("IMAGE", "QUEUED")
    assert message.body == "Mira esto" and message.file_id is not None
    row = migrator.execute(FILES + " WHERE id = %s", [message.file_id]).fetchone()
    assert row[1:] == ("image/jpeg", len(JPEG), hashlib.sha256(JPEG).hexdigest(),
                       "message-media", "PENDING")  # fmt: skip
    again, created = queue(world, convo, caption="Mira esto", client_message_id=key)
    assert (again.pk, created) == (message.pk, False)  # el mismo archivo: el mismo mensaje
    for other in ({"data": PNG, "caption": "Mira esto"}, {"caption": "Otra"}):
        with pytest.raises(commands.ClientMessageConflict):
            queue(world, convo, client_message_id=key, **other)
    text: dict[str, Any] = {
        "conversation_id": convo.pk,
        "purpose": "CUSTOMER_REPLY",
        "body": "Mira esto",
    }
    with pytest.raises(commands.ClientMessageConflict):  # un texto con el id de un medio
        run(world, commands.record_outbound, client_message_id=key, **text)
    said, _ = run(world, commands.record_outbound, client_message_id=uuid4(), **text)
    with pytest.raises(commands.ClientMessageConflict):  # y un medio con el id de un texto
        queue(world, convo, caption="Mira esto", client_message_id=said.client_message_id)
    assert migrator.execute("SELECT count(*) FROM files").fetchone() == (1,)


def test_two_identical_media_replies_at_once_store_one_message_and_one_file(
    world: SimpleNamespace, migrator: Any
) -> None:
    convo, key = talk(world), uuid4()
    files = [prepare(world), prepare(world)]  # cada petición con su blob

    def reply(index: int) -> Any:
        wanted: dict[str, Any] = {
            "conversation_id": convo.pk,
            "client_message_id": key,
            "body": "Imagen",
        }
        return lambda tenant: commands.record_outbound(
            tenant, purpose="CUSTOMER_REPLY", media=files[index], **wanted
        )

    first, second = race(world.a, world.ana, [reply(0), reply(1)])
    assert first[0].pk == second[0].pk and (first[1], second[1]) == (True, False)
    assert migrator.execute("SELECT count(*) FROM files").fetchone() == (1,)


def test_the_window_and_the_consent_hold_for_media_as_for_text(
    world: SimpleNamespace, graph: FakeGraph, migrator: Any
) -> None:
    convo = talk(world)
    window(world, convo, 25)
    with pytest.raises(WindowClosed):
        queue(world, convo)
    window(world, convo, 1)
    run(world, revoke_consent, contact_id=world.contact_a.pk, channel="WHATSAPP",
        purpose="ALL_PROACTIVE", source="KEYWORD_STOP")  # fmt: skip
    with pytest.raises(ConsentRevoked):  # lo proactivo no sale a quien se dio de baja
        queue(world, convo, purpose="FOLLOW_UP")
    assert migrator.execute("SELECT count(*) FROM files").fetchone() == (0,)
    message, _ = queue(world, convo)  # responder dentro de la ventana sí
    window(world, convo, 25)  # y el despachador la vuelve a mirar antes de subir nada
    saved = deliver(world, message)
    assert (saved.delivery_status, saved.provider_error_code) == ("FAILED", "WINDOW_CLOSED")
    assert graph.requests == []


def test_an_image_and_a_document_go_out_through_the_adapter_after_their_upload(
    world: SimpleNamespace, graph: FakeGraph
) -> None:
    convo = talk(world)
    image, _ = queue(world, convo, caption="Mira esto")
    document, _ = queue(world, convo, PDF, name="Cotización 12.pdf")
    assert deliver(world, image).delivery_status == "SENT"
    assert deliver(world, document).delivery_status == "SENT"
    upload, call = graph.calls[0], graph.calls[1]
    assert upload["path"].endswith("/media") and call["path"].endswith("/messages")
    assert upload["headers"]["Authorization"] == f"Bearer {TEST_TOKEN}"
    assert (
        JPEG in graph.uploads[0] and b"image/jpeg" in graph.uploads[0] and PDF in graph.uploads[1]
    )
    sent, paper = graph.sent
    assert (sent["type"], sent["image"], "text" in sent) == (
        "image", {"id": "770001", "caption": "Mira esto"}, False,
    )  # fmt: skip
    assert sent["biz_opaque_callback_data"] == str(image.pk)
    assert paper["document"] == {"id": "770002", "filename": "Cotización 12.pdf"}  # sin pie


@pytest.mark.parametrize(
    ("unscanned", "scan", "outcome"),
    [(False, "PENDING", "FAILED"), (True, "INFECTED", "FAILED"), (False, "CLEAN", "SENT"),
     (True, "PENDING", "SENT")],
)  # fmt: skip
def test_the_antivirus_is_checked_again_just_before_sending(
    world: SimpleNamespace, graph: FakeGraph, migrator: Any, settings: Any, unscanned: bool,
    scan: str, outcome: str,
) -> None:  # fmt: skip
    message, _ = queue(world, talk(world))
    migrator.execute("UPDATE files SET scan_status = %s", [scan])
    settings.MEDIA_SEND_UNSCANNED = unscanned
    saved = deliver(world, message)
    assert saved.delivery_status == outcome
    if outcome == "FAILED":
        assert saved.provider_error_code == "MEDIA_NOT_SCANNED" and graph.requests == []


@pytest.mark.parametrize("outcome", ["cut", "timeout", "server", "rate_limit", "cut_after"])
def test_an_upload_that_fails_never_reaches_the_customer_and_is_retried(
    world: SimpleNamespace, graph: FakeGraph, migrator: Any, outcome: str
) -> None:
    message, _ = queue(world, talk(world))
    graph.then(outcome)
    with pytest.raises(RuntimeError, match="retry"):
        deliver(world, message)
    assert state(world, message).delivery_status == "QUEUED" and graph.sent == []
    migrator.execute("UPDATE messages SET not_before = NULL")  # pasada su espera
    assert deliver(world, message).delivery_status == "SENT" and len(graph.sent) == 1


@pytest.mark.parametrize(("outcome", "reason"), [("token", "TOKEN_INVALID"),
                                                 ("no_number", "PROVIDER_REJECTED")])  # fmt: skip
def test_an_upload_that_meta_refuses_fails_with_a_closed_reason(
    world: SimpleNamespace, graph: FakeGraph, outcome: str, reason: str
) -> None:
    message, _ = queue(world, talk(world))
    graph.then(outcome)
    saved = deliver(world, message)
    assert (saved.delivery_status, saved.provider_error_code, graph.sent) == ("FAILED", reason, [])


def test_an_upload_reply_without_a_numeric_id_is_retried_and_nothing_is_sent(
    world: SimpleNamespace, graph: FakeGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    message, _ = queue(world, talk(world))
    real = graph.handle

    def odd(method: str, target: str, *args: Any) -> Any:
        status, headers, raw = real(method, target, *args)
        return (status, headers, b'{"id": "../12345"}') if target.endswith("/media") else (
            status, headers, raw,
        )  # fmt: skip

    monkeypatch.setattr(graph, "handle", odd)
    with pytest.raises(RuntimeError, match="retry"):
        deliver(world, message)
    assert state(world, message).delivery_status == "QUEUED" and graph.sent == []


def test_a_send_lost_after_the_upload_is_never_resent(
    world: SimpleNamespace, graph: FakeGraph
) -> None:
    message, _ = queue(world, talk(world))
    graph.then("ok", "timeout_after")  # la subida sale bien; la respuesta del envío se pierde
    assert deliver(world, message).delivery_status == "SENDING"
    assert deliver(world, message).delivery_status == "SENDING"  # otro intento: nada
    assert (len(graph.uploads), len(graph.sent)) == (1, 1)


def test_a_changed_or_missing_blob_or_a_channel_that_is_off_sends_nothing(
    world: SimpleNamespace, graph: FakeGraph, blobs: InMemoryStorage, settings: Any
) -> None:
    convo = talk(world)
    (changed, _), (missing, _), (off, _) = (queue(world, convo) for _ in range(3))
    with tenant_scope(ctx(world.a)):
        keys = {f.pk: f.storage_key for f in File.objects.all()}
    data, info = blobs.objects[keys[changed.file_id]]
    blobs.objects[keys[changed.file_id]] = (data + b"!", info)  # otro contenido, otro `sha256`
    saved = deliver(world, changed)
    assert (saved.delivery_status, saved.provider_error_code) == ("FAILED", "ADAPTER_REJECTED")
    del blobs.objects[keys[missing.file_id]]
    with pytest.raises(RuntimeError, match="retry"):  # el almacén falla: se repite
        deliver(world, missing)
    settings.META_ENABLED = False  # apagado: ni se lee el blob
    saved = deliver(world, off)
    assert (saved.delivery_status, saved.provider_error_code) == ("FAILED", "CHANNEL_UNAVAILABLE")
    assert graph.requests == []


INSERT = (
    "INSERT INTO messages (id, organization_id, conversation_id, channel_account_id, direction, "
    "sender_type, message_type, body, delivery_status, file_id, created_at) SELECT "
    "gen_random_uuid(), organization_id, conversation_id, channel_account_id, %s, %s, %s, 'x', "
    "%s, %s, now() FROM messages LIMIT 1"
)
VALID = (
    "SELECT count(*) FROM pg_constraint WHERE convalidated AND conname IN ('messages_file_ck', "
    "'messages_file_fk', 'messages_type_ck', 'messages_unsupported_ck', 'messages_error_code_ck')"
)


def test_the_database_ties_a_file_of_the_same_organization_to_an_outbound_media_message_only(
    world: SimpleNamespace, migrator: Any
) -> None:
    message, _ = queue(world, talk(world))
    mine, theirs = message.file_id, a_file(migrator, world.b, uuid4())
    out, inb = ("OUTBOUND", "SYSTEM"), ("INBOUND", "CONTACT")
    for direction, typed, state_, file, rule in (
        (out, "IMAGE", "QUEUED", None, "messages_file_ck"),
        (out, "DOCUMENT", "QUEUED", None, "messages_file_ck"),
        (inb, "DOCUMENT", None, None, "messages_file_ck"),  # un documento nunca es entrante
        (inb, "IMAGE", None, mine, "messages_file_ck"),
        (out, "TEXT", "QUEUED", mine, "messages_file_ck"),
        (out, "IMAGE", "QUEUED", theirs, "messages_file_fk"),  # el archivo de otra organización
        (out, "IMAGE", "QUEUED", uuid4(), "messages_file_fk"),
    ):  # fmt: skip
        with pytest.raises(DatabaseError) as caught:
            with tenant_scope(ctx(world.a)), connection.cursor() as cursor:
                cursor.execute(INSERT, [*direction, typed, state_, file])
        assert caught.value.__cause__.diag.constraint_name == rule  # type: ignore[union-attr]
    with pytest.raises(DatabaseError) as caught:  # y nadie le cambia el archivo a un mensaje
        with tenant_scope(ctx(world.a)), connection.cursor() as cursor:
            cursor.execute("UPDATE messages SET file_id = %s", [mine])
    assert type(caught.value.__cause__).__name__ == "InsufficientPrivilege"
    assert migrator.execute(VALID).fetchone() == (5,)
    found = "SELECT indexdef FROM pg_indexes WHERE indexname = 'messages_file_idx'"
    assert "WHERE (file_id IS NOT NULL)" in migrator.execute(found).fetchone()[0]
