"""F8-05: el gateway de IA con el adaptador de Anthropic contra la API falsa (`tests.ai_sim`, sin
red): reserva, corte duro sin llamar, ajuste único en `ai_llm_calls`, lo dudoso como gastado,
reintentos acotados, concurrencia y RLS, privilegios y guardas con `crm_app`. Claves ficticias."""

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
from django.db import DatabaseError, connection
from django.test import override_settings
from django.utils import timezone

from apps.ai_gateway import gateway, keys, prices
from apps.ai_gateway.gateway import GatewayError
from apps.ai_gateway.llm import LLMRequest, Outcome, Tool, Turn, Usage
from core.tenancy.scope import tenant_scope
from tests import ai_sim
from tests.ai_sim import BODY_MARK, TEST_KEY, FakeAnthropic
from tests.factories import make_user
from tests.test_memberships import ctx, join

pytestmark = pytest.mark.usefixtures("tenant_db")
DB = psycopg.Connection[Any]
PROMPT, USAGE = "texto-del-cliente-que-no-se-registra", {"input_tokens": 1000, "output_tokens": 500}
ASK = LLMRequest("SUGGEST", "Eres un asistente.", (Turn("user", PROMPT),))
BAD = ({"input_tokens": -1, "output_tokens": 1}, {"output_tokens": 1}, {"input_tokens": 1})
THREE = (*ASK.turns, Turn("assistant", "x"), Turn("user", "y"))  # el último, de quien pide
PARAMS = settings.AI_PROVIDER_PARAMS["ANTHROPIC"]
SCHEMA = {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"],
          "additionalProperties": False}  # fmt: skip
SWITCH_ON = "INSERT INTO ai_switches (organization_id, enabled, changed_at) VALUES (%s, %s, now())"
CACHED = {"cache_creation_input_tokens": 300, "cache_read_input_tokens": 1000,  # sin desglose: 1 h
          "cache_creation": {"ephemeral_5m_input_tokens": 100}}  # fmt: skip


@pytest.fixture
def world(orgs: dict[str, UUID], monkeypatch: pytest.MonkeyPatch, migrator: DB) -> SimpleNamespace:
    """A con cuenta de Anthropic; B sin cuenta. Ninguna con tope: sin fila, no hay llamadas. Las
    dos con la IA encendida (F8-06)."""
    for org in (orgs["A"], orgs["B"]):
        migrator.execute(SWITCH_ON, [org, True])
    ana = make_user(email="ana@example.com")
    join(orgs["A"], ana)
    with tenant_scope(ctx(orgs["A"], ana)) as tenant:
        keys.store(tenant, "ANTHROPIC", TEST_KEY)
    sleeps: list[float] = []
    monkeypatch.setattr(time, "sleep", sleeps.append)
    fake = ai_sim.install(monkeypatch, FakeAnthropic(usage=dict(USAGE)))
    return SimpleNamespace(a=orgs["A"], b=orgs["B"], ana=ana, fake=fake, sleeps=sleeps)


def budget(migrator: DB, org: UUID, usd: str, daily: str | None = None) -> None:
    """El tope mensual y el diario (F8-05b2), por omisión el mismo."""
    insert = "INSERT INTO ai_budgets VALUES (%s, uuidv7(), %s, %s)"
    migrator.execute(insert, [org, usd, daily or usd])


def call(w: SimpleNamespace, request: LLMRequest = ASK, org: UUID | None = None) -> Any:
    return gateway.complete(ctx(org or w.a, w.ana), request)


def code(w: SimpleNamespace, org: UUID | None = None) -> Outcome:
    with pytest.raises(GatewayError) as caught:
        call(w, ASK, org)
    assert str(caught.value) == caught.value.outcome.value  # solo el código
    return caught.value.outcome


def ledger(migrator: DB) -> tuple[Any, ...]:
    usage = migrator.execute("SELECT spent_usd, reserved_usd FROM ai_budget_usage").fetchall()
    calls = migrator.execute(  # una fila por reserva, con su llamada si ya se ajustó
        "SELECT outcome, reserved_usd, cost_usd, input_tokens, output_tokens, request_id,"
        " settled_at IS NOT NULL, cache_write_tokens, cache_read_tokens"
        " FROM ai_budget_reservations r LEFT JOIN ai_llm_calls c ON c.reservation_id = r.id"
        " ORDER BY r.created_at"
    ).fetchall()
    return usage, calls


