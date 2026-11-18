"""F8-05b2 (#949): el tope diario, con el corte duro del mensual. Sin tope diario no hay llamadas;
`ai_budget_reserve` reserva en el mes y después en el día de Lima, y `ai_budget_settle` ajusta en
ese orden el día de la reserva; cada sentencia filtra por la organización de la sesión (ADR-044) y
`crm_app` solo lee las filas del día. Claves ficticias."""

import threading
import time
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from django.db import DatabaseError, connection

from apps.ai_gateway.llm import Outcome
from core.tenancy.scope import tenant_scope
from tests import test_ai_gateway
from tests.conftest import migrator_conninfo
from tests.test_ai_budget_ledger import RESERVE, SETTLE, refused, scalar
from tests.test_ai_gateway import DB, budget, call, code, ledger
from tests.test_memberships import ctx

pytestmark = pytest.mark.usefixtures("tenant_db")
world = test_ai_gateway.world  # la fixture de F8-05
DAYS = "SELECT day = ai_budget_day(now()), spent_usd, reserved_usd FROM ai_budget_daily_usage"
MONTH = "SELECT spent_usd, reserved_usd FROM ai_budget_usage WHERE organization_id = %s"
DAY_ROW = (
    "INSERT INTO ai_budget_daily_usage VALUES (%s, uuidv7(), ai_budget_day(now()) - %s, %s, %s)"
)


def days(migrator: DB, org: Any) -> list[Any]:
    return migrator.execute(f"{DAYS} WHERE organization_id = %s ORDER BY day", [org]).fetchall()


def reserve(w: SimpleNamespace, amount: str) -> Any:
    with tenant_scope(ctx(w.a, w.ana)):
        return scalar(RESERVE, [amount, "SUGGEST"])


def test_without_a_daily_cap_nothing_is_reserved_or_called(
    world: SimpleNamespace, migrator: DB
) -> None:
    budget(migrator, world.b, "100")  # el tope diario de otra organización no vale para A
    migrator.execute("INSERT INTO ai_budgets VALUES (%s, uuidv7(), 100, NULL)", [world.a])
    assert code(world) == Outcome.AI_BUDGET_NOT_SET  # solo el mensual: falla cerrado
    with tenant_scope(ctx(world.a, world.ana)):
        assert refused(RESERVE, ["0.1", "SUGGEST"]) == "ai_budget_not_set"
    assert world.fake.calls == [] and ledger(migrator) == ([], []) and days(migrator, world.a) == []


def test_the_daily_cap_cuts_and_undoes_the_month(world: SimpleNamespace, migrator: DB) -> None:
    budget(migrator, world.a, "100", daily="0.01")  # menos que una llamada
    budget(migrator, world.b, "100")  # el de otra organización no le da margen
    assert code(world) == Outcome.AI_BUDGET_EXHAUSTED
    assert world.fake.calls == [] and ledger(migrator) == ([], []) and days(migrator, world.a) == []


def test_the_day_counts_spent_and_reserved(world: SimpleNamespace, migrator: DB) -> None:
    budget(migrator, world.a, "100", daily="1")
    held = reserve(world, "0.6")
    with tenant_scope(ctx(world.a, world.ana)):
        assert refused(RESERVE, ["0.6", "SUGGEST"]) == "ai_budget_exhausted"
    with tenant_scope(ctx(world.a)):
        assert scalar(SETTLE, [held, "OK", "0.5", 1, 1, ""]) == "SETTLED"
    reserve(world, "0.5")  # 0.5 gastado + 0.5: justo el tope
    with tenant_scope(ctx(world.a, world.ana)):
        assert refused(RESERVE, ["0.000001", "SUGGEST"]) == "ai_budget_exhausted"
    assert days(migrator, world.a) == [(True, Decimal("0.5"), Decimal("0.5"))]
    assert migrator.execute(MONTH, [world.a]).fetchall() == [(Decimal("0.5"), Decimal("0.5"))]


