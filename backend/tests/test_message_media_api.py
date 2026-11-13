"""F7-09a2: los adjuntos en la lectura de mensajes y `…/attachments/{id}/url/` (la URL firmada).
Middleware, sesión, motor de autorización y PostgreSQL con el rol `crm_app`; los adjuntos se
siembran como el migrador por la máquina de estados real (`0021`, `0022`)."""

import datetime as dt
import logging
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

import boto3
import psycopg
import pytest
from botocore.config import Config
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from core import storage
from core.storage.memory import InMemoryStorage
from core.storage.s3 import S3CompatibleStorage
from tests.test_authorization import give
from tests.test_conversations_api import BASE, UNAUTHENTICATED, crm, inbox, real_stack
from tests.test_conversations_messages_api import SEES, say
from tests.test_member_branch import DENIED
from tests.test_self_context import NOT_FOUND, reply, signed

__all__ = ["crm", "inbox", "real_stack"]  # los fixtures, para pytest
pytestmark = pytest.mark.usefixtures("tenant_db")
DB = psycopg.Connection[Any]
KINDS = {"IMAGE": "image/jpeg", "STICKER": "image/webp", "AUDIO": "audio/ogg", "VIDEO": "video/mp4"}
FILE = (
    "INSERT INTO files (id, organization_id, storage_key, original_name, mime_type, size_bytes, "
    "sha256, purpose, scan_status) VALUES (%s, %s, %s, 'evil.html', %s, 1234, %s, %s, %s)"
)


def attach(
    migrator: DB, org: UUID, convo: UUID, status: str = "DONE", kind: str = "IMAGE",
    mime: str | None = None, purpose: str = "message-media", scan: str = "CLEAN", **over: Any,
) -> SimpleNamespace:  # fmt: skip
    """Un mensaje entrante (u `over`) con su adjunto, llevado a `status` por las transiciones
    permitidas."""
    message = say(org, convo, "Imagen recibida", **({"message_type": "UNSUPPORTED"} | over))
    pk, attempt, run = uuid4(), uuid4(), migrator.execute
    run("INSERT INTO message_attachments (id, organization_id, message_id, kind, "
        "external_media_id, download_status, attempts, created_at, updated_at) VALUES "
        "(%s, %s, %s, %s, '900000000000009', 'PENDING', 0, now(), now())",
        [pk, org, message.pk, kind])  # fmt: skip
    if status == "FAILED":
        run("UPDATE message_attachments SET download_status = 'FAILED', "
            "failure_code = 'TOO_LARGE' WHERE id = %s", [pk])  # fmt: skip
    if status in ("DOWNLOADING", "DONE", "EXPIRED"):
        run("UPDATE message_attachments SET download_status = 'DOWNLOADING', attempts = 1, "
            "attempt_id = %s, lease_until = now() + interval '5 minutes' WHERE id = %s",
            [attempt, pk])  # fmt: skip
    if status == "EXPIRED":
        run("UPDATE message_attachments SET download_status = 'EXPIRED' WHERE id = %s", [pk])
    if status == "DONE":
        key = storage.tenant_key(org, purpose, attempt)
        run(FILE, [attempt, org, key, mime or KINDS[kind], "0" * 64, purpose, scan])
        run("UPDATE message_attachments SET download_status = 'DONE', file_id = %s "
            "WHERE id = %s", [attempt, pk])  # fmt: skip
    return SimpleNamespace(pk=pk, message=message.pk, attempt=attempt, convo=convo)


def url_of(convo: UUID, pk: UUID) -> str:
    return f"{BASE}{convo}/attachments/{pk}/url/"


@pytest.fixture
def blobs(monkeypatch: pytest.MonkeyPatch) -> InMemoryStorage:
    store = InMemoryStorage()
    monkeypatch.setattr(storage, "storage", lambda: store)
    return store


