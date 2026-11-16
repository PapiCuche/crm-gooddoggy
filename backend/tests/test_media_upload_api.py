"""F7-09b2: `POST …/conversations/{id}/media/`, la imagen o el documento en cola. Middleware,
sesión, tope por persona, motor de autorización y PostgreSQL con el rol `crm_app`; el cuerpo se
espía en `HttpRequest.read` (nada lo lee antes del permiso, el alcance y la política). F7-15b: el
antivirus antes de guardar, con el `clamd` falso de `test_antivirus`."""

import logging
import threading
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.http import HttpRequest
from django.test import Client
from django.test.client import encode_multipart
from django.utils import timezone

from apps.accounts.models import User
from apps.conversations import outbound_media, scan
from apps.conversations.api import media as route
from core import storage
from core.observability import reporting
from core.storage.memory import InMemoryStorage
from tests.factories import make_user
from tests.logs import log_text
from tests.test_antivirus import EICAR_PDF, FakeClamd, clamd
from tests.test_authorization import give
from tests.test_conversations_api import BASE, crm, inbox, real_stack
from tests.test_conversations_send_api import set_state
from tests.test_member_branch import DENIED
from tests.test_memberships import join
from tests.test_outbound_media import Flaky
from tests.test_self_context import NOT_FOUND, reply, signed

__all__ = ["clamd", "crm", "inbox", "real_stack"]  # los fixtures, para pytest
pytestmark = pytest.mark.usefixtures("tenant_db", "clamd")
DB = psycopg.Connection[Any]
JPEG, PDF = b"\xff\xd8\xff\xe0" + b"j" * 40, b"%PDF-1.7\n" + b"d" * 40
B = "BoUnDaRy"
COUNT = "SELECT (SELECT count(*) FROM messages), (SELECT count(*) FROM files)"


def named(data: bytes, name: str = "foto.jpg", kind: str = "image/jpeg") -> SimpleUploadedFile:
    return SimpleUploadedFile(name, data, kind)  # el nombre y el tipo, declarados


@pytest.fixture(autouse=True)
def blobs(monkeypatch: pytest.MonkeyPatch) -> InMemoryStorage:
    kept = InMemoryStorage()
    monkeypatch.setattr(storage, "storage", lambda: kept)
    return kept


