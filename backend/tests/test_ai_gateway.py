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

from apps.ai_gateway import gateway, keys, prices
from apps.ai_gateway.gateway import GatewayError
from apps.ai_gateway.llm import LLMRequest, Outcome, Tool, ToolCall, ToolResult, Turn, Usage
from core.tenancy.scope import tenant_scope
from tests import ai_sim
from tests.ai_sim import BODY_MARK, TEST_KEY, FakeAnthropic
from tests.factories import make_user
from tests.test_memberships import ctx, join

pytestmark = pytest.mark.usefixtures("tenant_db")
DB = psycopg.Connection[Any]
PROMPT, USAGE = "texto-del-cliente-que-no-se-registra", {"input_tokens": 1000, "output_tokens": 500}
ASK = LLMRequest("SUGGEST", "Eres un asistente.", (Turn("user", PROMPT),))
SCHEMA = {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"],
          "additionalProperties": False}  # fmt: skip
CACHED = {"cache_creation_input_tokens": 300, "cache_read_input_tokens": 1000,  # sin desglose: 1 h
          "cache_creation": {"ephemeral_5m_input_tokens": 100}}  # fmt: skip
LOOP = (Turn("assistant", raw=({"type": "thinking", "thinking": ""},)),
        Turn("user", tool_results=(ToolResult("t1", "ok"),)),
        Turn("assistant", "x", tool_calls=(ToolCall("t2", "f", {"q": "a"}),)))  # fmt: skip