def test_only_the_own_day_rows_change(world: SimpleNamespace, migrator: DB) -> None:
    budget(migrator, world.a, "1")
    budget(migrator, world.b, "1")
    migrator.execute(DAY_ROW, [world.b, 0, "0.2", "0.1"])
    call(world)
    assert days(migrator, world.b) == [(True, Decimal("0.2"), Decimal("0.1"))]
    [(spent, reserved)] = migrator.execute(MONTH, [world.a]).fetchall()
    assert days(migrator, world.a) == [(True, spent, reserved)] and reserved == 0 < spent


def test_the_day_is_lima_and_yesterday_does_not_count(world: SimpleNamespace, migrator: DB) -> None:
    day = "SELECT ai_budget_day(%s::timestamptz)"
    for at, expected in (("2026-11-19 04:59:59+00", "2026-11-18"),  # 23:59:59 en Lima
                         ("2026-11-19 05:00:00+00", "2026-11-19"),
                         ("2026-12-31 23:59:59-05", "2026-12-31")):  # fmt: skip
        assert str(migrator.execute(day, [at]).fetchone()[0]) == expected  # type: ignore[index]
    [(body,)] = migrator.execute("SELECT prosrc FROM pg_proc WHERE proname = 'ai_budget_reserve'")
    assert "d date := public.ai_budget_day(now());" in body
    budget(migrator, world.a, "100", daily="1")
    migrator.execute(DAY_ROW, [world.a, 1, 1, 0])
    held = reserve(world, "1")  # ayer se gastó el tope entero: hoy empieza de cero
    stored = "SELECT day = ai_budget_day(now()) FROM ai_budget_reservations WHERE id = %s"
    assert migrator.execute(stored, [held]).fetchone() == (True,)


def test_a_reservation_settles_on_its_own_day(world: SimpleNamespace, migrator: DB) -> None:
    budget(migrator, world.a, "100", daily="1")
    held = reserve(world, "0.5")
    migrator.execute(
        DAY_ROW, [world.a, 1, 0, "0.5"]
    )  # reservada ayer, ajustada pasada la medianoche
    for moved in (
        "UPDATE ai_budget_reservations SET day = day - 1 WHERE organization_id = %s",
        "UPDATE ai_budget_daily_usage SET reserved_usd = 0 WHERE organization_id = %s"
        " AND day = ai_budget_day(now())",
    ):
        migrator.execute(moved, [world.a])
    with tenant_scope(ctx(world.a)):
        assert scalar(SETTLE, [held, "PROVIDER_UNCERTAIN", None, None, None, ""]) == "SETTLED"
    assert days(migrator, world.a) == [(False, Decimal("0.5"), 0), (True, 0, 0)]
    assert migrator.execute(MONTH, [world.a]).fetchall() == [(Decimal("0.5"), 0)]


