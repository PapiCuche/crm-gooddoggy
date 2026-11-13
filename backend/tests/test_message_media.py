"""F7-09a1b a F7-09a1d: el adjunto de un medio, su máquina de estados (`media`, `0020` a `0022`,
`0026`, `files.0002`), su descarga (`media.fetch`, `tasks.fetch_media`) y el barredor de los varados
(`tasks.sweep_media`), con PostgreSQL real como `crm_app`, hilos a la vez, la Graph API del
simulador por `core.http` y un almacén en memoria."""

import datetime as dt
import hashlib
import importlib
import io
import logging
import pathlib
import re
import socket
import threading
import time
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
import urllib3
from django.core.management import call_command
from django.db import DatabaseError, connection, transaction

from apps.conversations import credentials, inbound, media, tasks
from apps.conversations.models import ChannelAccount, Message
from config.celery import app
from core import http, storage
from core.outbox import handlers_for
from core.storage.memory import InMemoryStorage
from core.tenancy.context import ActorType, TenantContext
from core.tenancy.scope import tenant_scope
from tests.meta_sim import GRAPH_VERSION, transport
from tests.meta_sim.graph import TEST_TOKEN, FakeGraph, FakeMedia
from tests.meta_sim.webhooks import CUSTOMER, MEDIA_HOST, PHONE_NUMBER_ID, WABA_ID
from tests.test_memberships import ctx
from tests.test_pipelines_tables import as_migrator

pytestmark = pytest.mark.usefixtures("tenant_db")
DB = psycopg.Connection[Any]
MEDIA = "200000000000001"
JPEG = b"\xff\xd8\xff\xe0" + bytes(60)
FTYP = (24).to_bytes(4, "big") + b"ftyp"
MP3 = bytes([0xFF, 0xFB, 0x90, 0x64]) + bytes(40)
ID3 = b"ID3\x04\x00\x00\x00\x00\x00\x0a" + bytes(10)
HTML = b"<html><script>alert(1)</script></html>"
LOGGED = {"event", "level", "timestamp", "logger", "request_id", "correlation_id"}  # y los ids
RETRY = "RETRY"
STATE = "SELECT download_status, failure_code, attempts FROM message_attachments WHERE id = %s"


class Storage(InMemoryStorage):  # compartido por la prueba, con fallos a la carta
    fail_put = fail_delete = False

    def put(self, key: str, *args: Any, **kwargs: Any) -> Any:
        if self.fail_put:  # como algunos backends: deja el objeto a medias y falla
            self.objects[key] = (b"\xff", None)  # type: ignore[assignment]
            raise OSError("put")
        return super().put(key, *args, **kwargs)

    def delete(self, key: str) -> None:
        if self.fail_delete:
            raise OSError("delete")
        super().delete(key)


@pytest.fixture
def world(orgs: dict[str, UUID], settings: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    settings.META_ENABLED, settings.META_GRAPH_VERSION = True, GRAPH_VERSION
    settings.HTTP_ALLOWED_HOSTS, settings.MEDIA_PER_ORG = [transport.GRAPH_HOST], 2
    graph, blobs = FakeGraph(), Storage()
    graph.media[MEDIA] = FakeMedia(JPEG)
    transport.install(monkeypatch, graph)
    monkeypatch.setattr(storage, "storage", lambda: blobs)
    mine = uuid4()
    with tenant_scope(ctx(orgs["A"])) as tenant:
        stored = credentials.store(tenant, mine, TEST_TOKEN)
        ChannelAccount.objects.create(
            id=mine, platform="WHATSAPP", name="WA", status="CONNECTED", credential_id=stored.pk,
            external_account_id=PHONE_NUMBER_ID, external_parent_id=WABA_ID,
            last_healthcheck_at="2026-11-01T00:00:00Z",
        )  # fmt: skip
    return SimpleNamespace(a=orgs["A"], b=orgs["B"], account=mine, graph=graph, blobs=blobs)


def receive(w: Any, kind: str = "image", media_id: str = MEDIA, wamid: str = "wamid.M1") -> None:
    """Un medio entrante: `inbound.receive` crea el mensaje y, si es nuevo, su adjunto."""
    declared = {"id": media_id, "mime_type": "text/html", "filename": "evil.html", "sha256": "f"}
    payload = {"from": CUSTOMER, "id": wamid, "timestamp": "1700000000", "type": kind,
               kind: declared}  # fmt: skip
    context = TenantContext(w.a, "webhook", actor_type=ActorType.INTEGRATION)
    with tenant_scope(context):
        inbound.receive(context, ChannelAccount.objects.get(pk=w.account), payload)


def attachments(migrator: DB) -> list[UUID]:
    rows = migrator.execute("SELECT id FROM message_attachments ORDER BY created_at").fetchall()
    return [row[0] for row in rows]


def one(
    w: Any, migrator: DB, wamid: str = "wamid.M1", content: bytes = JPEG, kind: str = "image"
) -> UUID:
    w.graph.media[MEDIA] = FakeMedia(content)
    receive(w, kind, wamid=wamid)
    return attachments(migrator)[-1]


def state(migrator: DB, pk: UUID) -> tuple[Any, ...]:
    return migrator.execute(STATE, [pk]).fetchone()  # type: ignore[return-value]


TRIGGER = "ALTER TABLE message_attachments {} TRIGGER message_attachments_advance"


def clock(migrator: DB, sql: str, params: Any) -> None:
    """El reloj a mano (`0026` lo prohíbe a todos): solo el dueño de la tabla, con el disparador
    apagado dentro de una transacción; si algo falla, el ROLLBACK lo deja encendido."""
    with migrator.transaction():
        migrator.execute(TRIGGER.format("DISABLE"))
        migrator.execute(sql, params)
        migrator.execute(TRIGGER.format("ENABLE"))


def due(migrator: DB, pk: UUID) -> None:  # el plazo o la espera, vencidos: el reloj pasa
    clock(migrator, "UPDATE message_attachments SET lease_until = now() - interval '1 second', "
          "not_before = NULL WHERE id = %s", [pk])  # fmt: skip


def files(migrator: DB) -> list[tuple[Any, ...]]:
    sql = "SELECT id, storage_key, original_name, mime_type, size_bytes, sha256, scan_status"
    return migrator.execute(f"{sql}, purpose FROM files").fetchall()


def claimed(org: UUID, pk: UUID) -> Any:
    with tenant_scope(ctx(org)) as tenant:
        return media.claim(tenant, pk)


def bytes_of(content: bytes = JPEG) -> media.Sink:
    (sink := media.Sink()).write(content)
    return sink


def store(w: Any, got: media.Claim, content: bytes = JPEG) -> tuple[str, int | None]:
    return media.store(TenantContext(w.a, "celery"), got, bytes_of(content), "image/jpeg")


def finish(w: Any, got: media.Claim, outcome: str) -> tuple[str, int | None]:
    with tenant_scope(ctx(w.a)) as tenant:
        return media.finish(tenant, got, outcome)


def run_at_once(work: Any, count: int = 2) -> list[Any]:
    start, outcome = threading.Barrier(count), []

    def body() -> None:
        try:
            start.wait(timeout=10)
            outcome.append(work())
        except Exception as error:  # noqa: BLE001 — lo comprueba el test
            outcome.append(type(error).__name__)
        finally:
            connection.close()

    threads = [threading.Thread(target=body) for _ in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    return outcome


# ---- El adjunto y un intento que termina bien ----


def test_a_media_message_gets_a_pending_attachment_and_a_stored_attempt_leaves_it_done(
    world: Any, migrator: DB
) -> None:
    pk = one(world, migrator)
    assert state(migrator, pk) == ("PENDING", None, 0)
    events = (
        "SELECT aggregate_id FROM outbox_events WHERE event_type = 'message_attachment.pending'"
    )
    assert migrator.execute(events).fetchall() == [(pk,)]
    receive(world)  # Meta repite el webhook: ni otro adjunto, ni otro evento, ni un error
    assert attachments(migrator) == [pk] and len(migrator.execute(events).fetchall()) == 1
    got = claimed(world.a, pk)
    assert (got.kind, got.media_id, got.number) == ("IMAGE", MEDIA, PHONE_NUMBER_ID)
    assert state(migrator, pk) == ("DOWNLOADING", None, 1)
    with pytest.raises(ValueError, match="tipo"):  # un tipo fuera de la lista: ni se sube
        media.store(TenantContext(world.a, "celery"), got, bytes_of(), "text/html")
    assert (
        world.blobs.objects == {}
        and store(world, got) == ("DONE", None)
        and state(migrator, pk) == ("DONE", None, 1)
    )
    ((file_id, key, *rest),) = files(migrator)
    sha = hashlib.sha256(JPEG).hexdigest()  # el calculado; nada de lo que declara Meta
    assert rest == ["image.jpg", "image/jpeg", len(JPEG), sha, "PENDING", "message-media"]
    assert file_id == got.attempt_id and key.endswith(f"/{file_id}")
    assert world.blobs.objects[key][0] == JPEG
    assert migrator.execute("SELECT file_id FROM message_attachments").fetchone() == (file_id,)
    assert claimed(world.a, pk) is None  # cerrado: no se vuelve a reclamar


@pytest.mark.parametrize(("kind", "media_id"), [
    ("document", MEDIA), ("location", MEDIA), ("image", "../../v1/me"), ("image", "12"),
    ("image", "2000000000000011?fields=x"),
])  # fmt: skip
def test_documents_other_kinds_and_bad_ids_leave_no_attachment(
    world: Any, migrator: DB, kind: str, media_id: str
) -> None:
    receive(world, kind, media_id)
    assert attachments(migrator) == []


def test_two_attachments_or_two_attempts_at_once_make_one_row_and_one_blob(
    world: Any, migrator: DB
) -> None:
    receive(world)
    (pk,) = attachments(migrator)
    with tenant_scope(ctx(world.a)) as tenant:
        message = Message.objects.get()
        with pytest.raises(DatabaseError) as caught:  # un adjunto por mensaje, en la base
            media.attach(tenant, message, "IMAGE", MEDIA)
    assert caught.value.__cause__.diag.constraint_name == "message_attachments_message_uq"  # type: ignore[union-attr]

    def attempt() -> Any:
        got = claimed(world.a, pk)
        return store(world, got) if isinstance(got, media.Claim) else got

    outcome = run_at_once(attempt, count=3)
    assert outcome.count(("DONE", None)) == 1, outcome
    assert all(o is None or isinstance(o, int) for o in outcome if o != ("DONE", None)), outcome
    assert len(world.blobs.objects) == 1 and len(files(migrator)) == 1


# ---- Cerrar un intento sin archivo: reintento con espera, caducado, fallido ----


@pytest.mark.parametrize(
    ("outcome", "expected", "wait"),
    [
        ("RETRY", ("PENDING", None, 1), 30),
        ("EXPIRED", ("EXPIRED", None, 1), None),
        ("TOKEN_INVALID", ("FAILED", "TOKEN_INVALID", 1), None),
        ("TYPE_MISMATCH", ("FAILED", "TYPE_MISMATCH", 1), None),
    ],
)
def test_an_attempt_without_a_file_closes_with_its_outcome(
    world: Any, migrator: DB, outcome: str, expected: tuple[Any, ...], wait: int | None
) -> None:
    pk = one(world, migrator)
    assert finish(world, claimed(world.a, pk), outcome) == (outcome, wait)
    assert state(migrator, pk) == expected and files(migrator) == []


def test_retries_wait_longer_each_time_until_the_last_attempt_fails_unavailable(
    world: Any, migrator: DB
) -> None:
    pk = one(world, migrator)
    waits = []
    for _ in range(media.MAX_ATTEMPTS):
        got = claimed(world.a, pk)
        assert claimed(world.a, pk) is None  # ni un segundo intento a la vez: el duplicado acaba
        waits.append(finish(world, got, "RETRY")[1])
        if waits[-1]:
            early = claimed(world.a, pk)  # ni antes de su espera: vuelve si llega poco antes
            if waits[-1] <= media.EARLY:
                assert isinstance(early, int) and waits[-1] - 2 <= early <= waits[-1] + 1
            else:
                assert early is None  # mucho antes: un duplicado, termina
            due(migrator, pk)
    assert waits == [30, 120, 600, 1800, None]
    assert state(migrator, pk) == ("FAILED", "UNAVAILABLE", 5)


# ---- La cuenta y la credencial, en el mismo scope del reclamo ----


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ("UPDATE channel_accounts SET status = 'DISCONNECTED'", "CHANNEL_UNAVAILABLE"),
        ("UPDATE channel_accounts SET status = 'ERROR'", "CHANNEL_UNAVAILABLE"),
        ("UPDATE channel_accounts SET deleted_at = now()", "CHANNEL_UNAVAILABLE"),
        ("UPDATE credentials SET revoked_at = now(), ciphertext = NULL", "TOKEN_INVALID"),
        ("UPDATE credentials SET owner_id = gen_random_uuid()", "TOKEN_INVALID"),  # de otra cuenta
    ],
)  # fmt: skip
def test_an_account_not_connected_or_a_revoked_credential_closes_the_claim(
    world: Any, migrator: DB, change: str, code: str
) -> None:
    pk = one(world, migrator)
    migrator.execute(change)
    assert claimed(world.a, pk) is None and state(migrator, pk) == ("FAILED", code, 0)