def test_the_message_list_shows_kind_state_real_type_and_size_and_nothing_else(
    inbox: SimpleNamespace, migrator: DB
) -> None:
    convo = inbox.convs["vera"]
    say(inbox.a, convo, "hola")
    ready = attach(migrator, inbox.a, convo)
    waiting = attach(migrator, inbox.a, convo, "PENDING", "AUDIO")
    unscanned = attach(migrator, inbox.a, convo, "DONE", "VIDEO", scan="PENDING")
    smuggled = attach(migrator, inbox.a, convo, purpose="quote-pdf", mime="application/pdf")
    response = signed(inbox.ana).get(f"{BASE}{convo}/messages/")
    rows = {r["id"]: r["attachment"] for r in response.json()["results"]}
    assert sorted(rows.values(), key=str)[0] is None  # el texto, sin adjunto
    assert rows[str(ready.message)] == {
        "id": str(ready.pk), "kind": "IMAGE", "status": "DONE", "mime_type": "image/jpeg",
        "size_bytes": 1234, "available": True,
    }  # fmt: skip
    assert rows[str(waiting.message)] == {
        "id": str(waiting.pk), "kind": "AUDIO", "status": "PENDING", "mime_type": None,
        "size_bytes": None, "available": False,
    }  # fmt: skip
    assert rows[str(unscanned.message)]["mime_type"] == "video/mp4"
    assert rows[str(unscanned.message)]["available"] is False  # antivirus `PENDING`: no
    assert rows[str(smuggled.message)] | {"id": None} == {
        "id": None, "kind": "IMAGE", "status": "DONE", "mime_type": None, "size_bytes": None,
        "available": False,
    }  # fmt: skip  # el archivo de otro propósito ni se describe
    text = response.content.decode()
    for hidden in ("900000000000009", "org/", "evil.html", str(ready.attempt), "quote-pdf"):
        assert hidden not in text, hidden
    vera = signed(inbox.vera)  # su conversación, sin ver al cliente: describe, no ofrece
    seen = {
        r["id"]: r["attachment"] for r in vera.get(f"{BASE}{convo}/messages/").json()["results"]
    }
    assert seen[str(ready.message)]["available"] is False
    give(inbox.a, inbox.m["vera"], SEES)
    seen = {
        r["id"]: r["attachment"] for r in vera.get(f"{BASE}{convo}/messages/").json()["results"]
    }
    assert seen[str(ready.message)]["available"] is True


def test_attachments_cost_two_queries_per_page_whatever_their_number(
    inbox: SimpleNamespace, migrator: DB
) -> None:
    convo, ana = inbox.convs["ana"], signed(inbox.ana)
    attach(migrator, inbox.a, convo)
    with CaptureQueriesContext(connection) as one:
        ana.get(f"{BASE}{convo}/messages/")
    for kind in ("AUDIO", "VIDEO", "STICKER"):
        attach(migrator, inbox.a, convo, kind=kind)
    with CaptureQueriesContext(connection) as four:
        assert len(ana.get(f"{BASE}{convo}/messages/").json()["results"]) == 4
    assert len(four) == len(one)


@pytest.mark.parametrize(("kind", "mime", "name"), [
    ("IMAGE", "image/jpeg", "image.jpg"), ("IMAGE", "image/png", "image.png"),
    ("STICKER", "image/webp", "sticker.webp"), ("AUDIO", "audio/ogg", "audio.ogg"),
    ("AUDIO", "audio/mpeg", "audio.mp3"), ("VIDEO", "video/mp4", "video.mp4"),
    ("VIDEO", "video/3gpp", "video.3gp"),
])  # fmt: skip
def test_the_url_forces_attachment_the_real_type_no_cache_and_a_short_expiry(
    inbox: SimpleNamespace, migrator: DB, blobs: InMemoryStorage, kind: str, mime: str, name: str
) -> None:
    convo = inbox.convs["ana"]
    made = attach(migrator, inbox.a, convo, kind=kind, mime=mime)
    before = timezone.now()
    response = signed(inbox.ana).get(url_of(convo, made.pk))
    assert response.status_code == 200, response.content
    assert response["Cache-Control"] == "private, no-store"
    assert response["X-Content-Type-Options"] == "nosniff"
    body = response.json()
    assert set(body) == {"url", "expires_at"}
    query = parse_qs(urlsplit(body["url"]).query)
    assert urlsplit(body["url"]).path.endswith(str(made.attempt))  # el blob de su intento
    assert query["type"] == [mime] and query["expires"] == ["60"]
    assert query["cache"] == ["private, no-store"]
    assert query["disposition"][0].startswith(f'attachment; filename="{name}"')  # nunca inline
    expires = dt.datetime.fromisoformat(body["expires_at"])
    assert dt.timedelta(seconds=59) <= expires - before <= dt.timedelta(seconds=62)