@pytest.fixture
def reads(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    seen: list[int] = []
    real = HttpRequest.read

    def spy(self: HttpRequest, *args: Any, **kwargs: Any) -> bytes:
        seen.append(1)
        return real(self, *args, **kwargs)

    monkeypatch.setattr(HttpRequest, "read", spy)
    return seen


def form(data: bytes = JPEG, **over: Any) -> dict[str, Any]:
    return {"client_message_id": str(uuid4()), "file": named(data)} | over


def upload(
    client: Client, convo: UUID, sent: Any = None, base: str = BASE, raw: bytes | None = None,
    **meta: Any,
) -> Any:  # fmt: skip
    client.cookies["csrftoken"] = token = "t" * 32
    body = raw if raw is not None else encode_multipart(B, form() if sent is None else sent)
    kind = meta.pop("kind", f"multipart/form-data; boundary={B}")
    return client.generic(
        "POST", f"{base}{convo}/media/", body, kind, headers={"X-CSRFToken": token}, **meta
    )


def test_an_image_and_a_document_are_queued_once_and_a_replay_keeps_only_the_first_blob(
    inbox: SimpleNamespace, blobs: InMemoryStorage, migrator: DB, clamd: FakeClamd
) -> None:
    ana, convo, key = signed(inbox.ana), inbox.convs["nobody"], str(uuid4())
    first = upload(ana, convo, form(caption="Mira 👍🏽", client_message_id=key))
    shown = first.json()
    assert (first.status_code, shown["message_type"], shown["delivery_status"], shown["body"]) == (
        202, "IMAGE", "QUEUED", "Mira 👍🏽",
    )  # fmt: skip
    (kept,) = blobs.objects
    again = upload(ana, convo, form(caption="Mira 👍🏽", client_message_id=key))
    assert (again.status_code, again.json()) == (200, shown)  # el mismo mensaje
    assert list(blobs.objects) == [kept]  # el blob del reintento se borró; el primero sigue
    paper = upload(ana, convo, form(PDF, file=named(PDF, "../Cotización.pdf", "text/html")))
    assert (paper.status_code, paper.json()["message_type"], paper.json()["body"]) == (
        202, "DOCUMENT", "Documento",
    )  # fmt: skip
    row = migrator.execute(
        "SELECT purpose, original_name FROM files WHERE mime_type = %s", ["application/pdf"]
    )
    assert row.fetchone() == ("message-media-out", "Cotización.pdf")
    assert migrator.execute(COUNT).fetchone() == (2, 2) and len(blobs.objects) == 2
    scanned = migrator.execute("SELECT DISTINCT scan_status FROM files").fetchall()
    assert scanned == [("CLEAN",)] and clamd.bodies == [JPEG, JPEG, PDF]  # cada subida, entera


def test_nothing_reads_the_body_before_the_permission_the_scope_and_the_policy(
    inbox: SimpleNamespace, blobs: InMemoryStorage, migrator: DB, reads: list[int], clamd: FakeClamd
) -> None:
    convo, ana = inbox.convs["vera"], signed(inbox.ana)
    assert reply(upload(signed(inbox.bea), convo)) == DENIED  # sin rol
    assert reply(upload(Client(), convo))[0] == 401
    assert reply(upload(signed(inbox.luis), convo)) == NOT_FOUND  # de B, en la ruta de A
    narrow: tuple[dict[str, str | None], ...] = (
        {"messages.send": "ORGANIZATION"}, {"conversations.view": "ORGANIZATION"},
    )  # fmt: skip
    for index, grants in enumerate(narrow):
        user = make_user(email=f"parcial{index}@example.com")
        give(inbox.a, join(inbox.a, user).pk, grants)
        assert reply(upload(signed(user), convo)) == DENIED  # cada permiso solo no basta
    reader = make_user(email="lee@example.com")  # ve toda la organización, envía solo lo suyo
    wide: dict[str, str | None] = {"conversations.view": "ORGANIZATION", "messages.send": "OWN"}
    give(inbox.a, join(inbox.a, reader).pk, wide)
    vera = signed(inbox.vera)
    for client, where in ((reader, inbox.convs["sam"]), (vera, inbox.convs["sam"]),
                          (vera, uuid4()), (ana, uuid4())):  # fmt: skip
        who = signed(client) if isinstance(client, User) else client
        assert reply(upload(who, where)) == NOT_FOUND
    set_state(inbox.a, convo, status="CLOSED", closed_at=timezone.now())
    assert upload(ana, convo).json()["code"] == "CONVERSATION_CLOSED"  # 409
    assert reads == [] and blobs.objects == {} and clamd.bodies == []
    assert migrator.execute(COUNT).fetchone() == (0, 0)


INFECTED = "SELECT action, result, actor_id, metadata FROM audit_logs WHERE entity_id = %s"


def test_an_infected_file_is_a_422_stores_nothing_and_is_audited_and_alerted_without_content(
    inbox: SimpleNamespace, blobs: InMemoryStorage, migrator: DB, clamd: FakeClamd,
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:  # fmt: skip
    seen: list[Any] = []
    capture = lambda *a, **kw: seen.append((a, kw))  # noqa: E731
    monkeypatch.setattr(reporting, "reporter", lambda: type("R", (), {"capture_message": capture}))
    convo = inbox.convs["nobody"]
    with caplog.at_level(logging.INFO):
        refused = upload(signed(inbox.ana), convo, form(EICAR_PDF, file=named(EICAR_PDF,
                         "Cotización secreta.pdf", "application/pdf")))  # fmt: skip
    assert (refused.status_code, refused.json()["code"]) == (422, "MEDIA_INFECTED")
    assert blobs.objects == {} and migrator.execute(COUNT).fetchone() == (0, 0)
    found = migrator.execute(INFECTED + " AND action LIKE 'message.media%%'", [convo]).fetchall()
    assert found == [("message.media_infected", "DENIED", inbox.ana.pk,
                      {"signature": "Eicar-Test-Signature"})]  # fmt: skip
    ((_, context), alert) = seen[0][0], seen[0][1]
    assert alert["fingerprint"] == [scan.ALERT]
    assert context["conversation_id"] == str(convo) and len(seen) == 1
    logged = log_text(caplog)
    for hidden in ("EICAR", "X5O", "secreta", "%PDF"):  # ni bytes ni nombre
        assert hidden not in logged + repr(seen) + refused.content.decode(), hidden


@pytest.mark.parametrize("address", ["", "127.0.0.1:9"])  # sin dirección o caído
def test_without_a_verdict_it_is_a_503_and_nothing_is_stored(
    inbox: SimpleNamespace, blobs: InMemoryStorage, migrator: DB, settings: Any, address: str
) -> None:
    settings.CLAMD_ADDRESS = address
    convo = inbox.convs["nobody"]
    refused = upload(signed(inbox.ana), convo)
    assert (refused.status_code, refused.json()["code"]) == (503, "MEDIA_SCAN_UNAVAILABLE")
    assert blobs.objects == {} and migrator.execute(COUNT).fetchone() == (0, 0)
    assert migrator.execute(INFECTED, [convo]).fetchall() == []  # nada que auditar


def test_a_clean_file_that_fails_to_store_leaves_no_blob_and_no_row(
    inbox: SimpleNamespace, migrator: DB, clamd: FakeClamd, monkeypatch: pytest.MonkeyPatch
) -> None:
    flaky = Flaky()
    monkeypatch.setattr(storage, "storage", lambda: flaky)
    with pytest.raises(OSError, match="cortó"):
        upload(signed(inbox.ana), inbox.convs["nobody"])
    assert clamd.bodies == [JPEG] and flaky.objects == {}
    assert migrator.execute(COUNT).fetchone() == (0, 0)


@pytest.mark.parametrize(
    ("meta", "status"),
    [({"CONTENT_LENGTH": ""}, 411), ({"CONTENT_LENGTH": "1e3"}, 411),
     ({"CONTENT_LENGTH": "١٢"}, 411), ({"CONTENT_LENGTH": str((10 << 20) + (64 << 10) + 1)}, 413)],
)  # fmt: skip
def test_a_body_without_length_or_over_the_cap_is_refused_unread(
    inbox: SimpleNamespace, reads: list[int], meta: dict[str, Any], status: int
) -> None:
    refused = upload(signed(inbox.ana), inbox.convs["nobody"], **meta)
    assert refused.status_code == status and reads == []


def test_the_upload_stops_one_byte_past_the_cap_and_the_type_is_the_real_one(
    inbox: SimpleNamespace, blobs: InMemoryStorage, migrator: DB, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(outbound_media, "CAPS", {"IMAGE": 100, "DOCUMENT": 200})
    kept: list[int] = []  # lo que el manejador llegó a escribir en su temporal
    real = outbound_media.Capped.receive_data_chunk

    def chunk(self: outbound_media.Capped, raw: bytes, start: int) -> None:
        try:
            real(self, raw, start)
        finally:
            kept.append(self.sink.size if self.sink else 0)

    monkeypatch.setattr(outbound_media.Capped, "receive_data_chunk", chunk)
    ana, convo = signed(inbox.ana), inbox.convs["nobody"]
    big = upload(ana, convo, form(PDF + b"d" * 5000))  # corta en el flujo, pasado el tope
    assert (big.status_code, big.json()["code"]) == (413, "MEDIA_TOO_LARGE") and max(kept) <= 200
    image = upload(ana, convo, form(JPEG + b"j" * (101 - len(JPEG))))  # el tope de su tipo
    assert (image.status_code, image.json()["code"]) == (413, "MEDIA_TOO_LARGE")
    for data in (b"GIF89a" + b"g" * 40, b"<html><script>", b""):  # llamado .jpg e image/jpeg
        odd = upload(ana, convo, form(data))
        assert (odd.status_code, odd.json()["code"]) == (415, "MEDIA_TYPE_NOT_ALLOWED"), data
    assert blobs.objects == {} and migrator.execute(COUNT).fetchone() == (0, 0)


ID = str(uuid4())
HOSTILE: list[tuple[Any, list[str]]] = [
    ({"client_message_id": ID, "file": [named(JPEG), named(JPEG)]}, ["file"]),  # dos archivos
    ({"client_message_id": ID, "photo": named(JPEG)}, ["file"]),  # otro nombre
    ({"client_message_id": ID}, ["file"]),  # sin archivo
    ({"client_message_id": [ID, str(uuid4())], "file": named(JPEG)}, ["client_message_id"]),
    (form(organization_id=str(uuid4())), ["organization_id"]),
    (form(caption="x" * 1025), ["caption"]), (form(caption="👍" * 512 + "x"), ["caption"]),
    (form(caption="x\x00y"), ["caption"]), (form(caption="‮"), ["caption"]),
    (form(client_message_id="x"), ["client_message_id"]),
]  # fmt: skip


@pytest.mark.parametrize(("sent", "fields"), [*HOSTILE, (b"{}", ["file"])])
def test_a_hostile_form_is_a_400_on_its_field_and_stores_nothing(
    inbox: SimpleNamespace, blobs: InMemoryStorage, migrator: DB, sent: Any, fields: list[str]
) -> None:
    raw = sent if isinstance(sent, bytes) else None
    kind = {"kind": "application/json"} if raw else {}
    refused = upload(signed(inbox.ana), inbox.convs["nobody"], sent, raw=raw, **kind)
    assert refused.status_code == 400 and sorted(refused.json()["fields"]) == fields
    assert blobs.objects == {} and migrator.execute(COUNT).fetchone() == (0, 0)


def test_a_failure_after_the_command_rolls_back_and_drops_the_blob_and_a_taken_id_too(
    inbox: SimpleNamespace, blobs: InMemoryStorage, migrator: DB, monkeypatch: pytest.MonkeyPatch
) -> None:
    ana, convo, key = signed(inbox.ana), inbox.convs["nobody"], str(uuid4())
    with monkeypatch.context() as patched:
        patched.setattr(route, "shows_body", lambda *a: 1 / 0)  # tras el comando
        with pytest.raises(ZeroDivisionError):
            upload(ana, convo, form(client_message_id=key))
    assert blobs.objects == {} and migrator.execute(COUNT).fetchone() == (0, 0)
    assert upload(ana, convo, form(client_message_id=key)).status_code == 202
    (kept,) = blobs.objects
    sam = signed(inbox.sam)  # otra persona con el mismo id: 409 tras el comando
    taken = upload(sam, inbox.convs["both"], form(client_message_id=key))
    assert (taken.status_code, taken.json()["code"]) == (409, "CLIENT_MESSAGE_ID_TAKEN")
    assert list(blobs.objects) == [kept] and migrator.execute(COUNT).fetchone() == (1, 1)


def test_two_identical_uploads_at_once_leave_one_message_one_file_and_one_blob(
    inbox: SimpleNamespace, blobs: InMemoryStorage, migrator: DB, monkeypatch: pytest.MonkeyPatch
) -> None:
    both, real = threading.Barrier(2, timeout=10), outbound_media.prepare

    def prepared(*args: Any) -> Any:  # las dos guardan su blob antes de que una encole
        made = real(*args)
        both.wait()
        return made

    monkeypatch.setattr(outbound_media, "prepare", prepared)
    key, statuses = str(uuid4()), list[int]()

    def go() -> None:
        try:
            sent = form(client_message_id=key)
            statuses.append(upload(signed(inbox.ana), inbox.convs["nobody"], sent).status_code)
        finally:
            connection.close()

    threads = [threading.Thread(target=go) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert sorted(statuses) == [200, 202]
    assert migrator.execute(COUNT).fetchone() == (1, 1)
    assert list(blobs.objects) == [r[0] for r in migrator.execute("SELECT storage_key FROM files")]


def test_each_person_has_an_upload_limit_counted_before_the_body(
    inbox: SimpleNamespace, migrator: DB, reads: list[int], settings: Any
) -> None:
    settings.MEDIA_UPLOAD_THROTTLE = (1, 600)
    ana, convo = signed(inbox.ana), inbox.convs["nobody"]
    assert upload(ana, convo, CONTENT_LENGTH="").status_code == 411  # cuenta igual
    limited = upload(ana, convo)
    assert (limited.status_code, limited.json()["code"]) == (429, "RATE_LIMITED") and reads == []
    assert 0 < int(limited.headers["Retry-After"]) <= 600
    assert upload(signed(inbox.sam), inbox.convs["both"]).status_code == 202  # el de otra persona
    found = migrator.execute("SELECT key FROM login_throttles WHERE key LIKE 'med:%' ORDER BY key")
    assert found.fetchall() == sorted([(f"med:usr:{inbox.ana.pk}",), (f"med:usr:{inbox.sam.pk}",)])
