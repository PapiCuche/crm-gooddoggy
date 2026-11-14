"""F7-11a: métricas y alertas mínimas de la ingesta de Meta (`apps.conversations.ingest_alerts`,
migraciones `0031` y `0032`; ADR-011, ADR-029 §3). PostgreSQL real con `crm_app` y el simulador
de Meta."""

import datetime as dt
import logging
import threading
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import psycopg
import pytest
from django.conf import settings
from django.db import DatabaseError, InterfaceError, connection, connections

from apps.conversations import ingest_alerts, ingress, tasks
from core.observability import reporting
from core.observability.logging import json_formatter
from core.redaction import redact
from tests.meta_sim import webhooks
from tests.meta_sim.webhooks import CUSTOMER, TEST_APP_SECRET
from tests.test_conversations_tables import conversation, message, world
from tests.test_meta_webhook import channel, post
from tests.test_webhook_ingress import put, text
from tests.test_whatsapp_account import make

__all__ = ["channel", "world"]  # las fixtures, para pytest
pytestmark = pytest.mark.usefixtures("tenant_db")
DB = psycopg.Connection[Any]
W = ingest_alerts.WINDOW
COUNTS = "SELECT * FROM public.meta_ingest_counts(%s, %s)"
PRIVATE = "Texto privado del cliente 987654321"
WRONG = "meta-sim-wrong-app-secret-not-real"  # noqa: S105 — ficticio
ALL = {"signature_invalid", "ingress_failed", "ingress_dead", "ingress_lag_seconds",
       "outbound_failed", "token_invalid_count"}  # fmt: skip


class Spy:
    def __init__(self) -> None:
        self.messages: list[tuple[str, dict[str, Any], list[str]]] = []

    def capture_message(self, message: str, context: Any = None, fingerprint: Any = None) -> None:
        self.messages.append((message, dict(context or {}), list(fingerprint or [])))


@pytest.fixture(autouse=True)
def fresh(monkeypatch: pytest.MonkeyPatch, settings: Any) -> Spy:
    """El contador del proceso a cero, el canal encendido, un reloj fijo y un reporte espía."""
    monkeypatch.setattr(ingest_alerts, "_pending", 0)
    monkeypatch.setattr(ingest_alerts, "_last_flush", 0.0)
    monkeypatch.setattr(ingest_alerts, "_now", lambda: 1000.0)
    settings.META_ENABLED = True
    spy = Spy()
    monkeypatch.setattr(reporting, "reporter", lambda: spy)
    return spy


def bucket_now() -> int:
    with connection.cursor() as cursor:
        cursor.execute("SELECT floor(extract(epoch FROM now()) / %s)::bigint", [W])
        return int(cursor.fetchone()[0])


def at(bucket: int, seconds: int = 10) -> dt.datetime:
    return dt.datetime.fromtimestamp(bucket * W + seconds, dt.UTC)


def sweep(monkeypatch: pytest.MonkeyPatch, current: int) -> list[str]:
    monkeypatch.setattr(ingest_alerts, "_current", lambda: current)
    return list(tasks.meta_alerts())


def failures(migrator: DB, bucket: int, *attempts: int) -> None:
    """Filas de `webhook_failures` dentro de la ventana `bucket` (un ítem cada una)."""
    for n, attempt in enumerate(attempts):
        key = f"wa:msg:f{bucket}-{n}"
        put(key)
        migrator.execute(
            "INSERT INTO webhook_failures (ingress_id, attempt, reason, created_at) "
            "SELECT id, %s, 'ERROR', %s FROM webhook_ingress WHERE event_key = %s",
            [attempt, at(bucket), key],
        )