# ---- Reclamos: el perdido, el vencido, el tope por organización, otra organización ----


def test_a_lost_claim_never_settles_and_only_its_own_blob_is_deleted(
    world: Any, migrator: DB, caplog: pytest.LogCaptureFixture
) -> None:
    pk = one(world, migrator)
    first = claimed(world.a, pk)  # A reclama y su descarga se queda colgada
    assert claimed(world.a, pk) is None  # un mensaje repetido de la tarea: el plazo es de A
    due(migrator, pk)
    second = claimed(world.a, pk)  # B reclama el plazo vencido mientras A sigue bajando
    assert finish(world, first, "EXPIRED") == ("LOST", None)  # A no cierra el reclamo de B
    assert store(world, first, b"\xff\xd8\xff de A") == ("LOST", None)  # ni guarda: su blob, fuera
    assert state(migrator, pk) == ("DOWNLOADING", None, 2) and world.blobs.objects == {}
    assert store(world, second) == ("DONE", None) and state(migrator, pk) == ("DONE", None, 2)
    ((theirs, key, *_),) = files(migrator)
    world.blobs.fail_delete = True  # A llega tarde otra vez y borrar su blob falla: se registra
    with caplog.at_level(logging.ERROR):
        assert store(world, first, b"\xff\xd8\xff otro") == ("LOST", None)
    assert "media.blob_delete_failed" in caplog.text
    assert finish(world, first, "RETRY") == ("LOST", None)
    assert state(migrator, pk) == ("DONE", None, 2) and files(migrator)[0][0] == theirs
    assert world.blobs.objects[key][0] == JPEG and theirs == second.attempt_id  # el de B, intacto