def test_concurrent_reserves_never_pass_the_daily_cap(world: SimpleNamespace, migrator: DB) -> None:
    budget(migrator, world.a, "100", daily="1.5")
    together, results = threading.Barrier(6), list[Any]()

    def run() -> None:
        try:
            together.wait(timeout=10)
            with tenant_scope(ctx(world.a, world.ana)):
                results.append(scalar(RESERVE, ["0.5", "SUGGEST"]) is not None)
        except DatabaseError as error:
            results.append(getattr(error.__cause__, "diag").constraint_name)  # noqa: B009
        finally:
            connection.close()

    threads = [threading.Thread(target=run) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert sorted(map(str, results)) == ["True"] * 3 + ["ai_budget_exhausted"] * 3
    assert days(migrator, world.a) == [(True, 0, Decimal("1.5"))]
    assert migrator.execute(MONTH, [world.a]).fetchall() == [(0, Decimal("1.5"))]


@pytest.mark.parametrize("step", ["reserve", "settle"])
def test_the_month_row_is_locked_before_the_day_row(
    world: SimpleNamespace, migrator: DB, step: str
) -> None:
    """Con la fila del mes bloqueada, la función espera sin haber tocado la del día: mes y después
    día, siempre en ese orden, así que una reserva y un ajuste no se interbloquean."""
    budget(migrator, world.a, "100")
    held, done = reserve(world, "0.1"), list[Any]()

    def run() -> None:
        try:
            if step == "reserve":
                done.append(reserve(world, "0.1"))
            else:
                with tenant_scope(ctx(world.a)):
                    done.append(scalar(SETTLE, [held, "PROVIDER_UNCERTAIN", None, None, None, ""]))
        finally:
            connection.close()

    with psycopg.connect(migrator_conninfo()) as holder:
        holder.execute("SELECT 1 FROM ai_budget_usage WHERE organization_id = %s FOR UPDATE",
                       [world.a])  # fmt: skip
        other = threading.Thread(target=run)
        other.start()
        try:
            waiting = "SELECT count(*) FROM pg_locks WHERE NOT granted"
            for _ in range(200):
                if migrator.execute(waiting).fetchone() != (0,):
                    break
                time.sleep(0.05)
            assert migrator.execute(waiting).fetchone() != (0,)  # la otra espera al mes
            free = holder.execute("SELECT 1 FROM ai_budget_daily_usage WHERE organization_id = %s"
                                  " FOR UPDATE NOWAIT", [world.a])  # fmt: skip
            assert free.fetchall() == [(1,)]  # y no tiene la del día
        finally:
            holder.rollback()
            other.join(timeout=20)
    assert len(done) == 1 and done[0] is not None


DENIED = (  # `crm_app`, en el scope de A: el día solo cambia por las funciones
    "INSERT INTO ai_budget_daily_usage SELECT organization_id, uuidv7(), day + 1, 0, 0"
    " FROM ai_budget_daily_usage",
    "UPDATE ai_budget_daily_usage SET reserved_usd = 0",
    "UPDATE ai_budget_daily_usage SET spent_usd = spent_usd + 1",
    "DELETE FROM ai_budget_daily_usage",
    "TRUNCATE ai_budget_daily_usage",
    "UPDATE ai_budgets SET daily_usd = 1000",
    "UPDATE ai_budget_reservations SET day = day",
    "SELECT public.ai_budget_day(now())",
)
GUARDED = {  # siguen para el dueño de las funciones -> la restricción que lo frena
    "UPDATE ai_budget_daily_usage SET reserved_usd = -1": "ai_budget_daily_usage_ck",
    "UPDATE ai_budget_daily_usage SET spent_usd = 0": "ai_budget_usage_spent_only_grows",
    "UPDATE ai_budget_daily_usage SET spent_usd = 'NaN'": "ai_budget_daily_usage_not_nan",
    "UPDATE ai_budgets SET daily_usd = 'NaN'": "ai_budgets_daily_not_nan",
    "UPDATE ai_budgets SET daily_usd = 0": "ai_budgets_daily_ck",
    "INSERT INTO ai_budget_daily_usage SELECT organization_id, uuidv7(), day, 0, 0"
    " FROM ai_budget_daily_usage":
    "ai_budget_daily_usage_day_uq",
    "INSERT INTO ai_budget_reservations SELECT organization_id, uuidv7(), month, 1, now(), NULL,"
    " purpose, requested_by_user_id, provider, model, NULL, day - 1 FROM ai_budget_reservations":
    "ai_budget_reservations_day_fk",  # una reserva sin su fila del día
}  # fmt: skip


def test_crm_app_reads_only_its_day_and_writes_it_only_through_the_functions(
    world: SimpleNamespace, migrator: DB
) -> None:
    budget(migrator, world.a, "1")
    call(world)
    for org, seen in ((world.a, 1), (world.b, 0)):
        with tenant_scope(ctx(org)):
            assert scalar("SELECT count(*) FROM ai_budget_daily_usage", []) == seen
    for statement in DENIED:
        with tenant_scope(ctx(world.a)), pytest.raises(DatabaseError, match="permission denied"):
            connection.cursor().execute(statement)


@pytest.mark.parametrize("statement", GUARDED)
def test_the_day_guards_hold_for_the_owner(
    world: SimpleNamespace, migrator: DB, statement: str
) -> None:
    budget(migrator, world.a, "1")
    call(world)
    with pytest.raises(psycopg.errors.IntegrityError) as caught:
        migrator.execute(statement)
    assert caught.value.diag.constraint_name == GUARDED[statement]