def test_a_call_passes_is_recorded_and_charged_without_logging_content(
    world: SimpleNamespace, migrator: DB, caplog: pytest.LogCaptureFixture
) -> None:
    budget(migrator, world.a, "1")
    world.fake.usage |= CACHED
    tools = (Tool("search_product", "Busca un producto", SCHEMA),)
    with caplog.at_level(logging.DEBUG):
        reply = call(world, LLMRequest("SUMMARY", "s", THREE, tools, SCHEMA))
    assert (reply.text, reply.stop, reply.usage) == (f"respuesta {BODY_MARK}", "end_turn",
                                                    Usage(1000, 500, 100, 200, 1000))  # fmt: skip
    [sent] = world.fake.calls
    assert (sent["method"], sent["path"], sent["headers"]["x-api-key"]) == (
        "POST", "/v1/messages", TEST_KEY)  # fmt: skip
    body = sent["body"]
    assert {k: body[k] for k in ("model", "max_tokens", "tool_choice", "output_config")} == {
        "model": "claude-sonnet-5-5", "max_tokens": 2048, "tool_choice": {"type": "auto"},
        "output_config": {"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA}},
    }  # fmt: skip
    assert body["tools"] == [{"name": "search_product", "description": "Busca un producto",
                              "strict": True, "input_schema": SCHEMA}]  # fmt: skip
    assert body["messages"] == [{"role": "user", "content": PROMPT},
                                {"role": "assistant", "content": "x"},
                                {"role": "user", "content": "y"}]  # fmt: skip
    assert world.fake.timeouts[0].read_timeout > 15  # el modelo tarda más que una API normal
    [(spent, reserved)], [row] = ledger(migrator)
    cost = Decimal("0.00815")  # 1000 × 2 + 100 × 2.5 + 200 × 4 + 1000 × 0.1 + 500 × 10, por millón
    assert row[0] == "OK" and row[2:] == (cost, 1000, 500, ai_sim.REQUEST_ID, True, 300, 1000)
    assert row[1] > row[2] and (spent, reserved) == (cost, 0)
    held = migrator.execute("SELECT id FROM ai_budget_reservations").fetchone()[0]  # type: ignore[index]
    assert (
        gateway.settle(ctx(world.a), held, Outcome.PROVIDER_ERROR, Decimal(0)) is False
    )  # una sola vez
    assert ledger(migrator) == ([(spent, reserved)], [row])
    logged = caplog.text + str([r.__dict__ for r in caplog.records])
    assert not any(s in logged for s in (PROMPT, BODY_MARK, TEST_KEY))
    with tenant_scope(ctx(world.b)), connection.cursor() as cursor:  # B no ve nada de A
        cursor.execute("SELECT (SELECT count(*) FROM ai_llm_calls) + (SELECT count(*) FROM"
                       " ai_budget_usage) + (SELECT count(*) FROM ai_budgets)")  # fmt: skip
        assert cursor.fetchone() == (0,)


def test_at_the_cap_the_next_call_is_refused(world: SimpleNamespace, migrator: DB) -> None:
    assert code(world) == Outcome.AI_BUDGET_NOT_SET  # sin tope (ni fila), nada
    assert world.fake.calls == [] and ledger(migrator) == ([], [])
    budget(migrator, world.a, "0.04")  # cada reserva es de ~0.0253 y cada llamada cuesta 0.007
    call(world)
    call(world)
    world.fake.usage = {"input_tokens": 1000, "output_tokens": 1500}  # esta, 0.017
    call(world)  # 0.014 + 0.0253 cabe; después, 0.031 + 0.0253 ya no
    assert code(world) == Outcome.AI_BUDGET_EXHAUSTED and len(world.fake.calls) == 3
    [(spent, reserved)], calls = ledger(migrator)
    assert (spent, reserved, len(calls)) == (Decimal("0.031"), 0, 3)
    assert code(world, world.b) == Outcome.AI_NOT_CONFIGURED  # B: sin cuenta, nada


def test_two_concurrent_calls_never_pass_the_cap(world: SimpleNamespace, migrator: DB) -> None:
    budget(migrator, world.a, "0.03")  # cabe una reserva, no dos
    together, outcome = threading.Barrier(2), list[Any]()

    def run() -> None:
        try:
            together.wait(timeout=10)
            outcome.append(call(world).stop)
        except GatewayError as error:
            outcome.append(error.outcome)
        finally:
            connection.close()

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
    assert sorted(outcome) == [Outcome.AI_BUDGET_EXHAUSTED, "end_turn"]
    assert len(world.fake.calls) == 1 and len(ledger(migrator)[1]) == 1


ERRORS = {  # del simulador -> (código, llamadas, ¿la reserva cuenta entera?)
    "unauthorized": (Outcome.CREDENTIALS_REJECTED, 1, False),
    "billing": (Outcome.PROVIDER_BILLING, 1, False),
    "tier_limit": (Outcome.PROVIDER_BILLING, 1, False),
    "bad_request": (Outcome.PROVIDER_ERROR, 1, False),
    "not_found": (Outcome.PROVIDER_ERROR, 1, False),
    "teapot": (Outcome.PROVIDER_UNAVAILABLE, 1, False),  # inesperado: sin reintento
    "rate_limit": (Outcome.PROVIDER_UNAVAILABLE, 3, False),
    "server": (Outcome.PROVIDER_UNAVAILABLE, 3, False),
    "overloaded": (Outcome.PROVIDER_UNAVAILABLE, 3, False),
    "unreachable": (Outcome.PROVIDER_UNAVAILABLE, 1, False),  # no salió: se libera
    "timeout": (Outcome.PROVIDER_UNAVAILABLE, 1, True),  # salió: pudo cobrarla
    "cut": (Outcome.PROVIDER_UNAVAILABLE, 1, True),
    "garbage": (Outcome.PROVIDER_UNAVAILABLE, 1, True),
}


@pytest.mark.parametrize("name", ERRORS)
def test_provider_errors_are_normalized_and_settled_once(
    world: SimpleNamespace, migrator: DB, name: str, caplog: pytest.LogCaptureFixture
) -> None:
    budget(migrator, world.a, "1")
    world.fake.outcome = name
    expected, calls, billed = ERRORS[name]
    with caplog.at_level(logging.DEBUG):
        assert code(world) == expected
    assert len(world.fake.calls) == calls and BODY_MARK not in caplog.text
    [(spent, reserved)], [row] = ledger(migrator)
    recorded = Outcome.PROVIDER_UNCERTAIN if billed else expected  # lo dudoso, distinguible
    assert (row[0], row[6], reserved) == (recorded.value, True, 0)
    assert (spent, row[2]) == ((row[1], row[1]) if billed else (0, 0))
    assert row[5] == ("" if name in ("unreachable", "timeout", "cut") else ai_sim.REQUEST_ID)
    if calls == 3:  # `retry-after` del 429 (2 s); sin él, 1 s
        assert world.sleeps == ([2.0, 2.0] if name == "rate_limit" else [1.0, 1.0])


def seconds(total: float) -> Any:
    return override_settings(AI_PROVIDER_PARAMS={"ANTHROPIC": {**PARAMS, "seconds": total}})


def test_retries_stop_at_the_deadline(world: SimpleNamespace, migrator: DB) -> None:
    budget(migrator, world.a, "1")
    world.fake.script = ["overloaded", "ok"]
    assert call(world).stop == "end_turn" and len(world.fake.calls) == 2
    world.fake.script = ["rate_limit"]  # pide 2 s y el plazo total es de 2
    with seconds(2):
        assert code(world) == Outcome.PROVIDER_UNAVAILABLE
    assert len(world.fake.calls) == 3 and world.sleeps == [1.0]
    world.fake.script, world.fake.retry_after = ["rate_limit", "ok"], "²"  # no es un número
    assert call(world).stop == "end_turn" and world.sleeps == [1.0, 1.0]
    for usage in BAD:  # usos imposibles: cuentan enteros
        world.fake.usage = usage
        assert code(world) == Outcome.PROVIDER_UNAVAILABLE
        assert ledger(migrator)[1][-1][1:3] == (ledger(migrator)[1][-1][1],) * 2  # cuenta entera
    world.fake.usage = USAGE
    for total in (500, 0.2):  # la espera de lectura, entre 1 y 120 s
        with seconds(total):
            call(world)
    assert [t.read_timeout for t in world.fake.timeouts[-2:]] == [120.0, 1.0]


@pytest.mark.parametrize(("left", "sent"), [(-5, None), (0.5, None), (0.999, None), (1, 1.0),
                                            (30, 30.0), (100, 60.0)])  # fmt: skip
def test_the_deadline_bounds_the_send_and_under_a_second_left_releases_without_sending(
    world: SimpleNamespace, migrator: DB, monkeypatch: pytest.MonkeyPatch,
    left: float, sent: float | None,
) -> None:  # fmt: skip
    """F8-08g2: lo que queda se mide al enviar y nunca pasa de `seconds` (60 s); con menos de 1 s,
    no sale y la reserva se libera (`PROVIDER_UNAVAILABLE`, costo 0), no cuenta como gastada."""
    budget(migrator, world.a, "1")
    monkeypatch.setattr(time, "monotonic", lambda: 1000.0)
    assert PARAMS["seconds"] == 60
    if sent is not None:
        gateway.complete(ctx(world.a, world.ana), ASK, deadline=1000 + left)
        assert [t.read_timeout for t in world.fake.timeouts] == [sent]
        return
    with pytest.raises(GatewayError) as caught:
        gateway.complete(ctx(world.a, world.ana), ASK, deadline=1000 + left)
    assert (caught.value.outcome, world.fake.calls) == (Outcome.PROVIDER_UNAVAILABLE, [])
    [(spent, reserved)], [row] = ledger(migrator)
    assert (row[0], row[2], row[6], spent, reserved) == ("PROVIDER_UNAVAILABLE", 0, True, 0, 0)
    held = migrator.execute("SELECT id FROM ai_budget_reservations").fetchall()
    assert held == [(caught.value.reservation_id,)]


SETTLES: dict[str, dict[str, Any]] = {  # cada ajuste de `complete` → lo que lo provoca
    "released": {}, "billed": {"outcome": "timeout"}, "unbilled": {"outcome": "bad_request"},
    "unoffered": {"content": [{"type": "tool_use", "id": "toolu_1", "name": "x", "input": {}}]},
    "ok": {}, "unpriced": {"usage": {"input_tokens": 0, "output_tokens": 0}},
}  # fmt: skip


@pytest.mark.parametrize("path", SETTLES)
def test_a_settle_that_fails_raises_internal_error_with_its_reservation(
    world: SimpleNamespace, migrator: DB, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture, path: str,
) -> None:  # fmt: skip
    """F8-08g2: si el ajuste falla (la base), la reserva queda abierta para el barrido y el error
    lleva su id; el log, solo el id y el tipo del error, nunca su mensaje."""
    budget(migrator, world.a, "1")

    def refused(*_: Any) -> None:
        raise DatabaseError("detalle-de-la-fila")

    monkeypatch.setattr(gateway, "settle", refused)
    for name, value in SETTLES[path].items():
        setattr(world.fake, name, value)
    deadline = None
    if path == "released":
        monkeypatch.setattr(time, "monotonic", lambda: 1000.0)
        deadline = 1000.5
    with caplog.at_level(logging.DEBUG), pytest.raises(GatewayError) as caught:
        gateway.complete(ctx(world.a, world.ana), ASK, deadline=deadline)
    [(held,)] = migrator.execute("SELECT id FROM ai_budget_reservations").fetchall()
    assert (caught.value.outcome, caught.value.reservation_id) == (Outcome.INTERNAL_ERROR, held)
    assert len(world.fake.calls) == (0 if path == "released" else 1)
    assert ledger(migrator)[1][0][6] is False  # abierta: el barrido la cierra como gastada
    failed = [r.msg for r in caplog.records if isinstance(r.msg, dict)
              and r.msg.get("event") == "ai.settle_failed"]  # fmt: skip
    assert [(e["reservation_id"], e["error"]) for e in failed] == [(str(held), "DatabaseError")]
    assert "detalle-de-la-fila" not in caplog.text + str([r.__dict__ for r in caplog.records])


def test_other_months_and_odd_request_ids(world: SimpleNamespace, migrator: DB) -> None:
    budget(migrator, world.a, "1")
    usage = "INSERT INTO ai_budget_usage VALUES (%s, uuidv7(), '2001-01-01', 0.9, 0.05)"
    migrator.execute(usage, [world.a])  # un mes anterior: ni su tope ni su ajuste
    for rid in ("r" * 80, "\x07id"):
        world.fake.request_id = rid
        call(world)
    assert [row[5] for row in ledger(migrator)[1]] == ["r" * 64, ""]
    rows = migrator.execute("SELECT spent_usd, reserved_usd FROM ai_budget_usage ORDER BY month")
    assert rows.fetchall() == [(Decimal("0.9"), Decimal("0.05")), (Decimal("0.014"), 0)]


def test_no_price_or_key_reserves_nothing(world: SimpleNamespace, migrator: DB) -> None:
    budget(migrator, world.a, "1")
    with override_settings(AI_MODELS={"ANTHROPIC": {}}):
        assert code(world) == Outcome.AI_NOT_CONFIGURED
    with tenant_scope(ctx(world.a)) as tenant:
        keys.revoke(tenant, "ANTHROPIC")
    assert code(world) == Outcome.AI_NOT_CONFIGURED
    assert world.fake.calls == [] and ledger(migrator) == ([], [])
    with tenant_scope(ctx(world.a, world.ana)), pytest.raises(RuntimeError):  # ni en un scope
        call(world)


def test_a_cap_equal_to_one_reservation(world: SimpleNamespace, migrator: DB) -> None:
    body = gateway.ADAPTERS["ANTHROPIC"].encode(ASK, PARAMS)  # más 1000 tokens por las tools
    amount = prices.ceiling("ANTHROPIC", PARAMS["model"], len(body) + 1000, PARAMS["max_tokens"])
    budget(migrator, world.a, str(amount))
    call(world)
    assert ledger(migrator)[1][0][1] == amount and code(world) == Outcome.AI_BUDGET_EXHAUSTED


CALL = ("INSERT INTO ai_llm_calls (organization_id, reservation_id, provider, model, purpose,"
        " reserved_usd, cost_usd, outcome, created_at)"
        " VALUES (%s, {}, 'ANTHROPIC', 'm', '{}', {}, 0, 'OK', {})")  # fmt: skip
HELD = "(SELECT id FROM ai_budget_reservations)"
PART = f"ai_llm_calls_y{timezone.now():%Y}m{timezone.now():%m}"  # la partición de este mes (UTC)
NEXT = "now() + interval '1 month'"
LATE = ("INSERT INTO ai_budget_reservations SELECT organization_id, uuidv7(), month, 1, now(),"
        " NULL, {}, {}, provider, model, NULL, day FROM ai_budget_reservations")  # fmt: skip
DENIED = (  # F8-05b1: `crm_app`, en el scope de A, solo cambia el registro por las funciones
    "UPDATE ai_budget_usage SET reserved_usd = 0",  # no libera lo reservado
    "UPDATE ai_budget_usage SET spent_usd = spent_usd + 1",
    "INSERT INTO ai_budget_usage SELECT organization_id, uuidv7(), '2999-01-01', 0, 0"
    " FROM ai_budget_usage",  # ni abre un mes a su gusto
    "UPDATE ai_budget_reservations SET settled_at = now()",
    "INSERT INTO ai_budget_reservations SELECT organization_id, uuidv7(), month, 1, now(), NULL,"
    " purpose, requested_by_user_id, provider, model FROM ai_budget_reservations",
    CALL.format(HELD, "SUGGEST", 1, "now()"),  # una segunda fila para una reserva ajustada
    CALL.format(HELD, "SUGGEST", 1, NEXT),  # una de un mes futuro
    f"INSERT INTO {PART} SELECT * FROM {PART}",  # noqa: S608 — ni por la partición
    "INSERT INTO ai_budgets (id, organization_id, monthly_usd) VALUES (uuidv7(), %s, 1000)",
    "DELETE FROM ai_budgets",  # sus topes, solo por la ruta (F8-05b3); nunca los borra
    "UPDATE ai_llm_calls SET cost_usd = 0",  # de solo inserción
    "UPDATE ai_budget_reservations SET amount_usd = 1",
    "UPDATE ai_budget_usage SET month = month",
    "DELETE FROM ai_llm_calls",  # ni borra lo gastado
    f"DELETE FROM {PART}",  # noqa: S608
    "DELETE FROM ai_budget_reservations",
    "DELETE FROM ai_budget_usage",
    "SELECT public.ai_llm_calls_ensure_partitions(12)",  # ni DDL (el mutante S09 de #933)
    "SELECT public.ai_budget_month(now())",
)
GUARDED = {  # las guardas siguen para el dueño de las funciones -> la restricción que lo frena
    "UPDATE ai_budget_usage SET reserved_usd = -1": "ai_budget_usage_ck",
    "UPDATE ai_budget_usage SET spent_usd = 0": "ai_budget_usage_spent_only_grows",
    "UPDATE ai_budget_usage SET spent_usd = 'NaN'": "ai_budget_usage_not_nan",
    "INSERT INTO ai_budget_usage SELECT * FROM ai_budget_usage ON CONFLICT (organization_id, month)"
    " DO UPDATE SET spent_usd = 0": "ai_budget_usage_spent_only_grows",
    "UPDATE ai_budget_reservations SET settled_at = now()": "ai_budget_reservations_settled_once",
    "INSERT INTO ai_budget_reservations SELECT organization_id, uuidv7(), month, 1, now(), now(),"
    " purpose, requested_by_user_id, provider, model FROM ai_budget_reservations":
    "ai_budget_reservations_settled_once",  # ni nacer ajustada
    CALL.format(HELD, "SUGGEST", 0, "now()"): "ai_llm_calls_reserved_usd_check",
    CALL.format(HELD, "OTHER", 1, "now()"): "ai_llm_calls_purpose_check",
    CALL.format("uuidv7()", "SUGGEST", 1, "now()"): "ai_llm_calls_reservation_fk",
    LATE.format("'OTHER'", "requested_by_user_id"): "ai_budget_reservations_purpose_ck",
    LATE.format("purpose", "uuidv7()"): "ai_budget_reservations_requested_by_fk",  # no miembro
}  # fmt: skip


@pytest.mark.parametrize("statement", DENIED)
def test_crm_app_cannot_write_the_ledger_but_through_the_functions(
    world: SimpleNamespace, migrator: DB, statement: str
) -> None:
    budget(migrator, world.a, "1")
    call(world)
    with tenant_scope(ctx(world.a)), pytest.raises(DatabaseError, match="permission denied"):
        connection.cursor().execute(statement, [world.a] if "%s" in statement else None)
    with tenant_scope(ctx(world.b)), pytest.raises(DatabaseError, match="permission denied"):
        connection.cursor().execute(CALL.format("uuidv7()", "SUGGEST", 1, "now()"), [world.a])


@pytest.mark.parametrize("statement", GUARDED)
def test_the_guards_still_hold_for_the_owner(
    world: SimpleNamespace, migrator: DB, statement: str
) -> None:
    budget(migrator, world.a, "1")
    call(world)
    with pytest.raises(psycopg.errors.IntegrityError) as caught:
        migrator.execute(statement, [world.a] if "%s" in statement else None)
    assert caught.value.diag.constraint_name == GUARDED[statement]


TIER = {"above": 100, "input": "3", "cache_write_5m": "3.75", "cache_write_1h": "6",
        "cache_read": "0.3", "output": "15"}  # fmt: skip
LONG = {"input": "1", "cache_write_5m": "1.25", "cache_write_1h": "2", "cache_read": "0.1",
        "output": "5", "long": TIER}  # fmt: skip


@override_settings(AI_MODELS={"ANTHROPIC": {"m": LONG}})
def test_prices_cover_every_category_the_long_tier_and_round_up() -> None:
    assert prices.ceiling("ANTHROPIC", "m", 1_000_000, 1_000_000) == Decimal(6 + 15)
    assert prices.ceiling("ANTHROPIC", "m", 1, 0) == Decimal("0.000006")
    assert prices.cost("ANTHROPIC", "m", Usage(10, 10, 20, 30, 40)) == Decimal("0.000149")  # 100
    assert prices.cost("ANTHROPIC", "m", Usage(1, 1, 0, 0, 100)) == Decimal("0.000048")  # 101
    assert prices.cost("ANTHROPIC", "m", Usage(0, 0, 0, 0, 1)) == prices.STEP  # hacia arriba
    with pytest.raises(prices.Unpriced):
        prices.cost("ANTHROPIC", "otro", Usage(1, 1))