def test_a_db_failure_after_the_put_deletes_only_that_attempts_blob(
    world: Any, migrator: DB, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = storage.tenant_key(world.a, "message-media", uuid4())
    world.blobs.objects[other] = (b"de otro", None)
    pk = one(world, migrator)
    real = media._close

    def broken(alias: str, row: UUID, status: str, *args: Any, **kwargs: Any) -> None:
        if status == "DONE":  # tras insertar el archivo, en la misma transacción
            raise DatabaseError("caída")
        real(alias, row, status, *args, **kwargs)

    monkeypatch.setattr(media, "_close", broken)
    with pytest.raises(DatabaseError):
        store(world, claimed(world.a, pk))
    assert list(world.blobs.objects) == [other] and files(migrator) == []
    assert state(migrator, pk) == ("DOWNLOADING", None, 1)  # vuelve al vencer el plazo


def test_a_storage_that_fails_leaves_no_file_row_and_retries(
    world: Any, migrator: DB, caplog: pytest.LogCaptureFixture
) -> None:
    pk = one(world, migrator)
    world.blobs.fail_put = True
    with caplog.at_level(logging.WARNING):
        assert store(world, claimed(world.a, pk)) == ("RETRY", 30)
    assert state(migrator, pk) == ("PENDING", None, 1) and files(migrator) == []
    assert world.blobs.objects == {}  # lo que el almacén dejó a medias, borrado
    logged = [r.msg for r in caplog.records if "media" in str(r.msg)]
    allowed = {"event", "attachment_id", "error", "level", "timestamp", "logger", "request_id",
               "correlation_id"}  # fmt: skip
    assert logged and all(set(m) <= allowed and m["error"] == "OSError" for m in logged), logged  # type: ignore[index]


def test_an_expired_lease_at_the_last_attempt_fails_unavailable(world: Any, migrator: DB) -> None:
    pk = one(world, migrator)
    for _ in range(4):  # cuatro intentos devueltos (los intentos solo suben de uno en uno)
        finish(world, claimed(world.a, pk), "RETRY")
        due(migrator, pk)
    assert isinstance(claimed(world.a, pk), media.Claim)  # el 5.º intento muere sin cerrar
    due(migrator, pk)
    assert claimed(world.a, pk) is None and state(migrator, pk) == ("FAILED", "UNAVAILABLE", 5)


def test_the_per_organization_cap_holds_under_concurrency(world: Any, migrator: DB) -> None:
    for n in range(5):
        receive(world, wamid=f"wamid.C{n}")
    queue = attachments(migrator)
    got = run_at_once(lambda: claimed(world.a, queue.pop()), count=5)
    assert sorted(type(g).__name__ for g in got) == ["Claim"] * 2 + ["int"] * 3, got
    assert {g for g in got if isinstance(g, int)} == {media.BUSY_WAIT}
    busy = "SELECT count(*) FROM message_attachments WHERE download_status = 'DOWNLOADING'"
    assert migrator.execute(busy).fetchone() == (2,)


def test_another_organization_or_an_unknown_id_is_the_same_no_op(world: Any, migrator: DB) -> None:
    pk = one(world, migrator)
    assert claimed(world.b, pk) is None and claimed(world.a, uuid4()) is None
    assert state(migrator, pk) == ("PENDING", None, 0)
    got = claimed(world.a, pk)
    other = media.Claim(pk, got.attempt_id, "IMAGE", MEDIA, PHONE_NUMBER_ID, got.credential_id)
    with tenant_scope(ctx(world.b)) as tenant:  # cerrar desde otra organización: no la ve
        assert media.finish(tenant, other, "EXPIRED") == ("LOST", None)
    assert media.store(TenantContext(world.b, "celery"), other, bytes_of(), "image/jpeg") == (
        "LOST", None)  # fmt: skip
    assert state(migrator, pk) == ("DOWNLOADING", None, 1) and world.blobs.objects == {}


# ---- Lo que la base hace cumplir (SQL crudo como `crm_app` y como el migrador) ----


def raw(org: UUID, sql: str, params: list[Any]) -> None:
    with tenant_scope(ctx(org)), connection.cursor() as cursor:
        cursor.execute(sql, params)


INSERT = (
    "INSERT INTO message_attachments (id, organization_id, message_id, kind, external_media_id, "
    "download_status, failure_code, attempts, file_id, created_at, updated_at) SELECT "
    "gen_random_uuid(), %s, %s, 'IMAGE', '300000000000001', {}, now(), now()"
)
NEW = INSERT.format("'PENDING', NULL, 0, NULL")
CLAIM = "UPDATE message_attachments SET download_status = 'DOWNLOADING', lease_until = now(), "


def a_file(migrator: DB, org: UUID, pk: UUID, purpose: str = "message-media") -> UUID:
    migrator.execute(
        "INSERT INTO files (id, organization_id, storage_key, original_name, mime_type, "
        "size_bytes, sha256, purpose, scan_status) VALUES (%s, %s, %s, 'x', 'image/jpeg', 1, %s, "
        "%s, 'PENDING')",
        [pk, org, storage.tenant_key(org, purpose, pk), "0" * 64, purpose],
    )
    return pk


def refused(org: UUID, rows: list[tuple[str, list[Any], str]]) -> None:
    for sql, params, error in rows:
        with pytest.raises(DatabaseError) as caught:
            raw(org, sql, params)
        assert type(caught.value.__cause__).__name__ == error, sql


def test_the_database_guards_the_table_for_the_app_role(world: Any, migrator: DB) -> None:
    done = one(world, migrator)
    store(world, claimed(world.a, done))
    pending = one(world, migrator, "wamid.M2")
    by_id = "SELECT message_id FROM message_attachments WHERE id = %s"
    (message,) = migrator.execute(by_id, [done]).fetchone()  # type: ignore[misc]
    quote = a_file(migrator, world.a, uuid4(), "quote-pdf")  # otro archivo de la organización
    born = [world.a, message]  # el disparador de INSERT actúa antes que la unicidad
    refused(world.a, [
        ("DELETE FROM message_attachments WHERE id = %s", [done], "InsufficientPrivilege"),
        ("UPDATE message_attachments SET kind = 'AUDIO' WHERE id = %s", [pending],
         "InsufficientPrivilege"),
        ("UPDATE message_attachments SET message_id = gen_random_uuid() WHERE id = %s", [pending],
         "InsufficientPrivilege"),
        ("UPDATE message_attachments SET download_status = 'PENDING', file_id = NULL "
         "WHERE id = %s", [done], "CheckViolation"),
        ("UPDATE message_attachments SET download_status = 'DONE' WHERE id = %s", [pending],
         "CheckViolation"),  # sin pasar por `DOWNLOADING` ni archivo
        ("UPDATE message_attachments SET download_status = 'EXPIRED' WHERE id = %s", [pending],
         "CheckViolation"),
        ("UPDATE message_attachments SET download_status = 'FAILED' WHERE id = %s", [pending],
         "CheckViolation"),  # sin código
        (NEW, born, "UniqueViolation"),  # un adjunto por mensaje
        (INSERT.format("'PENDING', NULL, 5, NULL"), born, "CheckViolation"),  # nace gastado
        (INSERT.format("'EXPIRED', NULL, 0, NULL"), born, "CheckViolation"),  # o cerrado
        (INSERT.format("'DONE', NULL, 0, %s"), [*born, quote], "CheckViolation"),  # con otro
        (f"{CLAIM}attempt_id = gen_random_uuid() WHERE id = %s", [pending], "CheckViolation"),
        (f"{CLAIM}attempt_id = gen_random_uuid(), attempts = 2 WHERE id = %s", [pending],
         "CheckViolation"),  # un reclamo suma exactamente un intento
    ])  # fmt: skip
    got = claimed(world.a, pending)
    refused(world.a, [
        (f"{CLAIM}attempts = 2 WHERE id = %s", [pending], "CheckViolation"),  # el mismo intento
        ("UPDATE message_attachments SET download_status = 'DONE', file_id = %s WHERE id = %s",
         [quote, pending], "CheckViolation"),  # el archivo de otro intento u otro propósito
    ])  # fmt: skip
    a_file(migrator, world.b, got.attempt_id)  # el id del intento, en otra organización: la FK
    with pytest.raises(DatabaseError) as caught:
        raw(world.a, "UPDATE message_attachments SET download_status = 'DONE', file_id = %s "
            "WHERE id = %s", [got.attempt_id, pending])  # fmt: skip
    assert caught.value.__cause__.diag.constraint_name == "message_attachments_file_fk"  # type: ignore[union-attr]
    with pytest.raises(DatabaseError) as caught:  # y el mensaje de otra organización, igual
        raw(world.b, NEW, [world.b, message])
    assert caught.value.__cause__.diag.constraint_name == "message_attachments_message_fk"  # type: ignore[union-attr]
    finish(world, got, "RETRY")  # `PENDING` con el `attempt_id` del intento devuelto
    refused(world.a, [(f"{CLAIM}attempts = 2 WHERE id = %s", [pending], "CheckViolation")])


def test_the_trigger_binds_the_migrator_too(world: Any, migrator: DB) -> None:
    pk = one(world, migrator)
    claimed(world.a, pk)
    for sql in (
        "UPDATE message_attachments SET attempts = 0 WHERE id = %s",
        "UPDATE message_attachments SET external_media_id = '300000000000001' WHERE id = %s",
        "UPDATE message_attachments SET created_at = now() WHERE id = %s",
        "UPDATE message_attachments SET lease_until = now() - interval '1 second' WHERE id = %s",
    ):
        with pytest.raises(psycopg.errors.CheckViolation):
            migrator.execute(sql, [pk])
    fail = "UPDATE message_attachments SET download_status = 'FAILED', failure_code = 'UNAVAILABLE'"
    migrator.execute(f"{fail} WHERE id = %s", [pk])  # DOWNLOADING → FAILED vale; después, nada
    with pytest.raises(psycopg.errors.CheckViolation):
        migrator.execute("UPDATE message_attachments SET not_before = now() WHERE id = %s", [pk])


def test_the_migrations_go_back_and_forward(world: Any, migrator: DB) -> None:
    named = "SELECT count(*) FROM pg_constraint WHERE conname = ANY(%s)"
    names = [["files_org_id_uq", "messages_org_id_uq", "message_attachments_file_fk"]]
    index = (
        "SELECT count(*) FROM pg_class WHERE relname IN ('files_org_id_uq', 'messages_org_id_uq', "
        "'message_attachments_file_idx')"
    )
    clock = "SELECT count(*) FROM pg_proc WHERE prosrc LIKE '%%reclamo antes de tiempo%%'"
    ahead = "SELECT count(*) FROM pg_proc WHERE prosrc LIKE '%%el reloj no se adelanta%%'"
    assert migrator.execute(clock).fetchone() == migrator.execute(ahead).fetchone() == (1,)
    sweep = "SELECT count(*) FROM pg_class WHERE relname = 'message_attachments_sweep_idx'"
    assert migrator.execute(sweep).fetchone() == (1,)
    with as_migrator():
        call_command("migrate", "conversations", "0025", verbosity=0)
    assert (
        migrator.execute(ahead).fetchone() == migrator.execute(sweep).fetchone() == (0,)
    )  # `0026` vuelve al cuerpo exacto de `0022`
    forward = importlib.import_module(
        "apps.conversations.migrations.0026_message_attachments_clock_forward"
    )
    live = "SELECT prosrc FROM pg_proc WHERE proname = 'message_attachments_advance'"
    assert migrator.execute(live).fetchone() == (forward.CLOCK.NEW,)
    with as_migrator():
        call_command("migrate", "conversations", "0021", verbosity=0)
    assert migrator.execute(clock).fetchone() == (0,)  # `0022` vuelve al cuerpo exacto de `0021`
    gone = (
        "SELECT to_regclass('public.message_attachments'), "
        "(SELECT count(*) FROM pg_proc WHERE proname = 'message_attachments_advance')"
    )
    assert migrator.execute(named, names).fetchone() == (3,)
    with as_migrator():
        call_command("migrate", "files", "0001", verbosity=0)
        call_command("migrate", "conversations", "0019", verbosity=0)
    assert migrator.execute(named, names).fetchone() == (0,)
    assert migrator.execute(index).fetchone() == (0,)
    assert migrator.execute(gone).fetchone() == (None, 0)
    with as_migrator():
        call_command("migrate", verbosity=0)
        with connection.cursor() as cursor:  # la conexión que migró, sin `lock_timeout` colgado
            cursor.execute("SHOW lock_timeout")
            assert cursor.fetchone() == ("0",)
    assert migrator.execute(named, names).fetchone() == (3,)
    assert migrator.execute(index).fetchone() == (3,)
    assert migrator.execute(clock).fetchone() == migrator.execute(ahead).fetchone() == (1,)
    assert migrator.execute(sweep).fetchone() == (1,)


# ---- F7-09a1c: la descarga de punta a punta y el webhook repetido a la vez ----


def test_an_image_webhook_ends_done_through_the_task_with_a_fresh_lookup(
    world: Any, migrator: DB
) -> None:
    pk = one(world, migrator)
    assert media.fetch(world.a, pk) == ("DONE", None)
    ((file_id, key, *rest),) = files(migrator)
    assert rest == ["image.jpg", "image/jpeg", len(JPEG), hashlib.sha256(JPEG).hexdigest(),
                    "PENDING", "message-media"]  # fmt: skip
    assert world.blobs.objects[key][0] == JPEG and state(migrator, pk) == ("DONE", None, 1)
    lookup, blob = world.graph.calls
    assert lookup["path"] == f"/{GRAPH_VERSION}/{MEDIA}"
    assert lookup["query"] == {"phone_number_id": [PHONE_NUMBER_ID]}
    assert blob["path"] == f"/blob/{MEDIA}"  # el token solo a Graph y al host de los medios
    sent = [{k.lower(): v for k, v in c["headers"].items()} for c in world.graph.calls]
    assert {h["authorization"] for h in sent} == {f"Bearer {TEST_TOKEN}"}
    assert media.fetch(world.a, pk) == ("SKIPPED", None) and len(world.graph.calls) == 2


def test_a_duplicate_webhook_and_two_tasks_at_once_make_one_attachment_and_one_blob(
    world: Any, migrator: DB
) -> None:
    assert run_at_once(lambda: receive(world)) == [None, None]
    (pk,) = attachments(migrator)
    results = sorted(r[0] for r in run_at_once(lambda: media.fetch(world.a, pk)))
    assert results[0] == "DONE" and results[1] in ("SKIPPED", "WAIT"), results
    assert len(world.blobs.objects) == 1 and len(files(migrator)) == 1


# ---- La tabla de errores: cada fila ----


@pytest.mark.parametrize(
    ("lookup", "blob", "expected", "lookups"),
    [
        (404, 200, ("EXPIRED", None), 1),
        (410, 200, ("EXPIRED", None), 1),
        (401, 200, ("FAILED", "TOKEN_INVALID"), 1),
        (403, 200, ("FAILED", "TOKEN_INVALID"), 1),
        (429, 200, ("PENDING", None), 1),
        (500, 200, ("PENDING", None), 1),
        (400, 200, ("FAILED", "UNAVAILABLE"), 1),  # otro 4xx (un id que Meta no reconoce)
        (200, 403, ("EXPIRED", None), 2),  # la URL fresca: una vez más y luego `EXPIRED`
        (200, 404, ("EXPIRED", None), 2),
        (200, 410, ("EXPIRED", None), 2),
        (200, 401, ("FAILED", "TOKEN_INVALID"), 1),
        (200, 429, ("PENDING", None), 1),
        (200, 503, ("PENDING", None), 1),
        (200, 418, ("FAILED", "UNAVAILABLE"), 1),
    ],
)
def test_each_row_of_the_error_table(
    world: Any, migrator: DB, lookup: int, blob: int, expected: tuple[str, Any], lookups: int
) -> None:
    pk = one(world, migrator)
    world.graph.media[MEDIA] = FakeMedia(JPEG, lookup=lookup, blob=blob)
    assert media.fetch(world.a, pk)[1] == (30 if expected[0] == "PENDING" else None)
    assert state(migrator, pk) == (*expected, 1)
    asked = [c for c in world.graph.calls if c["path"] == f"/{GRAPH_VERSION}/{MEDIA}"]
    assert len(asked) == lookups and files(migrator) == [] and world.blobs.objects == {}


def test_code_190_throttling_codes_and_timeouts(
    world: Any, migrator: DB, monkeypatch: pytest.MonkeyPatch
) -> None:
    pk = one(world, migrator)
    world.graph.token = "otro-token-que-meta-acepta-0000"  # noqa: S105 — 401 con el código 190
    assert media.fetch(world.a, pk) == ("TOKEN_INVALID", None)
    world.graph.token = TEST_TOKEN
    throttled = one(world, migrator, "wamid.M2")

    def code(*args: Any, **kwargs: Any) -> Any:  # 400 con un código de límite de tasa
        body = b'{"error": {"code": 130429}}'
        return urllib3.HTTPResponse(body=io.BytesIO(body), status=400, preload_content=True)

    real = http.request
    monkeypatch.setattr(http, "request", code)
    assert media.fetch(world.a, throttled) == ("RETRY", 30)
    slow = one(world, migrator, "wamid.M3")

    def timeout(*args: Any, **kwargs: Any) -> Any:
        raise urllib3.exceptions.ReadTimeoutError(None, "https://x/?oh=SECRETO", "Read timed out.")  # type: ignore[arg-type]

    monkeypatch.setattr(http, "request", timeout)
    assert media.fetch(world.a, slow) == ("RETRY", 30)
    monkeypatch.setattr(http, "request", real)
    late = one(world, migrator, "wamid.M4")
    monkeypatch.setattr(http, "download", lambda *a, **k: (_ for _ in ()).throw(
        http.DownloadTooSlow()))  # fmt: skip
    assert media.fetch(world.a, late) == ("RETRY", 30)


def test_too_large_off_list_host_encoding_and_retries_until_unavailable(
    world: Any, migrator: DB, monkeypatch: pytest.MonkeyPatch
) -> None:
    pk = one(world, migrator)
    monkeypatch.setitem(media.CAPS, "IMAGE", len(JPEG) - 1)
    assert media.fetch(world.a, pk) == ("TOO_LARGE", None)
    off = one(world, migrator, "wamid.M2")
    world.graph.calls.clear()
    world.graph.media[MEDIA] = FakeMedia(JPEG, host="evil.example.com")  # nunca se le pide nada
    assert media.fetch(world.a, off) == ("UNAVAILABLE", None)
    assert not any(c["path"].startswith("/blob/") for c in world.graph.calls)
    packed = one(world, migrator, "wamid.M3")
    world.graph.media[MEDIA] = FakeMedia(JPEG, headers={"Content-Encoding": "gzip"})
    assert media.fetch(world.a, packed) == ("UNAVAILABLE", None)
    tired = one(world, migrator, "wamid.M4")
    world.graph.media[MEDIA] = FakeMedia(JPEG, lookup=500)
    waits = []
    for _ in range(media.MAX_ATTEMPTS):
        waits.append(media.fetch(world.a, tired)[1])
        if waits[-1]:
            due(migrator, tired)
    assert waits == [30, 120, 600, 1800, None]
    assert state(migrator, tired) == ("FAILED", "UNAVAILABLE", 5) and world.blobs.objects == {}


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ("UPDATE channel_accounts SET status = 'DISCONNECTED'", "CHANNEL_UNAVAILABLE"),
        ("UPDATE credentials SET revoked_at = now(), ciphertext = NULL", "TOKEN_INVALID"),
    ],
)
def test_an_account_not_connected_or_a_revoked_credential_never_calls_meta(
    world: Any, migrator: DB, change: str, code: str
) -> None:
    pk = one(world, migrator)
    migrator.execute(change)
    assert media.fetch(world.a, pk) == ("SKIPPED", None)
    assert state(migrator, pk) == ("FAILED", code, 0) and world.graph.calls == []


