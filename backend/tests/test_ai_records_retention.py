"""F8-08k (#1033): el job de migraciones quita el detalle de la IA de más de 12 meses completos
(D-F8-05): todas las particiones viejas o ninguna, nunca el mes actual ni los siguientes, y solo
con el rol del migrador."""

import importlib
import logging
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from django.core.management import call_command
from django.db import DatabaseError, ProgrammingError

from apps.ai_agents import apps as ai_apps
from tests.test_access import as_migrator
from tests.test_ai_records import copy, save
from tests.test_conversations_tables import first, world
from tests.test_memberships import raw

__all__ = ["world"]  # el fixture, para pytest
pytestmark = pytest.mark.usefixtures("tenant_db")
DB = psycopg.Connection[Any]
TABLES = ("ai_llm_calls", "ai_runs", "ai_actions")
FUNCTION = "public.ai_records_drop_partitions(integer)"
MONTH = "(date_trunc('month', now() AT TIME ZONE 'UTC') + make_interval(months => %s::int))::date"
PART = (  # el nombre y la sentencia de la partición de hace `n` meses, como `*_ensure_partitions`
    "SELECT p, format('CREATE TABLE public.%%I PARTITION OF public.%%I FOR VALUES FROM (%%L) TO"
    " (%%L)', p, t, m::timestamp AT TIME ZONE 'UTC', (m + interval '1 month')::timestamp AT TIME"
    " ZONE 'UTC') FROM (SELECT %s::text, (date_trunc('month', now() AT TIME ZONE 'UTC')"
    " + make_interval(months => %s::int))::date) x(t, m),"
    " LATERAL (SELECT t || '_y' || to_char(m, 'YYYY\"m\"MM')) y(p)"
)
LOGGED = {"event", "partition", "rows", "level", "timestamp", "logger", "request_id",
          "correlation_id"}  # fmt: skip


def name(migrator: DB, table: str, months: int) -> str:
    row = migrator.execute(PART, [table, months]).fetchone()
    return str(row[0]) if row else ""


def exists(migrator: DB, partition: str) -> bool:
    return migrator.execute("SELECT to_regclass(%s) IS NOT NULL", [partition]).fetchone() == (True,)


@pytest.fixture
def old(migrator: DB) -> Iterator[dict[int, list[str]]]:
    """Las particiones de hace 13, 12 y 11 meses de las tres tablas; se quitan al acabar."""
    made: dict[int, list[str]] = {}
    for months in (-13, -12, -11):
        for table in TABLES:
            part, create = migrator.execute(PART, [table, months]).fetchone()  # type: ignore[misc]
            migrator.execute(create)
            made.setdefault(months, []).append(part)
    yield made
    for parts in made.values():
        for part in parts:
            migrator.execute(f"DROP TABLE IF EXISTS {part} CASCADE")


def age(migrator: DB, table: str, months: int) -> None:
    """Una copia de la última fila de `table` con fecha del mes de hace `months` meses."""
    when = migrator.execute(f"SELECT ({MONTH} + 1) AT TIME ZONE 'UTC'", [months]).fetchone()[0]  # type: ignore[index]
    copy(migrator, table, {"created_at": when.isoformat()})


def migrate() -> None:
    as_migrator(lambda: call_command("migrate", verbosity=0))


def dropped(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    return [
        dict(r.msg) for r in caplog.records
        if isinstance(r.msg, dict) and r.msg["event"] == "ai.records_partition_dropped"
    ]  # fmt: skip


def test_only_the_migrator_runs_it_and_it_is_off_until_the_owner_confirms(migrator: DB) -> None:
    row = migrator.execute(
        "SELECT p.prosecdef, pg_get_userbyid(p.proowner), p.proconfig,"
        " has_function_privilege('public', p.oid, 'EXECUTE'),"
        " has_function_privilege('crm_app', p.oid, 'EXECUTE') FROM pg_proc p"
        " WHERE p.oid = %s::regprocedure",
        [FUNCTION],
    ).fetchone()
    assert row == (False, "crm_migrator", ["search_path=pg_catalog, public, pg_temp"], False, False)
    with pytest.raises(ProgrammingError, match="permission denied"):
        raw("SELECT public.ai_records_drop_partitions(12)")
    assert importlib.import_module("config.settings.base").AI_RECORDS_RETENTION is False
    assert ai_apps.RETENTION_MONTHS == 12  # D-F8-05


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


@pytest.mark.parametrize("months", [None, 0, 11, 121])
def test_it_never_keeps_less_than_twelve_months(migrator: DB, months: int | None) -> None:
    with pytest.raises(psycopg.errors.RaiseException, match="p_keep_months fuera de rango"):
        migrator.execute("SELECT * FROM public.ai_records_drop_partitions(%s)", [months])
    assert exists(migrator, name(migrator, "ai_runs", 0))


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
