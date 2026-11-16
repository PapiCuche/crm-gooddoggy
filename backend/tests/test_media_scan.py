"""F7-15: el antivirus de un medio entrante descargado (`scan`, `tasks.scan_media`, el barredor),
con PostgreSQL real como `crm_app`, el almacén en memoria y un `clamd` falso (`test_antivirus`)."""

import io
import logging
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest

from apps.conversations import media, scan, tasks
from config.celery import app
from core import antivirus, storage
from core.observability import reporting
from core.outbox import handlers_for
from tests.logs import log_text
from tests.test_antivirus import EICAR, FakeClamd
from tests.test_message_media import JPEG, files, one, world

__all__ = ["world"]  # el fixture, para pytest
pytestmark = pytest.mark.usefixtures("tenant_db")
DB = psycopg.Connection[Any]
AUDIT = "SELECT action, changes::text || metadata::text FROM audit_logs WHERE entity_id = %s"


@pytest.fixture
def clamd(settings: Any) -> Any:
    fake = FakeClamd()
    settings.CLAMD_ADDRESS = fake.address
    yield fake
    fake.close()


def stored(w: Any, migrator: DB, content: bytes = JPEG, wamid: str = "wamid.M1") -> UUID:
    """Un medio recibido y descargado: su archivo `PENDING`."""
    pk = one(w, migrator, wamid, content)
    assert media.fetch(w.a, pk) == ("DONE", None)
    (file_id,) = migrator.execute(
        "SELECT file_id FROM message_attachments WHERE id = %s", [pk]
    ).fetchone()  # type: ignore[misc]
    return file_id  # type: ignore[no-any-return]


def status(migrator: DB, file_id: UUID) -> str:
    sql = "SELECT scan_status FROM files WHERE id = %s"
    return str(migrator.execute(sql, [file_id]).fetchone()[0])  # type: ignore[index]


def test_the_download_queues_the_scan_on_the_media_queue(
    world: Any, migrator: DB, monkeypatch: pytest.MonkeyPatch
) -> None:
    file_id = stored(world, migrator)
    queued: list[Any] = []
    monkeypatch.setattr(tasks.scan_media, "apply_async", lambda **kw: queued.append(kw))
    (event,) = migrator.execute(
        "SELECT id FROM outbox_events WHERE event_type = 'file.stored' AND aggregate_id = %s",
        [file_id],
    ).fetchone()  # type: ignore[misc]
    assert tasks.scan_event in handlers_for("file.stored")
    theirs = {"event_id": str(event), "organization_id": str(world.b)}  # el evento es de A
    assert tasks.scan_event.apply(kwargs=theirs).successful() and queued == []
    assert tasks.scan_event.apply(kwargs={**theirs, "organization_id": str(world.a)}).successful()
    assert queued == [{"kwargs": {"file_id": str(file_id), "organization_id": str(world.a)}}]
    assert app.amqp.router.route({}, "conversations.scan_media")["queue"].name == "media"


def test_a_clean_file_is_clean_and_nothing_else_changes_it(
    world: Any, migrator: DB, clamd: FakeClamd
) -> None:
    file_id = stored(world, migrator)
    assert scan.scan(world.b, file_id, 1) == ("SKIPPED", None)  # de A, no de B
    assert status(migrator, file_id) == "PENDING" and clamd.bodies == []
    assert scan.scan(world.a, file_id, 1) == ("CLEAN", None)
    assert clamd.bodies == [JPEG]  # el blob entero, leído del almacén
    assert scan.scan(world.a, file_id, 1) == ("SKIPPED", None)  # ya tiene veredicto
    assert scan.scan(world.a, uuid4(), 1) == ("SKIPPED", None)
    assert status(migrator, file_id) == "CLEAN" and len(clamd.bodies) == 1
    assert migrator.execute(AUDIT, [file_id]).fetchall() == []  # lo limpio no se audita