def test_every_other_case_is_the_same_404_and_the_permission_comes_first(
    inbox: SimpleNamespace, migrator: DB, blobs: InMemoryStorage
) -> None:
    a, mine = inbox.a, inbox.convs["vera"]
    ana, vera = signed(inbox.ana), signed(inbox.vera)
    ok = attach(migrator, a, mine)
    assert reply(vera.get(url_of(mine, ok.pk))) == NOT_FOUND  # sin ver al cliente: ni el medio
    give(a, inbox.m["vera"], SEES)
    assert vera.get(url_of(mine, ok.pk)).status_code == 200  # el caso bueno, para comparar
    other_org = attach(migrator, inbox.b, inbox.convs["b"])
    elsewhere = attach(migrator, a, inbox.convs["sam"])  # de sam: fuera del `OWN` de vera
    cases = {
        "desconocido": url_of(mine, uuid4()),
        "conversación desconocida": url_of(uuid4(), ok.pk),
        "de otra organización": url_of(inbox.convs["b"], other_org.pk),
        "de B por una conversación de A": url_of(mine, other_org.pk),
        "fuera del alcance": url_of(inbox.convs["sam"], elsewhere.pk),
        "de otra conversación": url_of(mine, elsewhere.pk),
    }
    for status in ("PENDING", "DOWNLOADING", "FAILED", "EXPIRED"):
        cases[status] = url_of(mine, attach(migrator, a, mine, status).pk)
    cases["antivirus PENDING"] = url_of(mine, attach(migrator, a, mine, scan="PENDING").pk)
    cases["antivirus INFECTED"] = url_of(mine, attach(migrator, a, mine, scan="INFECTED").pk)
    cases["otro propósito"] = url_of(mine, attach(migrator, a, mine, purpose="quote-pdf").pk)
    cases["tipo fuera del mapa"] = url_of(mine, attach(migrator, a, mine, mime="text/html").pk)
    note = {"direction": "INTERNAL", "message_type": "NOTE", "sender_type": "USER"}
    noted = attach(migrator, a, mine, sender_user_id=inbox.ana.pk, **note)  # no lo deja `attach`
    cases["de una nota"] = url_of(mine, noted.pk)
    for case, url in cases.items():
        assert reply(vera.get(url)) == NOT_FOUND, case
        if case not in ("fuera del alcance", "de otra conversación"):
            assert reply(ana.get(url)) == NOT_FOUND, case  # ni con todo el alcance
    assert reply(signed(inbox.bea).get(url_of(mine, ok.pk))) == DENIED  # sin el permiso: 403
    assert reply(signed(inbox.bea).get(url_of(mine, uuid4()))) == DENIED  # sin mirar nada
    assert reply(Client().get(url_of(mine, ok.pk))) == UNAUTHENTICATED
    assert reply(signed(inbox.luis).get(url_of(mine, ok.pk))) == NOT_FOUND  # de B, no de A


def test_the_signed_url_and_its_signature_never_reach_the_logs(
    inbox: SimpleNamespace, migrator: DB, blobs: InMemoryStorage, caplog: pytest.LogCaptureFixture
) -> None:
    convo = inbox.convs["ana"]
    made = attach(migrator, inbox.a, convo)
    with caplog.at_level(logging.DEBUG):
        url = signed(inbox.ana).get(url_of(convo, made.pk)).json()["url"]
    text = caplog.text + " ".join(str(r.__dict__) for r in caplog.records)
    assert "http.request.completed" in text  # el registro de la petición sí está
    for leaked in (url, "memory://", "disposition", str(made.attempt), "org/"):
        assert leaked not in text, leaked


def test_a_real_s3_signature_carries_only_the_forced_headers_and_the_expiry() -> None:
    secret = "s3cr3t-never-in-a-url-" + "0" * 18  # una clave de prueba, sin red
    client = boto3.client(
        "s3", endpoint_url="https://storage.example.com", region_name="garage",
        aws_access_key_id="GKEXAMPLE", aws_secret_access_key=secret,
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )  # fmt: skip
    key = storage.tenant_key(uuid4(), "message-media", uuid4())
    url = S3CompatibleStorage(bucket="crm", client=client).presign_get(
        key, expires_in=60, filename="image.jpg", content_type="image/jpeg"
    )
    query = parse_qs(urlsplit(url).query)
    assert secret not in url and query["X-Amz-Expires"] == ["60"]
    assert query["response-content-type"] == ["image/jpeg"]
    assert query["response-cache-control"] == ["private, no-store"]
    assert query["response-content-disposition"][0].startswith('attachment; filename="image.jpg"')
    assert set(query) == {
        "X-Amz-Algorithm", "X-Amz-Credential", "X-Amz-Date", "X-Amz-Expires",
        "X-Amz-SignedHeaders", "X-Amz-Signature", "response-content-type",
        "response-content-disposition", "response-cache-control",
    }  # fmt: skip