def test_a_credential_revoked_after_the_claim_fails_with_token_invalid(
    world: Any, migrator: DB, monkeypatch: pytest.MonkeyPatch
) -> None:
    pk = one(world, migrator)
    real = media.claim

    def then_revoke(tenant: TenantContext, attachment_id: UUID) -> Any:
        got = real(tenant, attachment_id)
        migrator.execute("UPDATE credentials SET revoked_at = now(), ciphertext = NULL")
        return got

    monkeypatch.setattr(media, "claim", then_revoke)
    assert media.fetch(world.a, pk) == ("TOKEN_INVALID", None) and world.graph.calls == []
    assert state(migrator, pk) == ("FAILED", "TOKEN_INVALID", 1)


# ---- El tipo real por magic bytes ----


@pytest.mark.parametrize(
    ("content", "found"),
    [
        (JPEG, ("IMAGE", "image/jpeg")),
        (b"\x89PNG\r\n\x1a\n" + bytes(8), ("IMAGE", "image/png")),
        (b"RIFF\x00\x01\x00\x00WEBPVP8 ", ("STICKER", "image/webp")),
        (b"RIFF\x00\x01\x00\x00AVI LIST", None),  # RIFF sin WEBP en el desplazamiento 8
        (b"OggS" + bytes(12), ("AUDIO", "audio/ogg")),
        (b"#!AMR\n" + bytes(10), ("AUDIO", "audio/amr")),
        (bytes([0xFF, 0xF1, 0x50, 0x80]) + bytes(12), ("AUDIO", "audio/aac")),
        (MP3, ("AUDIO", "audio/mpeg")),
        (ID3 + MP3, ("AUDIO", "audio/mpeg")),
        (FTYP + b"M4A " + bytes(12), ("AUDIO", "audio/mp4")),
        (FTYP + b"isom" + bytes(12), ("VIDEO", "video/mp4")),
        (FTYP + b"3gp5" + bytes(12), ("VIDEO", "video/3gpp")),
        (FTYP + b"qt  " + bytes(12), None),  # una marca fuera de la lista
        ((8).to_bytes(4, "big") + b"ftypisom" + bytes(4), None),  # caja `ftyp` imposible
        (ID3 + HTML, None),  # el políglota: una etiqueta ID3 y otra cosa detrás
        (ID3 + bytes([0xFF, 0xF1, 0x50, 0x80]), None),  # ADTS tras ID3: no
        (b"ID3\x04\x00\x00\x80\x00\x00\x00" + MP3, None),  # tamaño que no es syncsafe
        (bytes([0xFF, 0xFB, 0xF0, 0x64]), None),  # tasa de bits reservada
        (bytes([0xFF, 0xFB, 0x0C, 0x64]), None),  # tasa de bits libre y muestreo reservado
        (bytes([0xFF, 0xEB, 0x90, 0x64]), None),  # versión MPEG reservada
        (b"%PDF-1.7\n/JS (app.alert(1))", None),
        (b"GIF89a" + bytes(10), None),
        (HTML, None),
        (b"<svg xmlns='http://www.w3.org/2000/svg' onload='alert(1)'/>", None),
        (b"", None),
    ],
)
def test_the_type_comes_from_the_magic_bytes(content: bytes, found: Any) -> None:
    assert media.sniff(io.BytesIO(content)) == found