def failed_sends(world: SimpleNamespace, migrator: DB, bucket: int, *codes: str) -> None:
    if (convo := getattr(world, "convo", None)) is None:  # una conversación abierta por contacto
        world.convo = convo = conversation(world.a, world.contact_a, world.account_a)
    for code in codes:
        sent = message(
            world.a, convo, direction="OUTBOUND", sender_type="USER", body=PRIVATE,
            sender_user_id=world.ana.pk, delivery_status="QUEUED",
        )  # fmt: skip
        migrator.execute(
            "UPDATE messages SET delivery_status = 'FAILED', provider_error_code = %s, "
            "failed_at = %s WHERE id = %s",
            [code, at(bucket), sent.pk],
        )


def token_invalid(org: UUID, migrator: DB, bucket: int, number: str) -> None:
    account = make(org, external_account_id=number, status="ERROR", status_detail="TOKEN_INVALID")
    migrator.execute(
        "UPDATE channel_accounts SET updated_at = %s WHERE id = %s", [at(bucket), account.pk]
    )


def signatures(migrator: DB, bucket: int, count: int) -> None:
    migrator.execute(
        "INSERT INTO login_throttles (key, failures, updated_at) VALUES (%s, %s, now())",
        [f"mwh:sig:{bucket}", count],
    )


def lagging(migrator: DB, seconds: int) -> None:
    put("wa:msg:late")
    migrator.execute(
        "UPDATE webhook_ingress SET next_attempt_at = now() - make_interval(secs => %s) "
        "WHERE event_key = 'wa:msg:late'",
        [seconds],
    )


def everything(world: SimpleNamespace, migrator: DB, bucket: int, short: int) -> None:
    """Cada métrica justo en su umbral por omisión, menos `short`."""
    limits = settings.META_ALERTS
    signatures(migrator, bucket, limits["signature_invalid"] - short)
    failures(migrator, bucket, *[1] * (limits["ingress_failed"] - short))
    failures(migrator, bucket, *[8] * (limits["ingress_dead"] - short))
    failed_sends(world, migrator, bucket, *["TOKEN_INVALID"] * (limits["outbound_failed"] - short))
    for n in range(limits["token_invalid_count"] - short):
        token_invalid(world.a, migrator, bucket, f"5550000{bucket % 1000:03d}{n}")
    lagging(migrator, limits["ingress_lag_seconds"] - short * 120)


