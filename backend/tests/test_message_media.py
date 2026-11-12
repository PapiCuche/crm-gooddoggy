"""F7-09a1b: el adjunto de un medio entrante y su máquina de estados (`apps.conversations.media`,
migraciones `0020`, `0021` y `files.0002`): el reclamo, el cierre y el blob por intento. PostgreSQL
real con `crm_app` (también con hilos a la vez) y un almacén en memoria compartido por la prueba.
La descarga y la tarea son de F7-09a1c: aquí el «intento» recibe los bytes ya bajados."""

import hashlib
import logging
import threading
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from django.core.management import call_command
from django.db import DatabaseError, connection

from apps.conversations import credentials, inbound, media
from apps.conversations.models import ChannelAccount, Message
from core import storage
from core.storage.memory import InMemoryStorage
from core.tenancy.context import ActorType, TenantContext
from core.tenancy.scope import tenant_scope
from tests.meta_sim.graph import TEST_TOKEN
from tests.meta_sim.webhooks import CUSTOMER, PHONE_NUMBER_ID, WABA_ID
from tests.test_memberships import ctx
from tests.test_pipelines_tables import as_migrator

pytestmark = pytest.mark.usefixtures("tenant_db")
DB = psycopg.Connection[Any]
MEDIA = "200000000000001"
JPEG = b"\xff\xd8\xff\xe0" + bytes(60)
STATE = "SELECT download_status, failure_code, attempts FROM message_attachments WHERE id = %s"


class Storage(InMemoryStorage):
    """El almacén en memoria, compartido por la prueba, con fallos a la carta."""

    fail_put = fail_delete = False

    def put(self, key: str, *args: Any, **kwargs: Any) -> Any:
        if self.fail_put:
            raise OSError("put")
        return super().put(key, *args, **kwargs)

    def delete(self, key: str) -> None:
        if self.fail_delete:
            raise OSError("delete")
        super().delete(key)


@pytest.fixture
def world(orgs: dict[str, UUID], settings: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    settings.MEDIA_PER_ORG = 2
    blobs = Storage()
    monkeypatch.setattr(storage, "storage", lambda: blobs)
    mine = uuid4()
    with tenant_scope(ctx(orgs["A"])) as tenant:
        stored = credentials.store(tenant, mine, TEST_TOKEN)
        ChannelAccount.objects.create(
            id=mine, platform="WHATSAPP", name="WA", status="CONNECTED", credential_id=stored.pk,
            external_account_id=PHONE_NUMBER_ID, external_parent_id=WABA_ID,
            last_healthcheck_at="2026-11-01T00:00:00Z",
        )  # fmt: skip
    return SimpleNamespace(a=orgs["A"], b=orgs["B"], account=mine, blobs=blobs)


def receive(w: Any, kind: str = "image", media_id: str = MEDIA, wamid: str = "wamid.M1") -> None:
    """Un medio entrante y su adjunto, como los unirá `inbound.receive` en F7-09a1c."""
    declared = {"id": media_id, "mime_type": "text/html", "filename": "evil.html", "sha256": "f"}
    payload = {"from": CUSTOMER, "id": wamid, "timestamp": "1700000000", "type": kind,
               kind: declared}  # fmt: skip
    context = TenantContext(w.a, "webhook", actor_type=ActorType.INTEGRATION)
    with tenant_scope(context):
        inbound.receive(context, ChannelAccount.objects.get(pk=w.account), payload)
        media.attach(context, Message.objects.get(external_message_id=wamid), kind, payload)


def attachments(migrator: DB) -> list[UUID]:
    rows = migrator.execute("SELECT id FROM message_attachments ORDER BY created_at").fetchall()
    return [row[0] for row in rows]


def one(w: Any, migrator: DB, wamid: str = "wamid.M1") -> UUID:
    receive(w, wamid=wamid)
    return attachments(migrator)[-1]


def state(migrator: DB, pk: UUID) -> tuple[Any, ...]:
    return migrator.execute(STATE, [pk]).fetchone()  # type: ignore[return-value]


def due(migrator: DB, pk: UUID) -> None:  # el plazo o la espera, vencidos: el reloj pasa
    migrator.execute(
        "UPDATE message_attachments SET lease_until = now() - interval '1 second', "
        "not_before = NULL WHERE id = %s",
        [pk],
    )


def files(migrator: DB) -> list[tuple[Any, ...]]:
    sql = "SELECT id, storage_key, original_name, mime_type, size_bytes, sha256, scan_status"
    return migrator.execute(f"{sql}, purpose FROM files").fetchall()


def claimed(org: UUID, pk: UUID) -> Any:
    with tenant_scope(ctx(org)) as tenant:
        return media.claim(tenant, pk)


def bytes_of(content: bytes = JPEG) -> media.Sink:
    sink = media.Sink()
    sink.write(content)
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
            media.attach(tenant, message, "image", {"image": {"id": MEDIA}})
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
        assert isinstance(claimed(world.a, pk), int)  # ni un segundo intento a la vez
        waits.append(finish(world, got, "RETRY")[1])
        if waits[-1]:
            assert claimed(world.a, pk) is None  # ni antes de su espera
            due(migrator, pk)
    assert waits == [30, 120, 600, 3600, None]
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
    wait = claimed(world.a, pk)  # un mensaje repetido de la tarea: vuelve al vencer el plazo
    assert isinstance(wait, int) and 290 < wait <= 301
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
    logged = [r.msg for r in caplog.records if "media" in str(r.msg)]
    allowed = {"event", "attachment_id", "error", "level", "timestamp", "logger", "request_id",
               "correlation_id"}  # fmt: skip
    assert logged and all(set(m) <= allowed and m["error"] == "OSError" for m in logged), logged  # type: ignore[index]


def test_an_expired_lease_at_the_last_attempt_fails_unavailable(world: Any, migrator: DB) -> None:
    pk = one(world, migrator)
    migrator.execute("UPDATE message_attachments SET attempts = 4 WHERE id = %s", [pk])
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
    other = media.Claim(pk, got.attempt_id, "IMAGE", MEDIA, PHONE_NUMBER_ID, got.credential)
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
    "download_status, attempts, created_at, updated_at) SELECT gen_random_uuid(), %s, %s, "
    "'IMAGE', '300000000000001', 'PENDING', 0, now(), now()"
)


