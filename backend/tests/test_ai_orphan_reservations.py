"""F8-05b5 (#962; ADR-034 §5, ADR-044): las reservas de IA huérfanas se cierran como gastadas
(`ORPHANED`, la reserva entera, nunca liberada), dentro de la organización de cada una, que sale de
una función SECURITY DEFINER de solo lectura que devuelve solo ids. Claves ficticias."""

import importlib
import logging
import threading
import time
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import psycopg
import pytest
from django.conf import settings
from django.core.management import call_command
from django.db import DatabaseError, connection

from apps.ai_gateway import gateway, orphans, tasks
from core.tenancy.context import TenantContextError
from core.tenancy.scope import tenant_scope
from tests import test_ai_gateway
from tests.conftest import migrator_conninfo
from tests.test_ai_budget_ledger import RESERVE, SETTLE, refused, scalar
from tests.test_ai_gateway import DB, budget
from tests.test_memberships import ctx, join
from tests.test_pipelines_tables import as_migrator

pytestmark = pytest.mark.usefixtures("tenant_db")
world = test_ai_gateway.world  # la fixture de F8-05
MIGRATION = importlib.import_module("apps.ai_gateway.migrations.0007_orphan_reservations")
DAILY = importlib.import_module("apps.ai_gateway.migrations.0004_daily_budget")
DISCOVERY = "public.ai_budget_orphan_organizations(integer)"
SETTLE_SIG = (
    "public.ai_budget_settle(uuid, text, numeric, integer, integer, integer, integer, integer,"
    " text)"
)
MINUTES = int(orphans.ORPHAN_AFTER.total_seconds() // 60)
IDS = {"request_id", "correlation_id"}  # los de todo log
DONE = ("closed", "skipped", "failed")
AGE = "UPDATE ai_budget_reservations SET created_at = created_at - make_interval(mins => %s)"
CALLS = (
    "SELECT r.organization_id, c.outcome, c.reserved_usd = c.cost_usd, c.input_tokens,"
    " c.output_tokens, c.request_id FROM ai_llm_calls c JOIN ai_budget_reservations r"
    " ON r.id = c.reservation_id ORDER BY r.created_at"
)
OPEN = "SELECT id FROM ai_budget_reservations WHERE settled_at IS NULL ORDER BY created_at"
USAGE = (
    "SELECT u.organization_id, u.spent_usd, u.reserved_usd, d.spent_usd, d.reserved_usd"
    " FROM ai_budget_usage u JOIN ai_budget_daily_usage d USING (organization_id)"
    " ORDER BY u.spent_usd"
)


def hold(w: SimpleNamespace, org: UUID, amount: str = "0.5") -> UUID:
    """Una reserva nueva de `org`, pedida por Ana."""
    with tenant_scope(ctx(org, w.ana)):
        held: UUID = scalar(RESERVE, [amount, "SUGGEST"])
    return held


def age(migrator: DB, held: UUID, minutes: int) -> None:
    migrator.execute(AGE + " WHERE id = %s", [minutes, held])


def logged(caplog: pytest.LogCaptureFixture, event: str) -> list[dict[str, Any]]:
    """Los registros estructurados de un evento, como diccionarios."""
    return [
        dict(r.msg) for r in caplog.records if isinstance(r.msg, dict) and r.msg["event"] == event
    ]


def discover(limit: int | None) -> list[UUID]:
    with connection.cursor() as cursor:
        cursor.execute("SELECT * FROM public.ai_budget_orphan_organizations(%s)", [limit])
        return [row[0] for row in cursor.fetchall()]


@pytest.fixture
def both(world: SimpleNamespace, migrator: DB) -> SimpleNamespace:
    """A y B con topes, y Ana miembro de las dos."""
    join(world.b, world.ana)
    budget(migrator, world.a, "100")
    budget(migrator, world.b, "100")
    return world


def test_the_discovery_is_a_read_only_definer_and_crm_app_runs_no_ddl(migrator: DB) -> None:
    row = migrator.execute(
        "SELECT p.prosecdef, pg_get_userbyid(p.proowner), p.proconfig, p.provolatile,"
        " p.proretset, p.prorettype::regtype::text, has_function_privilege('public', p.oid,"
        " 'EXECUTE'), has_function_privilege('crm_app', p.oid, 'EXECUTE'), p.prosrc"
        " FROM pg_proc p WHERE p.oid = %s::regprocedure",
        [DISCOVERY],
    ).fetchone()
    assert row is not None
    fixed = ["search_path=pg_catalog, public, pg_temp"]
    assert row[:8] == (True, "crm_migrator", fixed, "s", True, "uuid", False, True)
    assert "least(coalesce(p_limit, 0), 500)" in row[8]  # tope de 500 dentro, sea cual sea p_limit
    ensure = "public.ai_llm_calls_ensure_partitions(integer)"  # el mutante S09 de #933
    for role in ("public", "crm_app"):
        acl = "SELECT has_function_privilege(%s, %s, 'EXECUTE')"
        assert migrator.execute(acl, [role, ensure]).fetchone() == (False,), role
    with tenant_scope(ctx(UUID(int=1))), pytest.raises(DatabaseError, match="permission denied"):
        connection.cursor().execute(f"SELECT {ensure.replace('integer', '12')}")


def test_the_discovery_returns_only_ids_of_organizations_with_old_open_reservations(
    both: SimpleNamespace, migrator: DB
) -> None:
    hold(both, both.a)  # joven
    settled = hold(both, both.b)
    age(migrator, settled, 60)
    with tenant_scope(ctx(both.b)):  # vieja pero ya ajustada
        assert scalar(SETTLE, [settled, "PROVIDER_ERROR", 0, None, None, ""]) == "SETTLED"
    assert discover(10) == []
    a_old, b_old = hold(both, both.a), hold(both, both.b)
    age(migrator, a_old, MINUTES - 1)  # todavía no
    age(migrator, b_old, MINUTES + 1)
    assert discover(10) == [both.b]
    age(migrator, a_old, 10)  # ahora la de A es la más atrasada
    assert discover(10) == [both.a, both.b]
    assert discover(1) == [both.a] and discover(0) == [] and discover(None) == []
    assert discover(-1) == []


def test_crm_app_closes_only_its_own_old_reservation_whole_and_never_releases(
    both: SimpleNamespace, migrator: DB
) -> None:
    held, other = hold(both, both.a), hold(both, both.b)
    with tenant_scope(ctx(both.a)):  # joven: nada cambia
        assert scalar(SETTLE, [held, "ORPHANED", None, None, None, ""]) is None
    age(migrator, held, MINUTES + 1)
    age(migrator, other, MINUTES + 1)
    for cost, tokens in ((0, None), ("0.1", None), (None, 1), ("0.5", 1)):  # nunca la libera
        with tenant_scope(ctx(both.a)):
            assert refused(SETTLE, [held, "ORPHANED", cost, tokens, tokens, ""]) == (
                "ai_budget_settle_cost"
            )
    with tenant_scope(ctx(both.a)):  # la de B, desde A: como si no existiera
        assert scalar(SETTLE, [other, "ORPHANED", None, None, None, ""]) is None
    for statement in (  # ni envejece una reserva ni la cierra por otro camino
        AGE.replace("%s", "60"),
        "UPDATE ai_budget_reservations SET settled_at = now()",
        "UPDATE ai_budget_usage SET reserved_usd = 0",
    ):
        with tenant_scope(ctx(both.a)), pytest.raises(DatabaseError, match="permission denied"):
            connection.cursor().execute(statement)
    assert migrator.execute(CALLS).fetchall() == []
    with tenant_scope(ctx(both.a)):
        assert scalar(SETTLE, [held, "ORPHANED", None, None, None, ""]) == "SETTLED"
        assert scalar(SETTLE, [held, "ORPHANED", None, None, None, ""]) is None  # una vez
        assert scalar(SETTLE, [held, "OK", 1, 1, 1, ""]) is None  # ni un ajuste tardío
    assert migrator.execute(CALLS).fetchall() == [(both.a, "ORPHANED", True, None, None, "")]
    half = Decimal("0.5")
    assert migrator.execute(USAGE).fetchall() == [
        (both.b, 0, half, 0, half), (both.a, half, 0, half, 0)]  # fmt: skip
    assert migrator.execute(OPEN).fetchall() == [(other,)]


def test_the_sweep_closes_each_orphan_in_its_own_organization_and_nothing_else(
    both: SimpleNamespace, migrator: DB, caplog: pytest.LogCaptureFixture
) -> None:
    old = [hold(both, org, amount) for org, amount in ((both.a, "0.5"), (both.b, "0.25"),
                                                       (both.a, "0.125"))]  # fmt: skip
    paid = hold(both, both.a, "0.0625")
    young = hold(both, both.b, "0.03125")
    for held in (*old, paid):
        age(migrator, held, MINUTES + 1)
    with tenant_scope(ctx(both.a)):
        assert scalar(SETTLE, [paid, "OK", "0.01", 1, 1, "r"]) == "SETTLED"
    with caplog.at_level(logging.DEBUG):
        assert tasks.close_orphan_reservations() == {"closed": 3, "skipped": 0, "failed": 0}
    assert migrator.execute(CALLS).fetchall() == [
        (both.a, "ORPHANED", True, None, None, ""), (both.b, "ORPHANED", True, None, None, ""),
        (both.a, "ORPHANED", True, None, None, ""), (both.a, "OK", False, 1, 1, "r")]  # fmt: skip
    assert migrator.execute(OPEN).fetchall() == [(young,)]
    spent = "SELECT organization_id, spent_usd, reserved_usd FROM ai_budget_usage ORDER BY 2"
    assert migrator.execute(spent).fetchall() == [
        (both.b, Decimal("0.25"), Decimal("0.03125")), (both.a, Decimal("0.635"), 0)]  # fmt: skip
    text = " ".join(str({k: v for k, v in dict(r.msg).items() if k != "timestamp"})
                    for r in caplog.records if isinstance(r.msg, dict))  # fmt: skip
    for amount in ("0.5", "0.25", "0.125", "0.635"):
        assert amount not in text  # ni importes en los logs
    [summary] = logged(caplog, "ai.orphans_closed")
    assert set(summary) == {"event", "level", "logger", "timestamp", *IDS, *DONE}
    assert {k: summary[k] for k in DONE} == {"closed": 3, "skipped": 0, "failed": 0}
    assert tasks.close_orphan_reservations() == {"closed": 0, "skipped": 0, "failed": 0}
    with tenant_scope(ctx(both.a)), pytest.raises(TenantContextError):
        tasks.close_orphan_reservations()  # de plataforma: nunca con un tenant ya puesto


def test_a_pass_respects_its_organization_and_batch_limits(
    both: SimpleNamespace, migrator: DB
) -> None:
    first, second, other = hold(both, both.a), hold(both, both.a), hold(both, both.b)
    for held in (first, second, other):
        age(migrator, held, MINUTES + 1)
    assert orphans.close(max_orgs=1, batch_size=1) == {"closed": 1, "skipped": 0, "failed": 0}
    assert migrator.execute(OPEN).fetchall() == [(second,), (other,)]  # la más vieja de A
    assert orphans.close(batch_size=1) == {"closed": 2, "skipped": 0, "failed": 0}
    assert migrator.execute(OPEN).fetchall() == []


def test_one_failure_does_not_stop_the_others(
    both: SimpleNamespace, migrator: DB, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:  # fmt: skip
    first, second, other = hold(both, both.a), hold(both, both.a), hold(both, both.b)
    age(migrator, first, MINUTES + 30)  # B, la menos atrasada: va después de A
    for held in (second, other):
        age(migrator, held, MINUTES + 1)
    real_settle, real_open, calls = gateway.settle, orphans._open, list[Any]()

    def flaky(*args: Any, **kwargs: Any) -> bool:
        calls.append(args[1])
        if len(calls) == 1:
            raise RuntimeError("caída")
        return real_settle(*args, **kwargs)

    monkeypatch.setattr(gateway, "settle", flaky)
    with caplog.at_level(logging.ERROR):
        assert orphans.close() == {"closed": 2, "skipped": 0, "failed": 1}
    assert calls == [first, second, other]
    [failed, *_] = [r for r in caplog.records if "ai.orphan_close_failed" in str(r.msg)]
    assert set(failed.msg) - {"event", "level", "logger", "timestamp"} <= {"error", *IDS}
    assert migrator.execute(OPEN).fetchall() == [(first,)]
    monkeypatch.setattr(gateway, "settle", real_settle)

    def broken(c: Any, batch_size: int) -> list[UUID]:
        if c.organization_id == both.a:
            raise RuntimeError("caída")
        return real_open(c, batch_size)

    third = hold(both, both.b)
    age(migrator, third, MINUTES + 1)
    monkeypatch.setattr(orphans, "_open", broken)
    caplog.clear()
    with caplog.at_level(logging.ERROR):
        assert orphans.close() == {"closed": 1, "skipped": 0, "failed": 1}  # A falla; B sigue
    [org_failed] = logged(caplog, "ai.orphan_close_failed")
    assert set(org_failed) <= {"event", "level", "logger", "timestamp", "error", *IDS}
    assert org_failed["error"] == "RuntimeError" and str(both.a) not in str(org_failed)
    assert migrator.execute(OPEN).fetchall() == [(first,)]


def test_two_sweeps_at_once_close_an_orphan_once(both: SimpleNamespace, migrator: DB) -> None:
    held = hold(both, both.a)
    age(migrator, held, MINUTES + 1)
    results: list[Any] = []

    def run() -> None:
        try:
            results.append(orphans.close())
        except Exception as error:  # pragma: no cover — se ve en el assert
            results.append(error)
        finally:
            connection.close()

    with psycopg.connect(migrator_conninfo()) as holder:
        holder.execute("SELECT 1 FROM ai_budget_reservations WHERE id = %s FOR UPDATE", [held])
        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads:
            thread.start()
        waiting = "SELECT count(*) FROM pg_locks WHERE NOT granted"
        for _ in range(200):
            if migrator.execute(waiting).fetchone() == (2,):
                break
            time.sleep(0.05)
        assert migrator.execute(waiting).fetchone() == (2,)  # los dos, a la vez
        holder.rollback()
    for thread in threads:
        thread.join(timeout=30)
    assert sorted(r["closed"] for r in results) == [0, 1]
    assert sorted(r["skipped"] for r in results) == [0, 1]
    assert [r["failed"] for r in results] == [0, 0]
    assert len(migrator.execute(CALLS).fetchall()) == 1
    usage = "SELECT spent_usd, reserved_usd FROM ai_budget_usage WHERE organization_id = %s"
    assert migrator.execute(usage, [both.a]).fetchall() == [(Decimal("0.5"), 0)]


def test_a_short_partition_horizon_is_reported(
    migrator: DB, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.ERROR):
        orphans.close()
    assert not [r for r in caplog.records if "horizon_short" in str(r.msg)]
    gone = migrator.execute(
        "SELECT 'ai_llm_calls_y' || to_char(now() AT TIME ZONE 'UTC' + interval '2 months',"
        " 'YYYY\"m\"MM')"
    ).fetchone()[0]  # type: ignore[index]
    migrator.execute(f"DROP TABLE {gone}")
    try:
        with caplog.at_level(logging.ERROR):
            orphans.close()
        assert [r for r in caplog.records if "ai.llm_calls_horizon_short" in str(r.msg)]
    finally:
        migrator.execute("SELECT ai_llm_calls_ensure_partitions(12)")


def test_one_age_everywhere_and_the_beat_runs_the_sweep(migrator: DB) -> None:
    assert MINUTES == 15 and orphans.ORPHAN_AFTER.total_seconds() == MINUTES * 60
    assert f"interval '{MINUTES} minutes'" == MIGRATION.ORPHAN_AGE
    for signature in (DISCOVERY, SETTLE_SIG):
        source = "SELECT prosrc FROM pg_proc WHERE oid = %s::regprocedure"
        [(body,)] = migrator.execute(source, [signature]).fetchall()
        assert body.count(f"now() - {MIGRATION.ORPHAN_AGE}") == 1, signature
    beat = settings.CELERY_BEAT_SCHEDULE["ai_gateway.close_orphan_reservations"]
    assert beat["task"] == tasks.close_orphan_reservations.name
    assert (beat["schedule"], beat["options"]) == (300.0, {"expires": 300})  # sin acumular
    assert tasks.close_orphan_reservations.tenancy == "platform"


def _settle_source(migrator: DB) -> Any:
    found = migrator.execute(
        "SELECT prosrc, prosecdef, proconfig, pg_get_userbyid(proowner),"
        " has_function_privilege('public', oid, 'EXECUTE'),"
        " has_function_privilege('crm_app', oid, 'EXECUTE') FROM pg_proc"
        " WHERE oid = %s::regprocedure",
        [SETTLE_SIG],
    ).fetchone()
    check = migrator.execute(
        "SELECT pg_get_constraintdef(oid) LIKE '%%ORPHANED%%' FROM pg_constraint"
        " WHERE conname = 'ai_llm_calls_outcome_check' AND conrelid = 'ai_llm_calls'::regclass"
    ).fetchone()
    discovery = migrator.execute("SELECT to_regprocedure(%s) IS NOT NULL", [DISCOVERY]).fetchone()
    return found, check, discovery


def test_the_migration_goes_back_to_the_exact_0004_settle_and_refuses_with_orphans(
    both: SimpleNamespace, migrator: DB
) -> None:
    def body(sql: str) -> str:
        return sql.split("$fn$")[1]

    rest = (True, ["search_path=pg_catalog, public, pg_temp"], "crm_migrator", False, True)
    assert _settle_source(migrator) == ((body(MIGRATION.SETTLE), *rest), (True,), (True,))
    try:
        with as_migrator():
            call_command("migrate", "ai_gateway", "0006", verbosity=0)
        assert _settle_source(migrator) == ((body(DAILY.SETTLE), *rest), (False,), (False,))
    finally:
        with as_migrator():
            call_command("migrate", verbosity=0)
    assert _settle_source(migrator)[0][0] == body(MIGRATION.SETTLE)
    held = hold(both, both.a)
    age(migrator, held, MINUTES + 1)
    assert orphans.close()["closed"] == 1
    with as_migrator(), pytest.raises(RuntimeError, match="ORPHANED"):
        call_command("migrate", "ai_gateway", "0006", verbosity=0)
    assert _settle_source(migrator)[1:] == ((True,), (True,))  # nada se deshizo ni se borró
    assert len(migrator.execute(CALLS).fetchall()) == 1
