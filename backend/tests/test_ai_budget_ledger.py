"""F8-05b1 (#942): el registro de gasto de IA solo cambia por `ai_budget_reserve` y
`ai_budget_settle` (SECURITY DEFINER), que toman la organización y quien pide de la sesión. Quien
no es miembro activo, un propósito inválido o la falta de partición no reservan ni llaman al
proveedor; la reserva se confirma antes de llamar; un costo mayor que lo reservado se cobra
entero y se avisa; el mes es el de Lima (ADR-043). Claves ficticias."""

import logging
import threading
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from django.core.management import call_command
from django.db import DatabaseError, connection, transaction

from apps.access.selectors import AccessDenied
from apps.ai_gateway import gateway
from apps.ai_gateway.llm import LLMRequest, Outcome
from core.tenancy.scope import tenant_scope
from tests import test_ai_gateway
from tests.conftest import migrator_settings
from tests.factories import make_user
from tests.test_ai_gateway import ASK, DB, PART, budget, call, ledger
from tests.test_memberships import ctx, join

pytestmark = pytest.mark.usefixtures("tenant_db")
world = test_ai_gateway.world  # la fixture de F8-05
RESERVE = "SELECT public.ai_budget_reserve(%s, %s, 'ANTHROPIC', 'm', NULL)"
SETTLE = "SELECT public.ai_budget_settle(%s, %s, %s, NULL, NULL, NULL, NULL, NULL, '')"
DEFINERS = (
    "public.ai_budget_reserve(numeric, text, text, text, uuid)",
    "public.ai_budget_settle(uuid, text, numeric, integer, integer, integer, integer, integer,"
    " text)",
)


def refused(statement: str, args: list[Any]) -> Any:
    with pytest.raises(DatabaseError) as caught:
        connection.cursor().execute(statement, args)
    return getattr(caught.value.__cause__, "diag").constraint_name  # noqa: B009


def scalar(statement: str, args: list[Any]) -> Any:
    with connection.cursor() as cursor:
        cursor.execute(statement, args)
        return cursor.fetchone()[0]


def test_the_functions_are_minimal_definers(migrator: DB) -> None:
    rows = migrator.execute(
        "SELECT p.oid::regprocedure::text, p.prosecdef, pg_get_userbyid(p.proowner),"
        " has_function_privilege('public', p.oid, 'EXECUTE'),"
        " has_function_privilege('crm_app', p.oid, 'EXECUTE') FROM pg_proc p"
        " WHERE p.proname IN ('ai_budget_reserve', 'ai_budget_settle', 'ai_budget_month',"
        " 'ai_llm_calls_ensure_partitions') ORDER BY 1"
    ).fetchall()
    assert [r[1:] for r in rows] == [
        (False, "crm_migrator", False, False),  # el mes: solo dentro de la reserva
        (True, "crm_migrator", False, True),
        (True, "crm_migrator", False, True),
        (False, "crm_migrator", False, False),  # sin DDL para el runtime
    ]


def test_only_an_active_member_reserves(
    world: SimpleNamespace, migrator: DB, monkeypatch: pytest.MonkeyPatch
) -> None:
    budget(migrator, world.a, "1")
    outsider, suspended, gone = (make_user(email=f"{n}@example.com") for n in "osg")
    join(world.b, outsider)
    join(world.a, suspended, status="SUSPENDED")
    join(world.a, gone)
    migrator.execute("UPDATE users SET is_active = false WHERE id = %s", [gone.pk])
    for asker in (None, outsider, suspended, gone):
        with pytest.raises(AccessDenied):
            gateway.complete(ctx(world.a, asker), ASK)
    with pytest.raises(DatabaseError) as caught:  # el propósito, antes de reservar
        gateway.complete(ctx(world.a, world.ana), LLMRequest("OTHER", "s", ASK.turns))
    assert caught.value.__cause__.diag.constraint_name == "ai_budget_reservations_purpose_ck"  # type: ignore[union-attr]
    assert world.fake.calls == [] and ledger(migrator) == ([], [])


