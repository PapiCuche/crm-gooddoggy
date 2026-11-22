"""F8-08k (#1033): el job de migraciones quita el detalle de la IA de más de 12 meses completos
(D-F8-05): todas las particiones viejas o ninguna, nunca el mes actual ni los siguientes, y solo
con el rol del migrador."""

import importlib
import logging
import pathlib
import re
import threading
import time
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from django.core.management import call_command
from django.db import DatabaseError, ProgrammingError
from psycopg import sql

from apps.ai_agents import apps as ai_apps
from tests.conftest import migrator_conninfo
from tests.test_access import as_migrator
from tests.test_ai_records import copy, save
from tests.test_conversations_tables import first, world
from tests.test_memberships import raw

__all__ = ["world"]  # el fixture, para pytest
pytestmark = pytest.mark.usefixtures("tenant_db")
DB = psycopg.Connection[Any]
TABLES = ("ai_llm_calls", "ai_runs", "ai_actions")
FUNCTION = "public.ai_records_drop_partitions(integer)"
CALL = "SELECT * FROM public.ai_records_drop_partitions(12)"
MONTH = "(date_trunc('month', now() AT TIME ZONE 'UTC') + make_interval(months => %s::int))::date"
BOUNDS = (  # el sufijo del nombre y los límites del mes UTC de hace `n` meses
    "SELECT to_char(m, 'YYYY\"m\"MM'), (m::timestamp AT TIME ZONE 'UTC')::text,"
    " ((m + make_interval(months => %s::int))::timestamp AT TIME ZONE 'UTC')::text"
    " FROM (SELECT (date_trunc('month', now() AT TIME ZONE 'UTC')"
    " + make_interval(months => %s::int))::date) x(m)"
)
OTHER = "ai_records_elsewhere"  # un esquema que no es `public`
LOGGED = {"event", "partition", "rows", "level", "timestamp", "logger", "request_id",
          "correlation_id"}  # fmt: skip


def bounds(migrator: DB, months: int, span: int = 1) -> tuple[str, str, str]:
    row = migrator.execute(BOUNDS, [span, months]).fetchone()
    assert row is not None
    return row[0], row[1], row[2]


def attach(
    migrator: DB, table: str, months: int, part: str = "", schema: str = "public", span: int = 1
) -> str:
    """Una partición de `table` que cubre `span` meses desde hace `months`; por omisión con el
    nombre de `*_ensure_partitions`."""
    suffix, start, end = bounds(migrator, months, span)
    part = part or f"{table}_y{suffix}"
    migrator.execute(
        sql.SQL("CREATE TABLE {}.{} PARTITION OF public.{} FOR VALUES FROM ({}) TO ({})").format(
            sql.Identifier(schema),
            sql.Identifier(part),
            sql.Identifier(table),
            sql.Literal(start),
            sql.Literal(end),
        )  # fmt: skip
    )
    return part if schema == "public" else f"{schema}.{part}"


def name(migrator: DB, table: str, months: int) -> str:
    return f"{table}_y{bounds(migrator, months)[0]}"


def exists(migrator: DB, partition: str) -> bool:
    return migrator.execute("SELECT to_regclass(%s) IS NOT NULL", [partition]).fetchone() == (True,)


@pytest.fixture
def old(migrator: DB) -> Iterator[dict[int, list[str]]]:
    """Las particiones de hace 13, 12 y 11 meses de las tres tablas; se quitan al acabar, con las
    que añada la prueba (`old[0]`) y el esquema `OTHER`."""
    made: dict[int, list[str]] = {0: []}
    for months in (-13, -12, -11):
        made[months] = [attach(migrator, table, months) for table in TABLES]
    yield made
    for parts in made.values():
        for part in parts:
            migrator.execute(f"DROP TABLE IF EXISTS {part} CASCADE")
    migrator.execute(f"DROP SCHEMA IF EXISTS {OTHER} CASCADE")


def age(migrator: DB, table: str, months: int) -> None:
    """Una copia de la última fila de `table` con fecha del mes de hace `months` meses."""
    when = migrator.execute(f"SELECT ({MONTH} + 1) AT TIME ZONE 'UTC'", [months]).fetchone()[0]  # type: ignore[index]
    copy(migrator, table, {"created_at": when.isoformat()})


def migrate() -> None:
    as_migrator(lambda: call_command("migrate", verbosity=0))


def events(caplog: pytest.LogCaptureFixture, event: str) -> list[dict[str, Any]]:
    return [
        dict(r.msg) for r in caplog.records if isinstance(r.msg, dict) and r.msg["event"] == event
    ]