def test_eicar_is_infected_audited_and_alerted_without_its_content(
    world: Any, migrator: DB, clamd: FakeClamd, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:  # fmt: skip
    seen: list[Any] = []
    capture = lambda *a, **kw: seen.append((a, kw))  # noqa: E731
    monkeypatch.setattr(reporting, "reporter", lambda: type("R", (), {"capture_message": capture}))
    file_id = stored(world, migrator, JPEG + EICAR)
    with caplog.at_level(logging.INFO):
        tasks.scan_media(str(file_id), str(world.a))
    assert status(migrator, file_id) == "INFECTED"
    ((action, text),) = migrator.execute(AUDIT, [file_id]).fetchall()
    assert action == "file.scan_infected" and "Eicar-Test-Signature" in text
    assert seen and seen[0][1]["fingerprint"] == [scan.ALERT]
    logged = log_text(caplog)
    assert scan.ALERT in logged and "Eicar-Test-Signature" in logged
    (key, name) = files(migrator)[0][1:3]
    for hidden in ("EICAR", "X5O", key, name):  # ni bytes, ni clave, ni nombre
        assert hidden not in logged + text + repr(seen), hidden


def test_clamd_down_retries_with_backoff_and_then_is_error_never_clean(
    world: Any, migrator: DB, settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[Any] = []
    capture = lambda *a, **kw: seen.append((a, kw))  # noqa: E731
    monkeypatch.setattr(reporting, "reporter", lambda: type("R", (), {"capture_message": capture}))
    file_id = stored(world, migrator)
    settings.CLAMD_ADDRESS = "127.0.0.1:9"  # nadie escucha
    queued: list[Any] = []
    monkeypatch.setattr(tasks.scan_media, "apply_async", lambda **kw: queued.append(kw))
    for attempt in range(1, scan.MAX_ATTEMPTS):
        tasks.scan_media(str(file_id), str(world.a), attempt)
        assert status(migrator, file_id) == "PENDING"
    assert [q["countdown"] for q in queued] == list(scan.BACKOFF)
    assert [q["kwargs"]["attempt"] for q in queued] == [2, 3, 4]
    tasks.scan_media(**queued[-1]["kwargs"])
    assert status(migrator, file_id) == "ERROR" and len(queued) == 3  # sin más intentos
    ((action, _),) = migrator.execute(AUDIT, [file_id]).fetchall()
    assert action == "file.scan_error"
    assert [s[1]["fingerprint"] for s in seen] == [["media.alert.scan_error"]]  # una caída se ve
    blobs, other = world.blobs, stored(world, migrator, wamid="wamid.M2")
    blobs.objects.clear()  # el almacén no lo tiene: tampoco es un veredicto
    assert scan.scan(world.a, other, 1) == ("RETRY", scan.BACKOFF[0])
    assert status(migrator, other) == "PENDING"


def test_the_sweeper_queues_only_stale_pending_scans_once_per_window(
    world: Any, migrator: DB, clamd: FakeClamd, monkeypatch: pytest.MonkeyPatch
) -> None:
    queued: list[Any] = []
    monkeypatch.setattr(tasks.scan_media, "apply_async", lambda **kw: queued.append(kw))
    old, fresh, done = (stored(world, migrator, wamid=f"wamid.S{i}") for i in range(3))
    scan.scan(world.a, done, 1)
    age = "UPDATE files SET created_at = now() - interval '20 minutes' WHERE id = ANY(%s)"
    migrator.execute(age, [[old, done]])
    assert tasks.sweep_media()["scans"] == 1
    assert queued == [{"kwargs": {"file_id": str(old), "organization_id": str(world.a)}}]
    assert tasks.sweep_media()["scans"] == 0  # marcado: no otra vez antes de `STALE`
    migrator.execute("UPDATE files SET scan_checked_at = now() - interval '16 minutes'")
    assert tasks.sweep_media()["scans"] == 1  # su tarea se perdió: otra vez
    assert status(migrator, fresh) == "PENDING"  # el reciente sigue esperando su evento


class Broken(io.BytesIO):
    def read(self, *args: Any) -> bytes:
        raise RuntimeError("se cortó el almacén a mitad")  # no es un OSError


def test_a_storage_error_mid_stream_is_no_verdict_and_retries(
    world: Any, migrator: DB, clamd: FakeClamd, monkeypatch: pytest.MonkeyPatch
) -> None:
    file_id = stored(world, migrator)
    monkeypatch.setattr(world.blobs, "open", lambda key: Broken(b"x"))
    assert scan.scan(world.a, file_id, 1) == ("RETRY", scan.BACKOFF[0])  # sin excepción
    assert scan.scan(world.a, file_id, scan.MAX_ATTEMPTS) == ("ERROR", None)
    assert status(migrator, file_id) == "ERROR" and clamd.bodies == []


class Unclosable(io.BytesIO):
    """Falla al cerrarse la primera vez y cuenta las veces (la siguiente, al recogerlo, cierra)."""

    closes = 0

    def close(self) -> None:
        self.closes += 1
        if self.closes == 1:
            raise RuntimeError("cerrar falla")
        super().close()


def test_the_blob_is_closed_once_and_a_failing_close_keeps_the_real_verdict(
    world: Any, migrator: DB, clamd: FakeClamd, monkeypatch: pytest.MonkeyPatch
) -> None:
    file_id, blob = stored(world, migrator), Unclosable(JPEG)
    monkeypatch.setattr(world.blobs, "open", lambda key: blob)
    assert scan.scan(world.a, file_id, 1) == ("CLEAN", None)
    assert blob.closes == 1  # cerrado (sin fuga), una vez


def test_only_inbound_media_files_are_scanned(world: Any, migrator: DB, clamd: FakeClamd) -> None:
    out = uuid4()  # un archivo `PENDING` de otro propósito (el que sube una persona para enviar)
    key = storage.tenant_key(world.a, "message-media-out", out)
    world.blobs.put(key, io.BytesIO(JPEG), content_type="image/jpeg", content_length=len(JPEG))
    migrator.execute(
        "INSERT INTO files (id, organization_id, storage_key, original_name, mime_type, "
        "size_bytes, sha256, purpose, scan_status) VALUES (%s, %s, %s, 'imagen.jpg', "
        "'image/jpeg', %s, %s, 'message-media-out', 'PENDING')",
        [out, world.a, key, len(JPEG), "0" * 64],
    )
    assert scan.scan(world.a, out, 1) == ("SKIPPED", None)  # ni se mira
    assert clamd.bodies == [] and status(migrator, out) == "PENDING"


def test_a_verdict_never_overwrites_another_written_meanwhile(
    world: Any, migrator: DB, clamd: FakeClamd, monkeypatch: pytest.MonkeyPatch
) -> None:
    file_id = stored(world, migrator, JPEG + EICAR)
    real = scan._verdict

    def slow(key: str) -> antivirus.Verdict:  # otra tarea escribe mientras esta escanea
        migrator.execute("UPDATE files SET scan_status = 'CLEAN' WHERE id = %s", [file_id])
        return real(key)

    monkeypatch.setattr(scan, "_verdict", slow)
    assert scan.scan(world.a, file_id, 1) == ("SKIPPED", None)
    assert (
        status(migrator, file_id) == "CLEAN" and migrator.execute(AUDIT, [file_id]).fetchall() == []
    )


def test_the_scan_uses_the_size_cap(
    world: Any, migrator: DB, clamd: FakeClamd, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert scan.CAP >= max(media.CAPS.values())  # ningún medio permitido pasa del tope
    file_id = stored(world, migrator)
    monkeypatch.setattr(scan, "CAP", 8)  # menor que el archivo: sin veredicto, nunca `CLEAN`
    assert scan.scan(world.a, file_id, scan.MAX_ATTEMPTS) == ("ERROR", None)
    assert clamd.bodies == []
