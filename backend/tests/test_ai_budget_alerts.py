"""F8-05b4 (#957): los avisos del presupuesto de IA. `ai.budget.threshold_reached` cuando una
reserva lleva el mes al 80 % del tope mensual y `ai.budget.exceeded` cuando el tope mensual corta
una reserva, cada uno una sola vez por organización y mes de Lima, con solo el id del tope y el mes.
Solo `ai_budget_reserve` pone las marcas y los eventos (ADR-044, ADR-045). Claves ficticias."""

import threading
import time
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from django.db import DatabaseError, connection

from apps.ai_gateway.llm import Outcome
from core.tenancy.scope import tenant_scope
from tests import test_ai_gateway
from tests.conftest import migrator_conninfo
from tests.test_ai_budget_ledger import RESERVE, refused, scalar
from tests.test_ai_daily_budget import reserve
from tests.test_ai_gateway import DB, budget, code
from tests.test_memberships import ctx

pytestmark = pytest.mark.usefixtures("tenant_db")
world = test_ai_gateway.world  # la fixture de F8-05
THRESHOLD, EXCEEDED = "ai.budget.threshold_reached", "ai.budget.exceeded"
EVENTS = (
    "SELECT e.organization_id, e.event_type, e.aggregate_type, e.aggregate_id = b.id, e.payload"
    " = jsonb_build_object('month', ai_budget_month(now())), e.correlation_id FROM outbox_events e"
    " LEFT JOIN ai_budgets b ON b.organization_id = e.organization_id"
    " WHERE e.event_type LIKE 'ai.budget.%%' ORDER BY e.occurred_at, e.event_type"
)
MARKS = (
    "SELECT month = ai_budget_month(now()), threshold_alerted_at IS NOT NULL, exceeded_alerted_at"
    " IS NOT NULL FROM ai_budget_usage WHERE organization_id = %s ORDER BY month"
)
MONTH_ROW = (  # una fila del mes sembrada por el dueño: lo gastado y las dos marcas
    "INSERT INTO ai_budget_usage (organization_id, id, month, spent_usd, reserved_usd,"
    " threshold_alerted_at, exceeded_alerted_at) VALUES (%s, uuidv7(),"
    " (ai_budget_month(now()) - %s * interval '1 month')::date, %s, 0, %s, %s)"
)


def events(migrator: DB) -> list[Any]:
    return migrator.execute(EVENTS).fetchall()


def marks(migrator: DB, org: Any) -> list[Any]:
    return migrator.execute(MARKS, [org]).fetchall()


def alert(org: Any, kind: str) -> tuple[Any, ...]:
    return (org, kind, "ai_budget", True, True, None)  # el id del tope y el mes, nada más


def b_in_the_same_month(world: SimpleNamespace, migrator: DB) -> None:
    """B: un tope pequeño y su fila del mes muy por encima del 80 % del tope de A, sin marcas. Un
    filtro de organización ausente la marcaría, o compararía el gasto de A con el tope de B."""
    budget(migrator, world.b, "1")
    migrator.execute(MONTH_ROW, [world.b, 0, "50", None, None])


def test_80_percent_alerts_once_with_the_cap_id_and_month_only(
    world: SimpleNamespace, migrator: DB
) -> None:
    budget(migrator, world.a, "10")
    b_in_the_same_month(world, migrator)
    reserve(world, "7.999999")  # por debajo del 80 % de A (y por encima del tope de B)
    assert events(migrator) == [] and marks(migrator, world.a) == [(True, False, False)]
    reserve(world, "0.000001")  # justo el 80 %
    reserve(world, "1")  # vuelve a pasar: no repite
    assert events(migrator) == [alert(world.a, THRESHOLD)]
    assert marks(migrator, world.a) == [(True, True, False)]
    assert marks(migrator, world.b) == [(True, False, False)]  # B no se toca


def test_a_monthly_cut_alerts_once_reserves_nothing_and_never_calls(
    world: SimpleNamespace, migrator: DB
) -> None:
    budget(migrator, world.a, "0.000001")  # menos que una llamada
    b_in_the_same_month(world, migrator)
    assert code(world) == Outcome.AI_BUDGET_EXHAUSTED  # el gateway confirma el aviso
    assert events(migrator) == [alert(world.a, EXCEEDED)]  # sin pasar por el 80 %
    with tenant_scope(ctx(world.a, world.ana)):
        assert scalar(RESERVE, ["0.000002", "SUGGEST"]) is None  # cortada: sin reserva
    assert code(world) == Outcome.AI_BUDGET_EXHAUSTED
    assert world.fake.calls == [] and events(migrator) == [alert(world.a, EXCEEDED)]
    usage = "SELECT spent_usd + reserved_usd FROM ai_budget_usage WHERE organization_id = %s"
    assert migrator.execute(usage, [world.a]).fetchall() == [(0,)]
    assert marks(migrator, world.a) == [(True, False, True)]
    assert marks(migrator, world.b) == [(True, False, False)]