def dropped(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    return events(caplog, "ai.records_partition_dropped")


def test_only_the_migrator_runs_it_and_it_is_off_until_the_owner_confirms(migrator: DB) -> None:
    row = migrator.execute(
        "SELECT p.prosecdef, pg_get_userbyid(p.proowner), p.proconfig,"
        " has_function_privilege('public', p.oid, 'EXECUTE'),"
        " has_function_privilege('crm_app', p.oid, 'EXECUTE') FROM pg_proc p"
        " WHERE p.oid = %s::regprocedure",
        [FUNCTION],
    ).fetchone()
    fixed = ["search_path=pg_catalog, public, pg_temp", "lock_timeout=5s"]
    assert row == (False, "crm_migrator", fixed, False, False)
    with pytest.raises(ProgrammingError, match="permission denied"):
        raw("SELECT public.ai_records_drop_partitions(12)")
    assert importlib.import_module("config.settings.base").AI_RECORDS_RETENTION is False
    assert ai_apps.RETENTION_MONTHS == 12  # D-F8-05


def test_only_the_base_settings_assign_the_switch() -> None:
    """Encenderla es un PR sobre `base.py`: ni `production.py`, ni `migrate.py`, ni el entorno."""
    root = pathlib.Path(__file__).parents[1]
    assign = re.compile(r"\bAI_RECORDS_RETENTION\b\s*(:[^=\n]*)?=(?!=)|setattr\(.*AI_RECORDS_RET")
    found = sorted(
        str(p.relative_to(root)) for p in root.rglob("*.py")
        if not {".venv", "tests"} & set(p.parts) and assign.search(p.read_text())
    )  # fmt: skip
    assert found == ["config/settings/base.py"]
    base = (root / "config/settings/base.py").read_text()
    assert re.findall(r"^.*AI_RECORDS_RETENTION\b.*=.*$", base, re.M) == [
        "AI_RECORDS_RETENTION = False"
    ]


def test_off_it_drops_nothing(old: dict[int, list[str]], migrator: DB) -> None:
    migrate()
    assert all(exists(migrator, part) for part in old[-13])


def test_a_partition_older_than_twelve_full_months_goes_and_the_rest_stay(
    world: SimpleNamespace,
    old: dict[int, list[str]],
    migrator: DB,
    settings: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    save(world, first(world))
    for months in (-13, -12):
        for table in ("ai_runs", "ai_actions"):
            age(migrator, table, months)
    settings.AI_RECORDS_RETENTION = True
    with caplog.at_level(logging.INFO):
        migrate()
    assert not any(exists(migrator, part) for part in old[-13])
    kept = old[-12] + old[-11] + [name(migrator, t, n) for t in TABLES for n in (0, 12)]
    assert all(exists(migrator, part) for part in kept)
    logs = dropped(caplog)
    assert sorted((m["partition"], m["rows"]) for m in logs) == sorted(
        zip(old[-13], (0, 1, 1), strict=True)
    )
    assert all(set(m) <= LOGGED for m in logs), logs  # nombres y cuentas, nada más
    counts = [migrator.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in TABLES[1:]]  # type: ignore[index]  # noqa: S608
    assert counts == [2, 3]  # lo de hace 12 meses y lo de hoy siguen ahí


def test_it_logs_only_what_is_already_committed(
    old: dict[int, list[str]], migrator: DB, settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[str, bool]] = []

    def info(event: str, partition: str, rows: int) -> None:  # otra conexión: solo lo confirmado
        seen.append((partition, exists(migrator, partition)))

    monkeypatch.setattr(ai_apps, "logger", SimpleNamespace(info=info))
    settings.AI_RECORDS_RETENTION = True
    migrate()
    assert sorted(seen) == sorted((part, False) for part in old[-13])


@pytest.mark.parametrize("zone", ["Pacific/Kiritimati", "Pacific/Pago_Pago"])  # UTC+14, UTC-11
def test_the_session_time_zone_does_not_move_the_cutoff(
    old: dict[int, list[str]], migrator: DB, zone: str
) -> None:
    migrator.execute(sql.SQL("SET TimeZone = {}").format(sql.Literal(zone)))
    assert sorted(p for p, _ in migrator.execute(CALL).fetchall()) == sorted(old[-13])
    assert all(exists(migrator, part) for part in old[-12])


@pytest.mark.parametrize("months", [None, 0, 11, 121])
def test_it_never_keeps_less_than_twelve_months(migrator: DB, months: int | None) -> None:
    with pytest.raises(psycopg.errors.RaiseException, match="p_keep_months fuera de rango"):
        migrator.execute("SELECT * FROM public.ai_records_drop_partitions(%s)", [months])
    assert exists(migrator, name(migrator, "ai_runs", 0))


@pytest.mark.parametrize(
    ("months", "span"),
    [(-10, 1), (-16, 3)],  # el mes de hace 10 (filas recientes) o tres meses con nombre viejo
)
def test_a_partition_that_does_not_cover_the_month_of_its_name_stops_everything(
    world: SimpleNamespace, old: dict[int, list[str]], migrator: DB, months: int, span: int
) -> None:
    liar = attach(migrator, "ai_runs", months, "ai_runs_y2000m01", span=span)
    old[0].append(liar)
    save(world, first(world))
    age(migrator, "ai_runs", months)  # una fila viva en la partición que miente
    with pytest.raises(psycopg.errors.RaiseException, match="no cubre el mes de su nombre"):
        migrator.execute(CALL)
    assert all(exists(migrator, part) for part in [*old[-13], liar])


def test_odd_names_and_other_schemas_are_never_dropped(
    old: dict[int, list[str]], migrator: DB
) -> None:
    migrator.execute(f"CREATE SCHEMA {OTHER}")
    odd = [
        attach(migrator, "ai_runs", -20, "ai_runs_y0000m01"),
        attach(migrator, "ai_runs", -21, "ai_runs_y2001m01x"),
        attach(migrator, "ai_runs", -22, "old_ai_runs_y2001m01"),
        attach(migrator, "ai_runs", -23, "ai_runs_y2001m13"),
        attach(migrator, "ai_runs", -24, schema=OTHER),  # su nombre y su mes, en otro esquema
    ]
    old[0].extend(odd)
    assert sorted(p for p, _ in migrator.execute(CALL).fetchall()) == sorted(old[-13])
    assert all(exists(migrator, part) for part in odd)


def test_a_failure_drops_nothing(
    old: dict[int, list[str]],
    migrator: DB,
    settings: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    last = old[-13][-1]  # `ai_actions`, la última que se quita: las anteriores ya se quitaron
    migrator.execute(f"CREATE VIEW ai_records_hold AS SELECT * FROM {last}")  # noqa: S608
    settings.AI_RECORDS_RETENTION = True
    try:
        with caplog.at_level(logging.INFO), pytest.raises(DatabaseError, match="depend"):
            migrate()
        assert all(exists(migrator, part) for part in old[-13]) and not dropped(caplog)
    finally:
        migrator.execute("DROP VIEW IF EXISTS ai_records_hold")


@pytest.mark.parametrize("busy", ["ai_runs", "partition"])
def test_a_busy_table_never_blocks_the_others_and_it_gives_up_in_seconds(
    old: dict[int, list[str]], migrator: DB, busy: str
) -> None:
    target = old[-13][1] if busy == "partition" else busy  # la de `ai_runs`
    outcome: dict[str, Any] = {}
    with (
        psycopg.connect(migrator_conninfo()) as holder,
        psycopg.connect(migrator_conninfo(), autocommit=True) as runner,
        psycopg.connect(migrator_conninfo()) as probe,
    ):
        holder.execute(f"LOCK TABLE {target} IN ACCESS SHARE MODE")  # noqa: S608
        runner.execute("SET statement_timeout = '20s'")

        def run() -> None:
            try:
                runner.execute(CALL).fetchall()
            except psycopg.Error as error:
                outcome["error"] = error

        started = time.monotonic()
        thread = threading.Thread(target=run)
        thread.start()
        time.sleep(1)
        probe.execute("SET LOCAL lock_timeout = '1s'")  # `ai_llm_calls` sigue libre mientras
        assert probe.execute("SELECT count(*) FROM ai_llm_calls").fetchone() is not None
        probe.rollback()
        thread.join(30)
        elapsed = time.monotonic() - started
        holder.rollback()
    assert isinstance(outcome.get("error"), psycopg.errors.LockNotAvailable), outcome
    assert 4 < elapsed < 10, elapsed
    assert all(exists(migrator, part) for part in old[-13])


def test_the_job_leaves_busy_tables_for_the_next_migrate(
    old: dict[int, list[str]],
    migrator: DB,
    settings: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings.AI_RECORDS_RETENTION = True
    with psycopg.connect(migrator_conninfo()) as holder:
        holder.execute("LOCK TABLE ai_actions IN ACCESS SHARE MODE")
        with caplog.at_level(logging.INFO):
            migrate()  # no falla
        holder.rollback()
    [busy] = events(caplog, "ai.records_retention_lock_busy")
    assert set(busy) <= LOGGED and busy["level"] == "warning"
    assert all(exists(migrator, part) for part in old[-13]) and not dropped(caplog)
    migrate()  # el siguiente lo consigue
    assert not any(exists(migrator, part) for part in old[-13])


def test_any_other_operational_error_still_fails_the_job(
    old: dict[int, list[str]],
    migrator: DB,
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Solo 55P03 se deja para el siguiente `migrate`: un `statement_timeout` (57014, también
    `OperationalError`) mientras espera hace fallar el job."""
    settings.AI_RECORDS_RETENTION = True
    monkeypatch.setenv("PGOPTIONS", "-c statement_timeout=1000")  # la conexión del job
    with psycopg.connect(migrator_conninfo()) as holder:
        holder.execute("LOCK TABLE ai_actions IN ACCESS SHARE MODE")
        with caplog.at_level(logging.INFO), pytest.raises(DatabaseError, match="statement timeout"):
            migrate()
        holder.rollback()
    assert not events(caplog, "ai.records_retention_lock_busy")
    assert all(exists(migrator, part) for part in old[-13])