@pytest.mark.parametrize(
    ("kind", "content", "code"),
    [
        ("image", b"%PDF-1.7\n/JS (app.alert(1))", "TYPE_NOT_ALLOWED"),
        ("image", FTYP + b"mp42" + bytes(12), "TYPE_MISMATCH"),
        ("image", b"GIF89a" + bytes(10), "TYPE_NOT_ALLOWED"),
        ("image", b"<svg onload='alert(1)'/>", "TYPE_NOT_ALLOWED"),
        ("audio", ID3 + HTML, "TYPE_NOT_ALLOWED"),
        ("video", FTYP + b"M4A " + bytes(12), "TYPE_MISMATCH"),
        ("sticker", JPEG, "TYPE_MISMATCH"),
    ],
)
def test_a_type_outside_its_kind_fails_and_writes_no_blob(
    world: Any, migrator: DB, kind: str, content: bytes, code: str
) -> None:
    pk = one(world, migrator, content=content, kind=kind)
    assert media.fetch(world.a, pk) == (code, None)
    assert state(migrator, pk) == ("FAILED", code, 1)
    assert world.blobs.objects == {} and files(migrator) == []


# ---- La tarea, el handler y la cola; los registros ----


def test_another_organization_or_an_unknown_id_is_the_same_no_op_also_in_the_log(
    world: Any, migrator: DB, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    pk = one(world, migrator)
    queued: list[Any] = []
    monkeypatch.setattr(tasks.fetch_media, "apply_async", lambda **kw: queued.append(kw))
    with caplog.at_level(logging.INFO):
        tasks.fetch_media(str(pk), str(world.b))
        tasks.fetch_media(str(uuid4()), str(world.a))
    assert state(migrator, pk) == ("PENDING", None, 0) and world.graph.calls == [] and not queued
    noise = ("attachment_id", "timestamp")
    logged = [r.msg for r in caplog.records if "media" in str(r.msg)]
    same = [{k: v for k, v in m.items() if k not in noise} for m in logged]  # type: ignore[union-attr]
    assert len(same) == 2 and same[0] == same[1] and same[0]["outcome"] == "SKIPPED"


def test_the_handler_queues_the_task_on_the_media_queue_and_the_task_comes_back(
    world: Any, migrator: DB, monkeypatch: pytest.MonkeyPatch
) -> None:
    pk = one(world, migrator)
    queued: list[Any] = []
    monkeypatch.setattr(tasks.fetch_media, "apply_async", lambda **kw: queued.append(kw))
    (event,) = migrator.execute(
        "SELECT id FROM outbox_events WHERE event_type = 'message_attachment.pending'"
    ).fetchone()  # type: ignore[misc]
    assert tasks.media_event in handlers_for("message_attachment.pending")
    theirs = {"event_id": str(event), "organization_id": str(world.b)}  # el evento es de A
    assert tasks.media_event.apply(kwargs=theirs).successful() and queued == []
    ours = {"event_id": str(event), "organization_id": str(world.a)}
    assert tasks.media_event.apply(kwargs=ours).successful()
    assert queued == [{"kwargs": {"attachment_id": str(pk), "organization_id": str(world.a)}}]
    assert app.amqp.router.route({}, "conversations.fetch_media")["queue"].name == "media"
    world.graph.media[MEDIA] = FakeMedia(JPEG, lookup=500)
    tasks.fetch_media(str(pk), str(world.a))  # reintento: vuelve tras su espera
    monkeypatch.setattr(media, "fetch", lambda *a: (_ for _ in ()).throw(KeyError("x")))
    tasks.fetch_media(str(pk), str(world.a))  # fallo inesperado: vuelve al vencer el plazo
    assert [q.get("countdown") for q in queued[1:]] == [30, 301]


def test_logs_carry_ids_and_codes_only(
    world: Any, migrator: DB, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tasks.fetch_media, "apply_async", lambda **kw: None)
    pk = one(world, migrator)
    with caplog.at_level(logging.DEBUG):
        tasks.fetch_media(str(pk), str(world.a))
        world.graph.media[MEDIA] = FakeMedia(JPEG, blob=404)
        tasks.fetch_media(str(one(world, migrator, "wamid.M2")), str(world.a))
        world.blobs.fail_put = True
        tasks.fetch_media(str(one(world, migrator, "wamid.M3")), str(world.a))
    text = caplog.text + " ".join(str(r.__dict__) for r in caplog.records)
    for secret in (TEST_TOKEN, "lookaside", "/blob/", "evil.html", "text/html", CUSTOMER, "Bearer"):
        assert secret not in text, secret


# ---- Lo que `store` y `finish` no aceptan; el reloj de la base en el reclamo (`0022`) ----


def test_store_refuses_a_callers_transaction_and_finish_an_unknown_outcome(
    world: Any, migrator: DB
) -> None:
    pk = one(world, migrator)
    got = claimed(world.a, pk)
    with tenant_scope(ctx(world.a)) as tenant:
        with pytest.raises(RuntimeError):  # su blob no se desharía con este rollback
            media.store(tenant, got, bytes_of(), "image/jpeg")
        with pytest.raises(ValueError, match="desconocido"):
            media.finish(tenant, got, "DONE")
    with transaction.atomic(), pytest.raises(RuntimeError):
        store(world, got)
    assert world.blobs.objects == {} and state(migrator, pk) == ("DOWNLOADING", None, 1)


def test_the_database_ties_a_claim_to_its_wait_and_to_the_lease_by_its_own_clock(
    world: Any, migrator: DB
) -> None:
    pk = one(world, migrator)
    finish(world, claimed(world.a, pk), "RETRY")  # `PENDING` con una espera de 30 s
    take = f"{CLAIM}attempt_id = gen_random_uuid(), attempts = attempts + 1 WHERE id = %s"
    refused(world.a, [(take, [pk], "CheckViolation")])  # antes de la espera
    clock(migrator, "UPDATE message_attachments SET not_before = now() + interval '1 second' "
          "WHERE id = %s", [pk])  # fmt: skip
    refused(world.a, [(take, [pk], "CheckViolation")])  # un segundo antes, tampoco
    clock(migrator, "UPDATE message_attachments SET not_before = now() WHERE id = %s", [pk])
    raw(
        world.a, take.replace("lease_until = now()", "lease_until = now() + interval '5 min'"), [pk]
    )  # fmt: skip — justo en la espera, sí
    refused(world.a, [(take, [pk], "CheckViolation")])  # sobre el plazo vivo de otro intento
    clock(migrator, "UPDATE message_attachments SET lease_until = now() + interval '1 second' "
          "WHERE id = %s", [pk])  # fmt: skip
    refused(world.a, [(take, [pk], "CheckViolation")])
    clock(migrator, "UPDATE message_attachments SET lease_until = now() WHERE id = %s", [pk])
    raw(world.a, take, [pk])  # vencido justo ahora: otro intento lo reclama
    assert state(migrator, pk) == ("DOWNLOADING", None, 3)


def test_0022_refuses_to_replace_a_function_it_does_not_expect(world: Any) -> None:
    clock = importlib.import_module(
        "apps.conversations.migrations.0022_message_attachments_claim_clock"
    )
    with as_migrator(), connection.schema_editor() as editor:
        with pytest.raises(RuntimeError, match="versión esperada"):  # ya está la de `0022`
            clock._swap(clock.OLD, clock.NEW)(None, editor)


# ---- Revisión de #717: la tarea temprana, el DNS que falla, el reloj de la sentencia ----


def test_a_task_that_runs_before_its_wait_comes_back_instead_of_dropping_the_attachment(
    world: Any, migrator: DB, monkeypatch: pytest.MonkeyPatch
) -> None:
    pk = one(world, migrator)
    world.graph.media[MEDIA] = FakeMedia(JPEG, lookup=500)
    queued: list[Any] = []
    monkeypatch.setattr(tasks.fetch_media, "apply_async", lambda **kw: queued.append(kw))
    tasks.fetch_media(str(pk), str(world.a))  # `PENDING` con 30 s de espera (reloj de la base)
    tasks.fetch_media(str(pk), str(world.a))  # el reintento llega antes (reloj del worker)
    assert [q["countdown"] for q in queued][0] == 30 and 28 <= queued[1]["countdown"] <= 31
    assert state(migrator, pk) == ("PENDING", None, 1) and len(world.graph.calls) == 1


def test_a_dns_failure_on_the_media_host_retries_and_an_off_list_host_does_not(
    world: Any, migrator: DB, monkeypatch: pytest.MonkeyPatch
) -> None:
    pk = one(world, migrator)

    def no_dns(*args: Any, **kwargs: Any) -> Any:
        raise socket.gaierror(socket.EAI_AGAIN, "temporary failure")

    def resolving(url: str, *args: Any, **kwargs: Any) -> int:  # lo que hace la conexión real
        http.resolve_public(urllib3.util.parse_url(url).host or "", 443)
        return 200

    monkeypatch.setattr(socket, "getaddrinfo", no_dns)
    monkeypatch.setattr(http, "download", resolving)
    assert media.fetch(world.a, pk) == ("RETRY", 30)
    assert state(migrator, pk) == ("PENDING", None, 1)


def test_throttling_codes_win_over_403_and_a_revoked_credential_without_its_key_fails(
    world: Any, migrator: DB, monkeypatch: pytest.MonkeyPatch
) -> None:
    pk = one(world, migrator)

    def limited(*args: Any, **kwargs: Any) -> Any:  # como `graph.py`: el límite, antes que el token
        body = b'{"error": {"code": 4}}'
        return urllib3.HTTPResponse(body=io.BytesIO(body), status=403, preload_content=True)

    real = http.request
    monkeypatch.setattr(http, "request", limited)
    assert media.fetch(world.a, pk) == ("RETRY", 30)
    monkeypatch.setattr(http, "request", real)
    other = one(world, migrator, "wamid.M2")
    taken = media.claim

    def then_revoke(tenant: TenantContext, attachment_id: UUID) -> Any:
        got = taken(tenant, attachment_id)  # revocada y con una versión de clave que no hay aquí
        migrator.execute(
            "UPDATE credentials SET revoked_at = now(), ciphertext = NULL, key_version = 999"
        )
        return got

    monkeypatch.setattr(media, "claim", then_revoke)
    assert media.fetch(world.a, other) == ("TOKEN_INVALID", None) and len(world.graph.calls) == 0


def test_a_claim_sees_what_fell_due_while_its_transaction_waited(world: Any, migrator: DB) -> None:
    pk = one(world, migrator)
    finish(world, claimed(world.a, pk), "RETRY")
    with tenant_scope(ctx(world.a)) as tenant:  # la transacción ya empezó (`now()` fijo)
        with connection.cursor() as cursor:
            cursor.execute("SELECT now()")
            (began,) = cursor.fetchone()
        # vence después del inicio de la transacción y antes de la sentencia del reclamo, sin dormir
        clock(migrator, "UPDATE message_attachments SET not_before = clock_timestamp() "
              "WHERE id = %s", [pk])  # fmt: skip
        (fell_due,) = migrator.execute(
            "SELECT not_before FROM message_attachments WHERE id = %s", [pk]
        ).fetchone()  # type: ignore[misc]
        assert fell_due > began
        assert isinstance(media.claim(tenant, pk), media.Claim)
    assert state(migrator, pk) == ("DOWNLOADING", None, 2)


# ---- F7-09a1d: el reloj no se adelanta (`0026`), la IP no pública, el barredor, los topes ----


def test_outside_a_claim_the_database_never_moves_the_clock_forward(
    world: Any, migrator: DB
) -> None:
    pk = one(world, migrator)
    claimed(world.a, pk)  # `DOWNLOADING` con 300 s de plazo vivo
    update = "UPDATE message_attachments SET {} WHERE id = %s"
    refused(world.a, [  # adelantar el plazo y reclamar después burlaría `0022`
        (update.format("lease_until = now()"), [pk], "CheckViolation"),
        (update.format("lease_until = lease_until - interval '1 microsecond'"), [pk],
         "CheckViolation"),
        (update.format("lease_until = NULL"), [pk], "CheckViolation"),
        (update.format("download_status = 'PENDING', not_before = NULL"), [pk],
         "CheckViolation"),  # un reintento sin espera: el plazo libre al instante
        (update.format("download_status = 'PENDING', not_before = statement_timestamp() - "
                       "interval '1 second'"), [pk], "CheckViolation"),
    ])  # fmt: skip
    raw(world.a, update.format("lease_until = lease_until + interval '1 second'"), [pk])  # alargar
    raw(
        world.a,
        update.format("download_status = 'PENDING', not_before = statement_timestamp()"),
        [pk],
    )  # fmt: skip — un reintento desde esta sentencia, sí
    refused(world.a, [
        (update.format("not_before = NULL"), [pk], "CheckViolation"),
        (update.format("not_before = not_before - interval '1 microsecond'"), [pk],
         "CheckViolation"),
        (update.format("download_status = 'FAILED', failure_code = 'UNAVAILABLE', "
                       "not_before = NULL"), [pk], "CheckViolation"),
    ])  # fmt: skip
    raw(world.a, update.format("not_before = not_before + interval '1 second'"), [pk])  # más tarde
    assert state(migrator, pk) == ("PENDING", None, 1)


OFF = r"session_replication_role|(ENABLE|DISABLE)\s+(ALWAYS\s+|REPLICA\s+)?TRIGGER"
# Solo la vuelta atrás de las migraciones que los crearon; una corrección de datos futura que
# necesite otra cosa es una migración revisada que se añade aquí.
DROPS = {
    "apps/conversations/migrations/0005_whatsapp_account.py",
    "apps/conversations/migrations/0021_message_attachments.py",
    "apps/pricing/migrations/0004_promotions_frozen.py",
    "apps/quotes/migrations/0002_approvals_and_discount_policies.py",
}


def test_no_production_code_turns_a_trigger_off() -> None:
    root = pathlib.Path(__file__).resolve().parents[1]
    sources = {
        str(path.relative_to(root)): path.read_text()
        for folder in ("apps", "core", "config")
        for path in (root / folder).rglob("*.py")
    }
    assert [name for name, text in sources.items() if re.search(OFF, text, re.IGNORECASE)] == []
    dropped = {name for name, text in sources.items() if re.search(r"DROP\s+TRIGGER", text, re.I)}
    assert dropped == DROPS


def test_resolve_public_tells_a_non_public_answer_from_a_dns_outage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def answers(*ips: str) -> Any:
        return lambda *a, **k: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443)) for ip in ips
        ]

    for ips in (("10.0.0.1",), ("157.240.1.1", "10.0.0.1"), ("127.0.0.1",)):
        monkeypatch.setattr(socket, "getaddrinfo", answers(*ips))
        with pytest.raises(http.NonPublicAddress):
            http.resolve_public(MEDIA_HOST, 443)

    def no_dns(*args: Any, **kwargs: Any) -> Any:
        raise socket.gaierror(socket.EAI_AGAIN, "temporary failure")

    monkeypatch.setattr(socket, "getaddrinfo", no_dns)
    with pytest.raises(http.BlockedDestination) as caught:
        http.resolve_public(MEDIA_HOST, 443)
    assert not isinstance(caught.value, http.NonPublicAddress)