def test_a_daily_cut_still_fails_and_undoes_the_80_percent_alert(
    world: SimpleNamespace, migrator: DB
) -> None:
    budget(migrator, world.a, "10", daily="1")
    with tenant_scope(ctx(world.a, world.ana)):
        assert refused(RESERVE, ["8", "SUGGEST"]) == "ai_budget_exhausted"  # 80 % del mes
    assert events(migrator) == [] and marks(migrator, world.a) == []  # nada queda


def test_a_new_lima_month_alerts_again(world: SimpleNamespace, migrator: DB) -> None:
    budget(migrator, world.a, "1")
    migrator.execute(MONTH_ROW, [world.a, 1, "1", "2026-01-01", "2026-01-01"])  # avisado
    migrator.execute(MONTH_ROW, [world.a, 2, "1", None, None])  # sin avisos, y no se avisa
    reserve(world, "0.8")
    with tenant_scope(ctx(world.a, world.ana)):
        assert scalar(RESERVE, ["0.5", "SUGGEST"]) is None
    assert events(migrator) == [alert(world.a, THRESHOLD), alert(world.a, EXCEEDED)]
    passed = [(False, False, False), (False, True, True)]  # el de hace dos meses, sin avisar
    assert marks(migrator, world.a) == [*passed, (True, True, True)]


@pytest.mark.parametrize(("amount", "kind"), [("0.3", THRESHOLD), ("3", EXCEEDED)])
def test_concurrent_reserves_alert_once(
    world: SimpleNamespace, migrator: DB, amount: str, kind: str
) -> None:
    """Las reservas esperan juntas a la fila del mes; al soltarla, una sola deja el aviso."""
    budget(migrator, world.a, "10")
    reserve(world, "7.5")
    results: list[Any] = []

    def run() -> None:
        try:
            results.append(reserve(world, amount))
        except DatabaseError as error:  # pragma: no cover — se ve en el assert
            results.append(error)
        finally:
            connection.close()

    with psycopg.connect(migrator_conninfo()) as holder:
        holder.execute("SELECT 1 FROM ai_budget_usage WHERE organization_id = %s FOR UPDATE",
                       [world.a])  # fmt: skip
        threads = [threading.Thread(target=run) for _ in range(4)]
        for thread in threads:
            thread.start()
        waiting = "SELECT count(*) FROM pg_locks WHERE NOT granted"
        for _ in range(200):
            if migrator.execute(waiting).fetchone() == (4,):
                break
            time.sleep(0.05)
        assert migrator.execute(waiting).fetchone() == (4,)  # las cuatro, a la vez
        holder.rollback()
    for thread in threads:
        thread.join(timeout=30)
    assert len(results) == 4 and not any(isinstance(r, Exception) for r in results)
    assert all((r is None) == (kind == EXCEEDED) for r in results)  # cortadas todas o ninguna
    assert events(migrator) == [alert(world.a, kind)]


DENIED = (  # `crm_app`, en el scope de A: las marcas solo las pone la reserva
    "UPDATE ai_budget_usage SET threshold_alerted_at = now()",
    "UPDATE ai_budget_usage SET exceeded_alerted_at = NULL",
    "INSERT INTO ai_budget_usage (organization_id, id, month, spent_usd, reserved_usd,"
    " threshold_alerted_at, exceeded_alerted_at) SELECT organization_id, uuidv7(), month + 31, 0,"
    " 0, now(), now() FROM ai_budget_usage",
    "DELETE FROM ai_budget_usage",
)

REWRITE = (  # el dueño: una marca puesta no vuelve a NULL ni cambia
    "UPDATE ai_budget_usage SET threshold_alerted_at = NULL",
    "UPDATE ai_budget_usage SET exceeded_alerted_at = NULL",
    "UPDATE ai_budget_usage SET threshold_alerted_at = threshold_alerted_at + interval '1 day'",
    "UPDATE ai_budget_usage SET exceeded_alerted_at = now() + interval '1 day'",
)


def test_crm_app_cannot_write_the_marks_and_the_owner_cannot_rewrite_them(
    world: SimpleNamespace, migrator: DB
) -> None:
    budget(migrator, world.a, "1")
    with tenant_scope(ctx(world.a, world.ana)):
        assert scalar(RESERVE, ["0.9", "SUGGEST"]) is not None
        assert scalar(RESERVE, ["0.2", "SUGGEST"]) is None
    for statement in DENIED:
        with tenant_scope(ctx(world.a)), pytest.raises(DatabaseError, match="permission denied"):
            connection.cursor().execute(statement)
    for statement in REWRITE:
        with pytest.raises(psycopg.errors.IntegrityError) as caught:
            migrator.execute(statement)
            assert caught.value.diag.constraint_name == "ai_budget_usage_alerted_once"
    assert marks(migrator, world.a) == [(True, True, True)]