def test_raw_calls_take_tenant_and_requester_from_the_session(
    world: SimpleNamespace, migrator: DB
) -> None:
    budget(migrator, world.a, "1")
    with transaction.atomic():  # sin organización en la sesión
        assert refused(RESERVE, [1, "SUGGEST"]) == "ai_budget_tenant_required"
    with transaction.atomic():
        assert refused(SETTLE, [uuid4(), "OK", 0]) == "ai_budget_tenant_required"
    for amount in (0, -1, "NaN", None):
        with tenant_scope(ctx(world.a, world.ana)):
            assert refused(RESERVE, [amount, "SUGGEST"]) == "ai_budget_res_ck"
    with tenant_scope(ctx(world.a, world.ana)):
        held = scalar(RESERVE, ["0.5", "SUMMARY"])
    for outcome, cost in (("PROVIDER_UNCERTAIN", 0), ("OK", None), ("OK", -1), ("OK", "NaN")):
        with tenant_scope(ctx(world.a)):
            assert refused(SETTLE, [held, outcome, cost]) == "ai_budget_settle_cost"
    with tenant_scope(ctx(world.b)):  # la reserva de otra organización: como si no existiera
        assert scalar(SETTLE, [held, "OK", 0]) is None
    with tenant_scope(ctx(world.a)):
        assert scalar(SETTLE, [uuid4(), "OK", 0]) is None
    assert ledger(migrator) == ([(0, Decimal("0.5"))], [(None, None, None, None, None, None,
                                                         False, None, None)])  # fmt: skip
    stored = (
        "SELECT requested_by_user_id, month = ai_budget_month(now()) FROM ai_budget_reservations"
    )
    assert migrator.execute(stored).fetchall() == [(world.ana.pk, True)]
    with tenant_scope(ctx(world.a)):
        assert scalar(SETTLE, [held, "PROVIDER_UNCERTAIN", None]) == "SETTLED"
        assert scalar(SETTLE, [held, "OK", 0]) is None  # una sola vez
    [(spent, reserved)], [row] = ledger(migrator)
    assert (spent, reserved, row[0], row[2]) == (Decimal("0.5"), 0, "PROVIDER_UNCERTAIN", spent)
    who = migrator.execute("SELECT requested_by_user_id, purpose FROM ai_llm_calls").fetchall()
    assert who == [(world.ana.pk, "SUMMARY")]  # de la reserva, no de quien ajusta


def test_a_reservation_touches_only_its_organization(world: SimpleNamespace, migrator: DB) -> None:
    budget(migrator, world.a, "1")
    budget(migrator, world.b, "1")
    other = "INSERT INTO ai_budget_usage VALUES (%s, uuidv7(), ai_budget_month(now()), 0.2, 0.1)"
    migrator.execute(other, [world.b])
    call(world)
    mine = "SELECT spent_usd, reserved_usd FROM ai_budget_usage WHERE organization_id = %s"
    assert migrator.execute(mine, [world.b]).fetchall() == [(Decimal("0.2"), Decimal("0.1"))]


def test_two_concurrent_settles_charge_once(world: SimpleNamespace, migrator: DB) -> None:
    budget(migrator, world.a, "1")
    with tenant_scope(ctx(world.a, world.ana)):
        held = scalar(RESERVE, ["0.5", "SUGGEST"])
    together, results = threading.Barrier(2), list[Any]()

    def run() -> None:
        try:
            together.wait(timeout=10)
            results.append(gateway.settle(ctx(world.a), held, Outcome.OK, Decimal("0.1")))
        finally:
            connection.close()

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
    assert sorted(results) == [False, True]
    [(spent, reserved)], [row] = ledger(migrator)
    assert (spent, reserved, row[2]) == (Decimal("0.1"), 0, Decimal("0.1"))