def test_without_data_nothing_fires_and_each_window_is_measured_once(
    monkeypatch: pytest.MonkeyPatch, fresh: Spy, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    now = bucket_now()
    assert sweep(monkeypatch, now) == [] and fresh.messages == []
    assert caplog.text.count("meta.ingest.metrics") == 2  # las dos últimas ventanas cerradas
    assert sweep(monkeypatch, now) == [] and caplog.text.count("meta.ingest.metrics") == 2
    assert "meta.alert" not in caplog.text


def test_below_every_threshold_nothing_fires_and_at_each_threshold_each_fires_once(
    world: SimpleNamespace, migrator: DB, monkeypatch: pytest.MonkeyPatch, fresh: Spy,
    caplog: pytest.LogCaptureFixture,
) -> None:  # fmt: skip
    now = bucket_now()
    everything(world, migrator, now - 4, short=1)
    assert sweep(monkeypatch, now - 3) == []  # un punto por debajo de cada umbral
    migrator.execute("DELETE FROM webhook_ingress WHERE event_key = 'wa:msg:late'")
    everything(world, migrator, now - 1, short=0)
    assert sorted(sweep(monkeypatch, now)) == sorted(ALL)
    assert sweep(monkeypatch, now) == [] and sweep(monkeypatch, now - 1) == []  # nunca dos veces
    names = [name for name, _, _ in fresh.messages]
    assert sorted(names) == sorted(f"meta.alert.{name}" for name in ALL)
    assert all(fingerprint == [name] for name, _, fingerprint in fresh.messages)  # agrupa
    context = {name: ctx for name, ctx, _ in fresh.messages}
    assert context["meta.alert.ingress_dead"]["count"] == 1
    assert context["meta.alert.outbound_failed"]["codes"] == [["TOKEN_INVALID", 1]]
    # Tras el redactor (el de los logs y el de Sentry), el código sigue a la vista.
    assert redact(context["meta.alert.outbound_failed"])["codes"] == [["TOKEN_INVALID", 1]]
    record = next(r for r in caplog.records if "meta.alert.outbound_failed" in str(r.msg))
    assert '"codes": [["TOKEN_INVALID", 1]]' in json_formatter().format(record)  # el JSON real


def test_a_window_that_was_skipped_is_caught_up_but_the_lag_only_counts_now(
    world: SimpleNamespace, migrator: DB, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = bucket_now()
    failures(migrator, now - 2, 8)
    lagging(migrator, 3600)
    # Beat se saltó la ventana `now - 2`: la recoge la pasada siguiente, y el retraso sale una vez.
    assert sweep(monkeypatch, now) == ["ingress_dead", "ingress_lag_seconds"]
    assert sweep(monkeypatch, now + 1) == ["ingress_lag_seconds"]  # sigue parada: otra ventana


def test_policy_refusals_are_measured_but_never_alert_and_a_raised_threshold_holds(
    world: SimpleNamespace, migrator: DB, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture, settings: Any,
) -> None:  # fmt: skip
    caplog.set_level(logging.INFO)
    now = bucket_now()
    failed_sends(world, migrator, now - 1, *settings.META_ALERT_QUIET_CODES)
    assert sweep(monkeypatch, now) == [] and "WINDOW_CLOSED" in caplog.text  # medido, sin alerta
    settings.META_ALERTS = settings.META_ALERTS | {"outbound_failed": 3}
    failed_sends(world, migrator, now, "PROVIDER_ERROR", "PROVIDER_ERROR")
    assert sweep(monkeypatch, now + 1) == []  # 2 de 3
    settings.META_ALERTS = settings.META_ALERTS | {"outbound_failed": 0, "ingress_dead": 0}
    assert sweep(monkeypatch, now + 2) == []  # un umbral a 0 no dispara sin datos


def test_a_burst_of_bad_signatures_through_the_webhook_counts_without_a_write_per_request(
    migrator: DB, monkeypatch: pytest.MonkeyPatch, fresh: Spy
) -> None:
    body = webhooks.encode(webhooks.envelope(webhooks.value(messages=[text("wamid.1")])))
    bad = webhooks.deliver(body, secret=WRONG)
    writes: list[str] = []
    real, before = connection.cursor, bucket_now()

    def counting() -> Any:
        writes.append("w")
        return real()

    monkeypatch.setattr(connection, "cursor", counting)
    statuses = {post(bad).status_code for _ in range(25)}  # un mismo segundo del proceso
    assert statuses == {401} and writes == ["w"]  # solo la primera llega a la base
    monkeypatch.setattr(ingest_alerts, "_now", lambda: 1001.0)
    assert post(bad).status_code == 401 and writes == ["w", "w"]  # el resto, al pasar el segundo
    monkeypatch.undo()
    monkeypatch.setattr(reporting, "reporter", lambda: fresh)
    found = migrator.execute(
        "SELECT key, failures FROM login_throttles WHERE key LIKE 'mwh:sig:%%'"
    ).fetchall()
    assert len(found) == 1 and found[0][1] == 26
    bucket = int(found[0][0].removeprefix("mwh:sig:"))
    assert bucket in (before, bucket_now())  # la ventana en curso, y vence al acabar esta
    assert migrator.execute(
        "SELECT updated_at FROM login_throttles WHERE key = %s", [found[0][0]]
    ).fetchone() == (at(bucket + 1, 0),)
    assert sweep(monkeypatch, bucket + 1) == ["signature_invalid"]
    assert post(webhooks.deliver(body)).status_code == 200  # una firma buena no cuenta
    assert migrator.execute("SELECT sum(failures) FROM login_throttles WHERE key LIKE 'mwh:sig:%%'"
                            ).fetchone() == (26,)  # fmt: skip


@pytest.mark.parametrize("error", [DatabaseError, InterfaceError])
def test_a_database_that_cannot_count_never_changes_the_401(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, error: type[Exception]
) -> None:
    def down(*args: Any, **kwargs: Any) -> Any:
        raise error("sin base")

    monkeypatch.setattr(connection, "cursor", down)
    body = webhooks.encode(webhooks.envelope(webhooks.value(messages=[text("wamid.1")])))
    response = post(webhooks.deliver(body, signature="sha256=" + "0" * 64))
    assert (response.status_code, response.content) == (401, b"")
    assert "meta.webhook.count_failed" in caplog.text


def test_the_acceptance_with_the_simulator_a_dead_a_burst_and_a_failed_send_alert_once(
    world: SimpleNamespace, migrator: DB, monkeypatch: pytest.MonkeyPatch, fresh: Spy,
    caplog: pytest.LogCaptureFixture,
) -> None:  # fmt: skip
    caplog.set_level(logging.DEBUG)
    body = webhooks.encode(webhooks.envelope(webhooks.value(messages=[text("wamid.9", PRIVATE)])))
    for n in range(settings.META_ALERTS["signature_invalid"]):
        monkeypatch.setattr(ingest_alerts, "_now", lambda n=n: 2000.0 + n)  # un segundo cada una
        assert post(webhooks.deliver(body, secret=WRONG)).status_code == 401
    assert post(webhooks.deliver(body)).status_code == 200  # firmado: se guarda
    migrator.execute("UPDATE webhook_ingress SET attempts = 7")
    claimed = ingress.claim()
    assert [ingress.mark(item, "FAILED", reason="ERROR") for item in claimed] == ["DEAD"]
    failed_sends(world, migrator, bucket_now(), "PROVIDER_ERROR")
    now = bucket_now()  # todo lo de arriba, en la ventana en curso o la anterior
    migrator.execute("UPDATE messages SET failed_at = now() WHERE failed_at IS NOT NULL")
    fired = sweep(monkeypatch, now + 1) + sweep(monkeypatch, now + 2)
    assert sorted(fired) == ["ingress_dead", "outbound_failed", "signature_invalid"]
    assert sweep(monkeypatch, now + 2) == []
    leaks = (PRIVATE, CUSTOMER, "wamid.9", TEST_APP_SECRET, WRONG, "Cliente de prueba")
    for leak in leaks:
        assert leak not in caplog.text and leak not in str(fresh.messages), leak


def test_two_sweeps_at_once_alert_once(
    migrator: DB, monkeypatch: pytest.MonkeyPatch, fresh: Spy
) -> None:
    now = bucket_now()
    failures(migrator, now - 1, 8)
    monkeypatch.setattr(ingest_alerts, "_current", lambda: now)
    barrier, results = threading.Barrier(2), []

    def run() -> None:
        try:
            barrier.wait()
            results.append(ingest_alerts.sweep())
        finally:
            connections.close_all()

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results) == [[], ["ingress_dead"]] and len(fresh.messages) == 1


def test_a_broken_reporter_never_stops_the_other_alerts_nor_the_log(
    world: SimpleNamespace, migrator: DB, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:  # fmt: skip
    class Broken:
        def capture_message(self, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError("sin Sentry")

    monkeypatch.setattr(reporting, "reporter", lambda: Broken())
    now = bucket_now()
    failures(migrator, now - 1, 8)
    failed_sends(world, migrator, now - 1, "RATE_LIMITED")
    assert sweep(monkeypatch, now) == ["ingress_dead", "outbound_failed"]
    assert caplog.text.count("meta.alert.report_failed") == 2
    assert "meta.alert.ingress_dead" in caplog.text and "ERROR" in caplog.text


def test_with_the_channel_off_the_sweep_does_nothing(
    migrator: DB, monkeypatch: pytest.MonkeyPatch, settings: Any
) -> None:
    settings.META_ENABLED = False
    failures(migrator, bucket_now() - 1, 8)
    assert sweep(monkeypatch, bucket_now()) == []
    assert migrator.execute("SELECT count(*) FROM login_throttles").fetchone() == (0,)


@pytest.mark.parametrize(
    "start, end",
    [
        (None, dt.datetime(2026, 1, 1, tzinfo=dt.UTC)),
        (dt.datetime(2026, 1, 1, tzinfo=dt.UTC), None),
        (dt.datetime(2026, 1, 1, tzinfo=dt.UTC), dt.datetime(2026, 1, 1, tzinfo=dt.UTC)),
        (dt.datetime(2026, 1, 2, tzinfo=dt.UTC), dt.datetime(2026, 1, 1, tzinfo=dt.UTC)),
        (dt.datetime(2026, 1, 1, tzinfo=dt.UTC), dt.datetime(2026, 1, 2, 0, 0, 1, tzinfo=dt.UTC)),
    ],
)
def test_the_counts_function_refuses_a_window_it_cannot_trust(
    start: dt.datetime | None, end: dt.datetime | None
) -> None:
    with connection.cursor() as cursor, pytest.raises(DatabaseError) as error:
        cursor.execute(COUNTS, [start, end])
    assert error.value.__cause__.sqlstate == "22023"  # type: ignore[union-attr]


def test_the_counts_function_gives_only_counts_and_closed_codes_and_is_locked_down(
    world: SimpleNamespace, migrator: DB
) -> None:
    now = bucket_now()
    failures(migrator, now - 1, 1, 8)
    failed_sends(world, migrator, now - 1, "PROVIDER_ERROR")
    failed_sends(world, migrator, now - 1, "RATE_LIMITED")  # falla justo al cerrar: la siguiente
    migrator.execute(
        "UPDATE messages SET failed_at = %s WHERE provider_error_code = 'RATE_LIMITED'",
        [at(now, 0)],
    )
    token_invalid(world.a, migrator, now - 1, "555000000099")  # cuenta, pero sin código
    other = make(world.b, status="ERROR", status_detail="PROVIDER_ERROR")  # otro motivo: no cuenta
    migrator.execute(
        "UPDATE channel_accounts SET updated_at = %s WHERE id = %s", [at(now - 1), other.pk]
    )
    put("wa:msg:later")  # un reintento programado para dentro de una hora no es retraso
    migrator.execute(
        "UPDATE webhook_ingress SET status = 'FAILED', next_attempt_at = now() + interval '1 hour',"
        " received_at = now() - interval '2 hours'"
    )
    with connection.cursor() as cursor:  # como `crm_app`, sin tenant: cruza solo por la función
        cursor.execute(COUNTS, [at(now - 1, 0), at(now, 0)])
        assert [c.name for c in cursor.description] == ["metric", "code", "total"]
        found = {(m, c): t for m, c, t in cursor.fetchall()}
    assert found == {
        ("ingress_failed", None): 1, ("ingress_dead", None): 1, ("ingress_lag_seconds", None): 0,
        ("token_invalid_count", None): 1, ("outbound_failed", "PROVIDER_ERROR"): 1,
    }  # fmt: skip
    for table in ("webhook_failures", "webhook_ingress"):
        with connection.cursor() as cursor, pytest.raises(DatabaseError, match="permission denied"):
            cursor.execute(f"SELECT count(*) FROM {table}")  # noqa: S608
        connection.close()
    with connection.cursor() as cursor:  # RLS: sin tenant, `crm_app` no ve ningún mensaje
        cursor.execute("SELECT count(*) FROM messages")
        assert cursor.fetchone() == (0,)
    row = migrator.execute(
        "SELECT p.prosecdef, pg_get_userbyid(p.proowner), p.proconfig, p.provolatile,"
        " has_function_privilege('public', p.oid, 'EXECUTE'),"
        " has_function_privilege('crm_app', p.oid, 'EXECUTE')"
        " FROM pg_proc p WHERE p.oid = 'public.meta_ingest_counts(timestamptz, timestamptz)'"
        "::regprocedure"
    ).fetchone()
    assert row == (True, "crm_migrator", ["search_path=pg_catalog, public, pg_temp"], "s", False,
                   True)  # fmt: skip


def test_a_temp_table_cannot_shadow_the_counts(migrator: DB) -> None:
    now = bucket_now()
    with connection.cursor() as cursor:
        cursor.execute(
            "CREATE TEMP TABLE webhook_failures (created_at timestamptz, attempt integer)"
        )
        cursor.execute("INSERT INTO pg_temp.webhook_failures VALUES (%s, 8)", [at(now - 1)])
        cursor.execute(COUNTS, [at(now - 1, 0), at(now, 0)])
        assert ("ingress_dead", None, 0) in cursor.fetchall()  # la fila falsa no cuenta
    connection.close()


def test_the_task_is_scheduled_and_its_windows_outlive_the_purge(settings: Any) -> None:
    job = settings.CELERY_BEAT_SCHEDULE["conversations.meta_alerts"]
    assert job == {"task": tasks.meta_alerts.name, "schedule": 60.0, "options": {"expires": 60}}
    assert tasks.meta_alerts.tenancy == "platform"
    # Un contador (fechado al fin de su ventana) se lee hasta 2 ventanas y la gracia después, y
    # una reclamación vive 2 ventanas: la purga de los contadores de acceso (cada hora) solo borra
    # lo que lleva su calma sin tocarse.
    calm = max(window for _, window, *_ in settings.LOGIN_THROTTLE.values())
    assert 2 * W + ingest_alerts.GRACE <= calm
    assert set(settings.META_ALERTS) == ALL


def test_sentry_groups_the_same_alert_by_its_fingerprint() -> None:
    seen: dict[str, Any] = {}

    class Scope:
        def __enter__(self) -> Scope:
            return self

        def __exit__(self, *args: Any) -> None:
            seen["fingerprint"] = getattr(self, "fingerprint", None)

        def set_context(self, key: str, value: Any) -> None:
            seen["context"] = value

    sdk = SimpleNamespace(new_scope=Scope, capture_message=lambda m: seen.setdefault("message", m))
    sentry = object.__new__(reporting.SentryReporter)
    sentry._sdk = sdk  # type: ignore[assignment]
    sentry.capture_message("meta.alert.ingress_dead", {"count": 1}, fingerprint=["meta.alert.x"])
    assert seen == {"context": {"count": 1}, "message": "meta.alert.ingress_dead",
                    "fingerprint": ["meta.alert.x"]}  # fmt: skip


def test_a_sweep_that_breaks_midway_gives_its_window_back(
    migrator: DB, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = bucket_now()
    failures(migrator, now - 2, 8)
    real, calls = ingest_alerts._measure, []

    def once(*args: Any) -> Any:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("caída a medias")
        return real(*args)

    monkeypatch.setattr(ingest_alerts, "_measure", once)
    with pytest.raises(RuntimeError):
        sweep(monkeypatch, now)
    claims = "SELECT count(*) FROM login_throttles WHERE key LIKE 'mwh:alr:%%'"
    assert migrator.execute(claims).fetchone() == (0,)  # la reclamación se deshizo
    assert sweep(monkeypatch, now) == ["ingress_dead"]  # y la ventana se evalúa de nuevo


def test_the_current_window_trails_the_database_clock_by_the_grace() -> None:
    grace = ingest_alerts.GRACE
    assert ingest_alerts.window_of(10 * W + grace) == 10
    assert ingest_alerts.window_of(10 * W + grace - 0.001) == 9  # aún en gracia: no se cierra
    with connection.cursor() as cursor:
        cursor.execute("SELECT extract(epoch FROM now())")
        before = float(cursor.fetchone()[0])
    found = ingest_alerts._current()  # sin parchear: el reloj de la base
    assert found in (ingest_alerts.window_of(before), ingest_alerts.window_of(before + 1))