def test_the_database_guards_the_table_for_the_app_role(world: Any, migrator: DB) -> None:
    done = one(world, migrator)
    store(world, claimed(world.a, done))
    pending = one(world, migrator, "wamid.M2")
    (message,) = migrator.execute(
        "SELECT message_id FROM message_attachments WHERE id = %s", [done]
    ).fetchone()  # type: ignore[misc]
    denied = [
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
        (INSERT, [world.a, message], "UniqueViolation"),  # un adjunto por mensaje
    ]  # fmt: skip
    for sql, params, error in denied:
        with pytest.raises(DatabaseError) as caught:
            raw(world.a, sql, params)
        assert type(caught.value.__cause__).__name__ == error, sql
    theirs = uuid4()
    migrator.execute(
        "INSERT INTO files (id, organization_id, storage_key, original_name, mime_type, "
        "size_bytes, sha256, purpose, scan_status) VALUES (%s, %s, %s, 'x', 'image/jpeg', 1, %s, "
        "'message-media', 'PENDING')",
        [theirs, world.b, storage.tenant_key(world.b, "message-media", theirs), "0" * 64],
    )
    claimed(world.a, pending)
    with pytest.raises(DatabaseError) as caught:  # el archivo de otra organización: la FK
        raw(world.a, "UPDATE message_attachments SET download_status = 'DONE', file_id = %s "
            "WHERE id = %s", [theirs, pending])  # fmt: skip
    assert caught.value.__cause__.diag.constraint_name == "message_attachments_file_fk"  # type: ignore[union-attr]
    with pytest.raises(DatabaseError) as caught:  # y el mensaje de otra organización, igual
        raw(world.b, INSERT, [world.b, message])
    assert caught.value.__cause__.diag.constraint_name == "message_attachments_message_fk"  # type: ignore[union-attr]


def test_the_trigger_binds_the_migrator_too(world: Any, migrator: DB) -> None:
    pk = one(world, migrator)
    claimed(world.a, pk)
    for sql in (
        "UPDATE message_attachments SET attempts = 0 WHERE id = %s",
        "UPDATE message_attachments SET external_media_id = '300000000000001' WHERE id = %s",
        "UPDATE message_attachments SET created_at = now() WHERE id = %s",
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
        "SELECT count(*) FROM pg_class WHERE relname IN ('files_org_id_uq', 'messages_org_id_uq')"
    )
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
    assert migrator.execute(named, names).fetchone() == (3,)
    assert migrator.execute(index).fetchone() == (2,)