@pytest.mark.parametrize(
    ("host", "nth", "blob", "expected"),
    [
        (MEDIA_HOST, 1, 200, ("FAILED", "UNAVAILABLE", 1)),  # la URL de Meta: definitivo
        (transport.GRAPH_HOST, 1, 200, ("PENDING", None, 1)),  # el DNS del despliegue: reintento
        (transport.GRAPH_HOST, 2, 404, ("PENDING", None, 1)),  # también en la segunda consulta
    ],
)
def test_a_non_public_dns_answer_never_connects_nor_sends_the_token(
    world: Any, migrator: DB, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    host: str, nth: int, blob: int, expected: tuple[Any, ...],
) -> None:  # fmt: skip
    pk = one(world, migrator)
    world.graph.media[MEDIA] = FakeMedia(JPEG, blob=blob)
    guarded = urllib3.PoolManager(retries=False)  # la red real de `core.http` para `host`
    guarded.pool_classes_by_scheme = {"https": http._GuardedPool}
    sim, seen = http._manager, [0]

    class Split:
        def request(self, method: str, url: str, **kwargs: Any) -> Any:
            if urllib3.util.parse_url(url).host == host:
                seen[0] += 1
                if seen[0] >= nth:
                    return guarded.request(method, url, **kwargs)
            return sim.request(method, url, **kwargs)

    opened: list[Any] = []

    def connect(*args: Any, **kwargs: Any) -> Any:
        opened.append(args)
        raise AssertionError("conectó")

    rebound = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 443))]
    monkeypatch.setattr(http, "_manager", Split())
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: rebound)
    monkeypatch.setattr(urllib3.util.connection, "create_connection", connect)
    with caplog.at_level(logging.INFO):
        result = media.fetch(world.a, pk)
    assert result == ((RETRY, 30) if expected[0] == "PENDING" else ("UNAVAILABLE", None))
    assert state(migrator, pk) == expected and opened == []
    lookups = [c for c in world.graph.calls if not c["path"].startswith("/blob/")]
    assert len(lookups) == (1 if host == MEDIA_HOST else nth - 1)  # la bloqueada no salió
    assert sum(c["path"].startswith("/blob/") for c in world.graph.calls) == (blob == 404)
    blocked = [r.msg for r in caplog.records if "media.blocked_ip" in str(r.msg)]
    assert len(blocked) == 1 and set(blocked[0]) <= LOGGED | {"attachment_id", "role"}
    assert blocked[0]["role"] == ("media" if host == MEDIA_HOST else "graph")  # type: ignore[index]
    text = caplog.text + " ".join(str(r.__dict__) for r in caplog.records)
    for secret in ("10.0.0.1", MEDIA_HOST, transport.GRAPH_HOST, TEST_TOKEN, "lookaside"):
        assert secret not in text, secret