@pytest.fixture
def world(orgs: dict[str, UUID], monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """A con cuenta de Anthropic; B sin cuenta. Ninguna con tope: sin fila, no hay llamadas."""
    ana = make_user(email="ana@example.com")
    join(orgs["A"], ana)
    with tenant_scope(ctx(orgs["A"], ana)) as tenant:
        keys.store(tenant, "ANTHROPIC", TEST_KEY)
    sleeps: list[float] = []
    monkeypatch.setattr(time, "sleep", sleeps.append)
    fake = ai_sim.install(monkeypatch, FakeAnthropic(usage=dict(USAGE)))
    return SimpleNamespace(a=orgs["A"], b=orgs["B"], ana=ana, fake=fake, sleeps=sleeps)


def budget(migrator: DB, org: UUID, usd: str) -> None:
    insert = "INSERT INTO ai_budgets (id, organization_id, monthly_usd) VALUES (uuidv7(), %s, %s)"
    migrator.execute(insert, [org, usd])


def call(w: SimpleNamespace, request: LLMRequest = ASK, org: UUID | None = None) -> Any:
    return gateway.complete(ctx(org or w.a, w.ana), request)


def code(w: SimpleNamespace, org: UUID | None = None) -> Outcome:
    with pytest.raises(GatewayError) as caught:
        call(w, ASK, org)
    assert str(caught.value) == caught.value.outcome.value  # solo el código
    return caught.value.outcome


def ledger(migrator: DB) -> tuple[Any, ...]:
    usage = migrator.execute("SELECT spent_usd, reserved_usd FROM ai_budget_usage").fetchall()
    calls = migrator.execute(
        "SELECT outcome, reserved_usd, cost_usd, input_tokens, output_tokens, request_id,"
        " settled_at IS NOT NULL, cache_write_tokens, cache_read_tokens FROM ai_llm_calls"
        " ORDER BY created_at"
    ).fetchall()
    return usage, calls


def test_a_call_passes_is_recorded_and_charged_without_logging_content(
    world: SimpleNamespace, migrator: DB, caplog: pytest.LogCaptureFixture
) -> None:
    budget(migrator, world.a, "1")
    world.fake.usage |= CACHED
    tools = (Tool("search_product", "Busca un producto", SCHEMA),)
    with caplog.at_level(logging.DEBUG):
        reply = call(world, LLMRequest("SUMMARY", "s", (*ASK.turns, *LOOP), tools, SCHEMA))
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
    assert [m["content"] for m in body["messages"]] == [
        [{"type": "text", "text": PROMPT}], [{"type": "thinking", "thinking": ""}],
        [{"type": "tool_result", "tool_use_id": "t1", "content": "ok", "is_error": False}],
        [{"type": "text", "text": "x"},
         {"type": "tool_use", "id": "t2", "name": "f", "input": {"q": "a"}}],
    ]  # fmt: skip
    assert world.fake.timeouts[0].read_timeout > 15  # el modelo tarda más que una API normal
    [(spent, reserved)], [row] = ledger(migrator)
    cost = Decimal("0.00815")  # 1000 × 2 + 100 × 2.5 + 200 × 4 + 1000 × 0.1 + 500 × 10, por millón
    assert row[0] == "OK" and row[2:] == (cost, 1000, 500, ai_sim.REQUEST_ID, True, 300, 1000)
    assert row[1] > row[2] and (spent, reserved) == (cost, 0)
    call_id = migrator.execute("SELECT id FROM ai_llm_calls").fetchone()[0]  # type: ignore[index]
    assert gateway.settle(ctx(world.a), call_id, Outcome.OK, Decimal(0)) is False  # una sola vez
    assert ledger(migrator) == ([(spent, reserved)], [row])
    logged = caplog.text + str([r.__dict__ for r in caplog.records])
    assert not any(s in logged for s in (PROMPT, BODY_MARK, TEST_KEY))
    with tenant_scope(ctx(world.b)), connection.cursor() as cursor:  # B no ve nada de A
        cursor.execute("SELECT (SELECT count(*) FROM ai_llm_calls) + (SELECT count(*) FROM"
                       " ai_budget_usage) + (SELECT count(*) FROM ai_budgets)")  # fmt: skip
        assert cursor.fetchone() == (0,)


def test_at_the_cap_the_next_call_is_refused(world: SimpleNamespace, migrator: DB) -> None:
    assert code(world) == Outcome.AI_BUDGET_EXHAUSTED  # sin tope (ni fila), nada
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
    assert (row[0], row[6], reserved) == (expected.value, True, 0)
    assert (spent, row[2]) == ((row[1], row[1]) if billed else (0, 0))
    if calls == 3:  # `retry-after` del 429 (2 s); sin él, 1 s
        assert world.sleeps == ([2.0, 2.0] if name == "rate_limit" else [1.0, 1.0])


def test_retries_stop_at_the_deadline(world: SimpleNamespace, migrator: DB) -> None:
    budget(migrator, world.a, "1")
    world.fake.script = ["overloaded", "ok"]
    assert call(world).stop == "end_turn" and len(world.fake.calls) == 2
    params = {"ANTHROPIC": {**settings.AI_PROVIDER_PARAMS["ANTHROPIC"], "seconds": 2}}
    world.fake.script = ["rate_limit"]  # pide 2 s y el plazo total es de 2
    with override_settings(AI_PROVIDER_PARAMS=params):
        assert code(world) == Outcome.PROVIDER_UNAVAILABLE
    assert len(world.fake.calls) == 3 and world.sleeps == [1.0]
    world.fake.usage = {"input_tokens": -1, "output_tokens": 1}  # un uso imposible
    assert code(world) == Outcome.PROVIDER_UNAVAILABLE


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
    params = settings.AI_PROVIDER_PARAMS["ANTHROPIC"]
    body = gateway.ADAPTERS["ANTHROPIC"].encode(ASK, params)  # más 1000 tokens por las tools
    amount = prices.ceiling("ANTHROPIC", params["model"], len(body) + 1000, params["max_tokens"])
    budget(migrator, world.a, str(amount))
    call(world)
    assert ledger(migrator)[1][0][1] == amount and code(world) == Outcome.AI_BUDGET_EXHAUSTED


CALL = ("INSERT INTO ai_llm_calls (id, organization_id, month, provider, model, purpose,"
        " reserved_usd, outcome, request_id, created_at) VALUES (uuidv7(), %s, {}, 'ANTHROPIC',"
        " 'm', '{}', {}, '', '', now())")  # fmt: skip
NOW = "(SELECT month FROM ai_budget_usage)"
REFUSED = {  # SQL de `crm_app` en el scope de A -> la restricción o el privilegio que lo frena
    "UPDATE ai_budget_usage SET reserved_usd = -1": "ai_budget_usage_ck",
    CALL.format(NOW, "SUGGEST", 0): "ai_llm_calls_ck",
    CALL.format(NOW, "OTHER", 1): "ai_llm_calls_ck",
    CALL.format("'2001-01-01'", "SUGGEST", 1): "ai_llm_calls_month_fk",
    "UPDATE ai_llm_calls SET outcome = 'OK', cost_usd = 0": "ai_llm_calls_settled_once",
    "INSERT INTO ai_budgets (id, organization_id, monthly_usd)"
    " VALUES (uuidv7(), %s, 1000)": "permission denied",
    "UPDATE ai_budgets SET monthly_usd = 1000": "permission denied",  # no sube su tope
    "UPDATE ai_llm_calls SET reserved_usd = 0.000001": "permission denied",
    "UPDATE ai_budget_usage SET month = month": "permission denied",
    "DELETE FROM ai_llm_calls": "permission denied",  # ni borra lo gastado
    "DELETE FROM ai_budget_usage": "permission denied",
}


@pytest.mark.parametrize("statement", REFUSED)
def test_the_database_guards_the_ledger_from_crm_app(
    world: SimpleNamespace, migrator: DB, statement: str
) -> None:
    budget(migrator, world.a, "1")
    call(world)
    with tenant_scope(ctx(world.a)), pytest.raises(DatabaseError) as caught:
        connection.cursor().execute(statement, [world.a] if "%s" in statement else None)
    cause = caught.value.__cause__
    assert REFUSED[statement] in (cause.diag.constraint_name, str(cause).split(" for ")[0])  # type: ignore[union-attr]
    with tenant_scope(ctx(world.b)), pytest.raises(DatabaseError, match="row-level security"):
        connection.cursor().execute(CALL.format("'2026-11-01'", "SUGGEST", 1), [world.a])


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