def test_the_budget_month_is_lima_for_every_organization(migrator: DB) -> None:
    month = "SELECT ai_budget_month(%s::timestamptz)"
    for at, expected in (("2026-12-01 04:59:59+00", "2026-11-01"),  # 23:59:59 del 30 en Lima
                         ("2026-12-01 05:00:00+00", "2026-12-01"),
                         ("2027-01-01 00:00:00-05", "2027-01-01")):  # fmt: skip
        assert str(migrator.execute(month, [at]).fetchone()[0]) == expected  # type: ignore[index]


def test_a_cost_above_the_reservation_is_charged_and_flagged(
    world: SimpleNamespace, migrator: DB, caplog: pytest.LogCaptureFixture
) -> None:
    budget(migrator, world.a, "1")
    world.fake.usage = {"input_tokens": 1_000_000, "output_tokens": 0}  # 2 USD: más que la cota
    with caplog.at_level(logging.WARNING):
        call(world)
    [(spent, reserved)], [row] = ledger(migrator)
    assert (spent, reserved, row[0], row[2]) == (Decimal(2), 0, "OK", Decimal(2)) and row[1] < 1
    [warned] = [r for r in caplog.records if "ai.reservation_exceeded" in str(r.msg)]
    [(held,)] = migrator.execute("SELECT id FROM ai_budget_reservations").fetchall()
    fields = set(warned.msg) - {"event", "level", "logger", "timestamp"}
    assert fields <= {"reservation_id", "request_id", "correlation_id"}  # solo ids
    assert str(held) in str(warned.msg)
    world.fake.usage = {"input_tokens": 1, "output_tokens": 1}
    with pytest.raises(gateway.GatewayError, match="AI_BUDGET_EXHAUSTED"):
        call(world)  # el presupuesto se equivoca hacia arriba


def test_the_reservation_is_committed_before_the_provider_call(
    world: SimpleNamespace, migrator: DB, monkeypatch: pytest.MonkeyPatch
) -> None:
    budget(migrator, world.a, "1")
    adapter, seen = gateway.ADAPTERS["ANTHROPIC"], []
    send = adapter.send

    def spy(*args: Any) -> Any:
        open_ = migrator.execute("SELECT count(*) FROM ai_budget_reservations"
                                 " WHERE settled_at IS NULL").fetchone()  # fmt: skip
        seen.append((connection.in_atomic_block, open_))
        return send(*args)

    monkeypatch.setattr(adapter, "send", spy)
    call(world)
    assert seen == [(False, (1,))]  # otra conexión ya la ve; ninguna transacción abierta


def test_without_a_partition_nothing_is_reserved_or_called(
    world: SimpleNamespace, migrator: DB
) -> None:
    budget(migrator, world.a, "1")
    migrator.execute(f"DROP TABLE {PART}")
    try:
        with pytest.raises(DatabaseError) as caught:
            call(world)
        name = caught.value.__cause__.diag.constraint_name  # type: ignore[union-attr]
        assert name == "ai_llm_calls_partition_missing"
        assert world.fake.calls == [] and ledger(migrator) == ([], [])
    finally:
        migrator.execute("SELECT ai_llm_calls_ensure_partitions(12)")


def test_post_migrate_restores_the_calls_horizon(migrator: DB) -> None:
    last = migrator.execute(
        "SELECT 'ai_llm_calls_y' || to_char(now() AT TIME ZONE 'UTC' + interval '12 months',"
        " 'YYYY\"m\"MM')"
    ).fetchone()[0]  # type: ignore[index]
    migrator.execute(f"DROP TABLE {last}")  # simula un año sin desplegar
    runtime = dict(connection.settings_dict)  # el job de migraciones: migrate como crm_migrator
    connection.close()
    connection.settings_dict.update(migrator_settings())
    try:
        call_command("migrate", verbosity=0)
    finally:
        connection.close()
        connection.settings_dict.clear()
        connection.settings_dict.update(runtime)
    exists = migrator.execute("SELECT to_regclass(%s) IS NOT NULL", [last]).fetchone()
    assert exists == (True,)