def test_no_task_waits_longer_than_the_broker_takes_to_redeliver_it() -> None:
    visibility = app.conf.broker_transport_options["visibility_timeout"]
    ours = (*media.BACKOFF, int(media.LEASE.total_seconds()) + 1, media.BUSY_WAIT, media.EARLY + 1)
    assert max(ours) == media.MAX_COUNTDOWN  # cada `countdown` de medios
    delays = (*ours, tasks.BACKOFF_MAX, tasks.REQUEUE_WAIT + 1, 2**tasks.REALTIME_RETRIES)
    assert max(delays) * 1.5 <= visibility == 3600  # y los del resto del repositorio


def test_unexpected_failures_come_back_a_bounded_number_of_times_and_waits_are_capped(
    world: Any, migrator: DB, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    pk = one(world, migrator)
    queued: list[Any] = []
    monkeypatch.setattr(tasks.fetch_media, "apply_async", lambda **kw: queued.append(kw))
    monkeypatch.setattr(media, "fetch", lambda *a: (_ for _ in ()).throw(KeyError("x")))
    kwargs: dict[str, Any] = {"attachment_id": str(pk), "organization_id": str(world.a)}
    runs = 0
    with caplog.at_level(logging.INFO):
        while True:
            tasks.fetch_media(**kwargs)
            runs += 1
            if not queued:
                break
            again = queued.pop()
            assert again["countdown"] == 301 and runs < 10
            kwargs = again["kwargs"]
    assert runs == tasks.MEDIA_GIVE_UP and "media.fetch_gave_up" in caplog.text
    monkeypatch.setattr(media, "fetch", lambda *a: ("WAIT", 30))
    tasks.fetch_media(**kwargs)  # un intento que no falla pone la cuenta a cero
    assert queued == [{"kwargs": {"attachment_id": str(pk), "organization_id": str(world.a)},
                       "countdown": 30}]  # fmt: skip


def test_a_download_without_time_left_on_its_lease_retries_instead_of_storing(
    world: Any, migrator: DB, monkeypatch: pytest.MonkeyPatch
) -> None:
    late, ok = one(world, migrator), one(world, migrator, "wamid.M2")
    real, sniffed = time.monotonic, media.sniff
    spent = [0.0]
    monkeypatch.setattr(media, "time", SimpleNamespace(monotonic=lambda: real() + spent[0]))

    def slow(blob: Any) -> Any:  # la descarga se comió el plazo
        spent[0] = media.LEASE.total_seconds() - media.STORE_RESERVE + 1
        return sniffed(blob)

    monkeypatch.setattr(media, "sniff", slow)
    assert media.fetch(world.a, late) == ("RETRY", 30)
    assert state(migrator, late) == ("PENDING", None, 1)
    assert world.blobs.objects == {} and files(migrator) == []
    spent[0] = 0.0
    spent_200 = lambda blob: (spent.__setitem__(0, 200.0), sniffed(blob))[1]  # noqa: E731
    monkeypatch.setattr(media, "sniff", spent_200)
    assert media.fetch(world.a, ok) == ("DONE", None)  # con un minuto y algo, aún guarda


AGED = (
    "UPDATE message_attachments SET created_at = now() - %(ago)s, updated_at = now() - %(ago)s "
    "WHERE id = ANY(%(ids)s)"
)
STRAND = AGED.replace(" WHERE", ", lease_until = CASE WHEN download_status = 'DOWNLOADING' "
                      "THEN now() - %(ago)s END WHERE")  # fmt: skip


ELAPSE = (
    "UPDATE message_attachments SET not_before = not_before - %(ago)s, "
    "lease_until = lease_until - %(ago)s, updated_at = updated_at - %(ago)s WHERE id = ANY(%(ids)s)"
)


def strand(migrator: DB, pk: UUID, minutes: int = 10) -> None:  # su tarea se perdió hace tanto
    clock(migrator, STRAND, {"ago": dt.timedelta(minutes=minutes), "ids": [pk]})


def test_the_sweeper_requeues_stranded_attachments_once_per_window_and_only_those(
    world: Any, migrator: DB, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    queued: list[Any] = []
    monkeypatch.setattr(tasks.fetch_media, "apply_async", lambda **kw: queued.append(kw))
    fresh = one(world, migrator)  # su tarea va en camino
    lost = one(world, migrator, "wamid.M2")  # `PENDING` cuya tarea se perdió
    waiting = one(world, migrator, "wamid.M5")  # `PENDING` que espera su reintento
    finish(world, claimed(world.a, waiting), "RETRY")
    dead = one(world, migrator, "wamid.M3")  # `DOWNLOADING` cuyo worker murió
    claimed(world.a, dead)
    live = one(world, migrator, "wamid.M4")  # `DOWNLOADING` vivo
    claimed(world.a, live)
    for pk in (lost, dead):
        strand(migrator, pk)
    clock(migrator, AGED, {"ago": dt.timedelta(minutes=10), "ids": [live, waiting]})
    near = one(world, migrator, "wamid.M6")  # vencido, pero no hace `SWEEP_AFTER`
    strand(migrator, near, 4)
    with caplog.at_level(logging.INFO):
        done = tasks.sweep_media()
    assert (done["requeued"], done["failed"]) == (2, 0) and done["organizations"] >= 2
    assert sorted(q["kwargs"]["attachment_id"] for q in queued) == sorted([str(lost), str(dead)])
    assert {q["kwargs"]["organization_id"] for q in queued} == {str(world.a)}
    assert all(q["countdown"] == media.BUSY_WAIT for q in queued)
    logged = [r.msg for r in caplog.records if "media.swept" in str(r.msg)]
    counters = {"organizations", "requeued", "failed"}
    assert logged and set(logged[0]) <= LOGGED | counters
    assert tasks.sweep_media()["requeued"] == 0  # marcados: no otra vez antes de `SWEEP_AFTER`
    for pk in (lost, dead):
        strand(migrator, pk)
    assert tasks.sweep_media(batch_size=1)["requeued"] == 1  # el tope por pasada
    queued.clear()
    assert media.fetch(world.a, lost) == media.fetch(world.a, dead) == ("DONE", None)
    assert state(migrator, fresh) == ("PENDING", None, 0)  # nada de lo que se atiende cambió
    assert state(migrator, live) == ("DOWNLOADING", None, 1)


def test_the_sweeper_skips_inactive_or_failing_organizations_and_locked_rows(
    world: Any, migrator: DB, monkeypatch: pytest.MonkeyPatch
) -> None:
    queued: list[Any] = []
    monkeypatch.setattr(tasks.fetch_media, "apply_async", lambda **kw: queued.append(kw))
    pk = one(world, migrator)
    strand(migrator, pk)
    migrator.execute("UPDATE organizations SET status = 'SUSPENDED' WHERE id = %s", [world.a])
    assert tasks.sweep_media()["requeued"] == 0 and queued == []
    migrator.execute("UPDATE organizations SET status = 'ACTIVE' WHERE id = %s", [world.a])
    real = media.stranded

    def broken(tenant: TenantContext, limit: int) -> list[UUID]:
        if tenant.organization_id == world.b:
            raise DatabaseError("caída")
        return real(tenant, limit)

    monkeypatch.setattr(media, "stranded", broken)
    done = tasks.sweep_media()  # B falla y A sigue
    assert (done["requeued"], done["failed"]) == (1, 1) and len(queued) == 1
    strand(migrator, pk)
    swept: list[Any] = []
    with migrator.transaction():  # un reclamo tiene la fila: el barredor no la espera
        migrator.execute("SELECT 1 FROM message_attachments WHERE id = %s FOR UPDATE", [pk])

        def sweep() -> None:
            swept.append(tasks.sweep_media())
            connection.close()

        worker = threading.Thread(target=sweep)
        worker.start()
        worker.join(timeout=10)
        stuck = worker.is_alive()
    worker.join(timeout=30)
    assert not stuck and swept[0]["requeued"] == 0


def test_a_row_waiting_behind_the_cap_is_not_swept_and_duplicate_tasks_end(
    world: Any, migrator: DB, monkeypatch: pytest.MonkeyPatch
) -> None:
    queued: list[Any] = []
    monkeypatch.setattr(tasks.fetch_media, "apply_async", lambda **kw: queued.append(kw))
    first, second, third = (one(world, migrator, f"wamid.B{n}") for n in range(3))
    held = claimed(world.a, first)
    claimed(world.a, second)  # el tope de la organización (2), lleno
    strand(migrator, third)  # su alta es vieja: parecería abandonada...
    assert claimed(world.a, third) == media.BUSY_WAIT  # ...pero su tarea vuelve y la marca
    assert claimed(world.a, third) is None  # otra tarea la marcó hace nada: esta es un duplicado
    clock(migrator, "UPDATE message_attachments SET updated_at = updated_at - interval '20 s' "
          "WHERE id = %s", [third])  # fmt: skip
    assert claimed(world.a, third) == media.BUSY_WAIT  # pasado medio `BUSY_WAIT`, la que vuelve
    fresh = one(world, migrator, "wamid.B9")  # recién llegado: su alta no cuenta como marca
    assert claimed(world.a, fresh) == media.BUSY_WAIT
    assert tasks.sweep_media()["requeued"] == 0 and queued == []
    for _ in range(3):  # pasan las ventanas: mientras su tarea siga, ninguna tarea más
        strand(migrator, third)
        tasks.fetch_media(str(third), str(world.a))
        assert tasks.sweep_media()["requeued"] == 0
    assert [q["countdown"] for q in queued] == [media.BUSY_WAIT] * 3
    queued.clear()
    tasks.fetch_media(str(first), str(world.a))  # un duplicado con el plazo vivo de otro: acaba
    assert queued == [] and state(migrator, first) == ("DOWNLOADING", None, 1)
    finish(world, held, "RETRY")  # espera de 30 s: la tarea que llega algo antes vuelve
    assert isinstance(claimed(world.a, first), int)
    later = "UPDATE message_attachments SET not_before = now() + interval '10 min' WHERE id = %s"
    raw(world.a, later, [first])  # una espera larga: quien llega tanto antes es un duplicado
    assert claimed(world.a, first) is None


@pytest.mark.parametrize(
    ("failures", "requeued"), [(-5, 1), (1, 2), (10**9, None), ("x", None), (None, None)]
)
def test_the_failure_count_from_the_message_is_clamped(
    world: Any, migrator: DB, monkeypatch: pytest.MonkeyPatch, failures: Any, requeued: Any
) -> None:
    pk = one(world, migrator)
    queued: list[Any] = []
    monkeypatch.setattr(tasks.fetch_media, "apply_async", lambda **kw: queued.append(kw))
    monkeypatch.setattr(media, "fetch", lambda *a: (_ for _ in ()).throw(KeyError("x")))
    tasks.fetch_media(str(pk), str(world.a), failures=failures)
    assert [q["kwargs"]["failures"] for q in queued] == ([] if requeued is None else [requeued])


def test_the_same_task_carries_an_attachment_through_every_wait_until_it_gives_up(
    world: Any, migrator: DB, monkeypatch: pytest.MonkeyPatch
) -> None:
    pk = one(world, migrator)
    world.graph.media[MEDIA] = FakeMedia(JPEG, lookup=500)
    queued: list[Any] = []
    monkeypatch.setattr(tasks.fetch_media, "apply_async", lambda **kw: queued.append(kw))
    kwargs: dict[str, Any] = {"attachment_id": str(pk), "organization_id": str(world.a)}
    countdowns = []
    while True:
        tasks.fetch_media(**kwargs)
        if not queued:
            break
        (again,) = queued
        queued.clear()
        countdowns.append(again["countdown"])
        # el reloj avanza lo que espera la tarea, ni más (sin `due`, que deja la fila vencida)
        clock(migrator, ELAPSE, {"ago": dt.timedelta(seconds=again["countdown"]), "ids": [pk]})
        kwargs = again["kwargs"]
    assert countdowns == list(media.BACKOFF)  # la misma tarea vuelve a su hora en cada espera
    assert state(migrator, pk) == ("FAILED", "UNAVAILABLE", media.MAX_ATTEMPTS)
    assert len(world.graph.calls) == media.MAX_ATTEMPTS


def test_the_sweepers_task_for_a_row_behind_the_cap_waits_its_turn_and_then_downloads(
    world: Any, migrator: DB, monkeypatch: pytest.MonkeyPatch
) -> None:
    queued: list[Any] = []
    monkeypatch.setattr(tasks.fetch_media, "apply_async", lambda **kw: queued.append(kw))
    first, second, third = (one(world, migrator, f"wamid.S{n}") for n in range(3))
    held = claimed(world.a, first)
    claimed(world.a, second)  # el tope lleno
    strand(migrator, third)
    assert tasks.sweep_media()["requeued"] == 1
    (rescue,) = queued
    assert rescue["countdown"] == media.BUSY_WAIT  # llega después de la marca del barredor
    queued.clear()
    elapse = {"ago": dt.timedelta(seconds=media.BUSY_WAIT), "ids": [third]}
    clock(migrator, ELAPSE, elapse)
    tasks.fetch_media(**rescue["kwargs"])  # con el tope aún lleno: sigue, no es un duplicado
    (again,) = queued
    assert again["countdown"] == media.BUSY_WAIT
    queued.clear()
    finish(world, held, "EXPIRED")  # el tope se libera
    clock(migrator, ELAPSE, elapse)
    tasks.fetch_media(**again["kwargs"])
    assert queued == [] and state(migrator, third) == ("DONE", None, 1)
