"""F8-05b6 (#965): el modelo de la cuenta del proveedor de IA (ADR-034 §3, §4). `PUT …/{id}/model/`
y `accounts.change_model`: primero `ai.manage`, después el segundo factor reciente; solo un modelo
de la lista permitida de su proveedor (`AI_MODELS`), que nunca se repite en un error; auditoría
`ai.account.model_changed` con el antes y el después; la organización, la de la ruta. Y el gateway:
estima, reserva, llama y cobra con el modelo de la cuenta; uno sin precio falla cerrado antes de
abrir la clave y de reservar. PostgreSQL real con `crm_app`; la API de Anthropic, simulada."""

import logging
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from django.conf import settings as django_settings
from django.core.management import call_command
from django.db import DatabaseError, connection
from django.test import Client

from apps.access.selectors import AccessDenied
from apps.ai_gateway import accounts, gateway, keys, prices
from apps.ai_gateway.api.views import MODEL_REFUSED
from apps.ai_gateway.llm import Outcome, Usage
from core.api.step_up import STEP_UP_REQUIRED
from core.tenancy.scope import tenant_scope
from tests.ai_sim import TEST_KEY
from tests.test_ai_gateway import ASK, USAGE, budget, call, code, ledger, world
from tests.test_ai_provider_accounts import OTHER_KEY, audit, crm, real_stack
from tests.test_ai_provider_accounts_api import BASE, create
from tests.test_ai_provider_test_api import COUNTED
from tests.test_authorization import give
from tests.test_channel_accounts_api import signed
from tests.test_conversations_api import UNAUTHENTICATED
from tests.test_member_branch import DENIED
from tests.test_memberships import ctx
from tests.test_pipelines_tables import as_migrator
from tests.test_self_context import NOT_FOUND, reply
from tests.test_self_context import signed as plain
from tests.test_team_writes import send

__all__ = ["crm", "real_stack", "world"]  # los fixtures, para pytest
pytestmark = pytest.mark.usefixtures("tenant_db")
DB = psycopg.Connection[Any]
B_BASE = "/api/v1/o/org-b/ai/provider-accounts/"
SONNET, HAIKU, UNPRICED = "claude-sonnet-5-5", "claude-haiku-5-5", "claude-unpriced-1"
CHEAP = {"input": "1", "cache_write_5m": "1.25", "cache_write_1h": "2", "cache_read": "0.1",
         "output": "5", "since": "2026-11-19"}  # fmt: skip
MODELS = "SELECT organization_id, model FROM ai_provider_accounts ORDER BY organization_id"


@pytest.fixture(autouse=True)
def two_models(settings: Any) -> None:
    """Una lista permitida con dos modelos de precio distinto (la real tiene uno) y sin el tope."""
    settings.AI_ACCOUNT_THROTTLE = (60, 600)
    real = django_settings.AI_MODELS["ANTHROPIC"][SONNET]
    settings.AI_MODELS = {"ANTHROPIC": {SONNET: real, HAIKU: CHEAP}}


def pick(client: Any, account_id: Any, model: Any = HAIKU, base: str = BASE, **over: Any) -> Any:
    return send(client, "put", f"{base}{account_id}/model/", {"model": model} | over)


def changes(migrator: DB) -> list[Any]:
    return [row[1:] for row in audit(migrator) if row[0] == "ai.account.model_changed"]


def test_the_owner_changes_the_model_and_it_is_audited_with_before_and_after_only(
    crm: SimpleNamespace, migrator: DB, caplog: pytest.LogCaptureFixture
) -> None:
    ana = signed(crm.ana)
    account = create(ana).json()
    assert account["model"] == SONNET  # una cuenta nueva nace con el de `AI_PROVIDER_PARAMS`
    with caplog.at_level(logging.DEBUG):
        done = pick(ana, account["id"], HAIKU, organization_id=str(crm.b), provider="X")
        again = pick(ana, account["id"], HAIKU)  # el mismo: nada que cambiar ni auditar
        listed = ana.get(BASE).json()
    assert done.status_code == again.status_code == 200
    assert done.json()["model"] == listed[0]["model"] == HAIKU and "api_key" not in done.json()
    assert migrator.execute(MODELS).fetchall() == [(crm.a, HAIKU)]
    assert changes(migrator) == [("SUCCESS", {"model": [SONNET, HAIKU]}, {"provider": "ANTHROPIC"})]
    assert TEST_KEY not in caplog.text + str(audit(migrator))
    rotated = send(ana, "post", f"{BASE}{account['id']}/key/", {"api_key": OTHER_KEY})
    assert rotated.json()["model"] == HAIKU  # rotar la clave no cambia el modelo


def test_the_permission_goes_before_the_second_factor_and_nothing_changes_without_both(
    crm: SimpleNamespace, migrator: DB
) -> None:
    account_id = create(signed(crm.ana)).json()["id"]
    for model in (HAIKU, "no-existe"):
        for vera in (plain(crm.vera), signed(crm.vera)):  # vendedora: sin `ai.manage`
            assert reply(pick(vera, account_id, model)) == DENIED
        late = pick(plain(crm.ana), account_id, model)  # Owner sin segundo factor reciente
        assert (late.status_code, late.json()["code"]) == (403, STEP_UP_REQUIRED)
        assert reply(pick(Client(), account_id, model)) == UNAUTHENTICATED
    with tenant_scope(ctx(crm.a, crm.vera)) as seller, pytest.raises(AccessDenied):
        accounts.change_model(
            seller, provider="ANTHROPIC", model="no-existe"
        )  # antes que el modelo
    assert migrator.execute(MODELS).fetchall() == [(crm.a, SONNET)] and changes(migrator) == []


@pytest.mark.parametrize("model", ["claude-opus-9", "", "x" * 65, None, 7, "CLAUDE-SONNET-5-5"])
def test_a_model_outside_the_list_is_a_400_before_the_404_that_never_echoes_it(
    crm: SimpleNamespace, migrator: DB, model: Any
) -> None:
    ana = signed(crm.ana)
    account_id = create(ana).json()["id"]
    give(crm.b, crm.m_luis, {"ai.manage": None})
    luis = signed(crm.luis)
    for refusal in (pick(ana, account_id, model), pick(luis, account_id, model, B_BASE)):
        assert refusal.status_code == 400 and refusal.json()["fields"] == {
            "model": [{"code": refusal.json()["fields"]["model"][0]["code"],
                       "message": MODEL_REFUSED}]}  # fmt: skip
        assert model in (None, "", 7) or str(model) not in refusal.content.decode()
    assert migrator.execute(MODELS).fetchall() == [(crm.a, SONNET)] and changes(migrator) == []


def test_a_model_of_another_provider_is_refused_by_the_command(
    crm: SimpleNamespace, migrator: DB, settings: Any
) -> None:
    settings.AI_MODELS = {**settings.AI_MODELS, "OPENAI": {"gpt-y": CHEAP}}
    ana = signed(crm.ana)
    account_id = create(ana).json()["id"]
    refusal = pick(ana, account_id, "gpt-y")  # está en la lista, pero no en la de Anthropic
    assert refusal.status_code == 400 and refusal.json()["fields"]["model"][0]["message"] == (
        MODEL_REFUSED)  # fmt: skip
    assert "gpt-y" not in refusal.content.decode()
    with tenant_scope(ctx(crm.a, crm.ana)) as tenant, pytest.raises(accounts.InvalidModel):
        accounts.change_model(tenant, provider="ANTHROPIC", model="gpt-y")
    assert migrator.execute(MODELS).fetchall() == [(crm.a, SONNET)] and changes(migrator) == []


def test_each_organization_changes_only_its_own_active_account(
    crm: SimpleNamespace, migrator: DB
) -> None:
    give(crm.b, crm.m_luis, {"ai.manage": None})
    ana, luis = signed(crm.ana), signed(crm.luis)
    mine, theirs = create(ana).json()["id"], create(luis, OTHER_KEY, B_BASE).json()["id"]
    assert reply(pick(luis, mine, HAIKU, B_BASE)) == NOT_FOUND  # la de A no existe para B
    assert reply(pick(ana, theirs, HAIKU)) == NOT_FOUND
    assert pick(luis, theirs, HAIKU, B_BASE, organization_id=str(crm.a)).status_code == 200
    assert migrator.execute(MODELS).fetchall() == sorted([(crm.a, SONNET), (crm.b, HAIKU)])
    assert send(ana, "delete", f"{BASE}{mine}/", None).status_code == 204
    assert reply(pick(ana, mine, HAIKU)) == NOT_FOUND  # desactivada: el mismo 404
    with tenant_scope(ctx(crm.a, crm.ana)) as tenant, pytest.raises(accounts.AccountNotFound):
        accounts.change_model(tenant, provider="ANTHROPIC", model=HAIKU)
    assert migrator.execute(MODELS).fetchall() == sorted([(crm.a, SONNET), (crm.b, HAIKU)])


def test_the_change_counts_in_the_shared_write_limit(
    crm: SimpleNamespace, migrator: DB, settings: Any
) -> None:
    ana = signed(crm.ana)
    account_id = create(ana).json()["id"]
    settings.AI_ACCOUNT_THROTTLE = (2, 600)
    answers = [pick(ana, account_id, model) for model in (HAIKU, SONNET)]
    assert [a.status_code for a in answers] == [200, 429]  # la creación ya contó
    assert int(answers[-1]["Retry-After"]) > 0 and ana.get(BASE).status_code == 200
    assert [pick(signed(crm.vera), account_id).status_code for _ in range(3)] == [403, 403, 429]
    assert migrator.execute(COUNTED + " ORDER BY key").fetchall() == sorted(
        [(f"aik:usr:{crm.ana.pk}", 3), (f"aik:usr:{crm.vera.pk}", 3)])  # fmt: skip


DENIED_SQL = (  # `crm_app`, en el scope de A: el modelo sí; esto, no (0001 y 0008)
    "UPDATE ai_provider_accounts SET provider = 'ANTHROPIC'",
    "UPDATE ai_provider_accounts SET organization_id = %s",
    "UPDATE ai_provider_accounts SET id = uuidv7()",
    "UPDATE ai_provider_keys SET account_id = uuidv7()",
    "UPDATE ai_provider_keys SET provider = 'ANTHROPIC'",
    "UPDATE ai_provider_keys SET organization_id = %s",
    "DELETE FROM ai_provider_accounts",
)


def test_crm_app_writes_only_a_well_formed_model_of_its_own_account(
    crm: SimpleNamespace, migrator: DB
) -> None:
    for org, user in ((crm.a, crm.ana), (crm.b, crm.luis)):
        with tenant_scope(ctx(org, user)) as tenant:
            keys.store(tenant, "ANTHROPIC", TEST_KEY)
    for statement in DENIED_SQL:
        with tenant_scope(ctx(crm.a)), pytest.raises(DatabaseError, match="permission denied"):
            connection.cursor().execute(statement, [crm.b] if "%s" in statement else None)
    for bad in ("", "Claude Opus", "-x", "a" * 65):
        with tenant_scope(ctx(crm.a)), pytest.raises(DatabaseError) as caught:
            connection.cursor().execute("UPDATE ai_provider_accounts SET model = %s", [bad])
        assert "ai_provider_accounts_model_ck" in str(caught.value) or bad == "a" * 65
    with tenant_scope(ctx(crm.a)), connection.cursor() as cursor:
        cursor.execute("UPDATE ai_provider_accounts SET model = %s", [HAIKU])
        assert cursor.rowcount == 1  # solo la suya: RLS
    assert migrator.execute(MODELS).fetchall() == sorted([(crm.a, HAIKU), (crm.b, SONNET)])


def test_existing_accounts_keep_todays_model(migrator: DB, orgs: dict[str, Any]) -> None:
    """La migración rellena las cuentas que ya existían con el modelo que el gateway usaba hasta
    hoy: el gasto no cambia."""
    insert = (
        "INSERT INTO ai_provider_accounts (organization_id, provider, status, key_last_four,"
        " configured_at, updated_at) VALUES (%s, 'ANTHROPIC', 'ACTIVE', 'abcd', now(), now())"
    )
    try:
        with as_migrator():
            call_command("migrate", "ai_gateway", "0007", verbosity=0)
        migrator.execute(insert, [orgs["A"]])
    finally:
        with as_migrator():
            call_command("migrate", verbosity=0)
    assert migrator.execute(MODELS).fetchall() == [(orgs["A"], SONNET)]
    assert SONNET == django_settings.AI_PROVIDER_PARAMS["ANTHROPIC"]["model"]


def test_the_gateway_estimates_reserves_calls_and_charges_with_the_account_model(
    world: SimpleNamespace, migrator: DB
) -> None:
    budget(migrator, world.a, "1")
    with tenant_scope(ctx(world.a)), connection.cursor() as cursor:
        cursor.execute("UPDATE ai_provider_accounts SET model = %s", [HAIKU])
    call(world)
    assert world.fake.calls[0]["body"]["model"] == HAIKU
    body = gateway.ADAPTERS["ANTHROPIC"].encode(ASK, {**django_settings.AI_PROVIDER_PARAMS[
        "ANTHROPIC"], "model": HAIKU})  # fmt: skip
    ceiling = prices.ceiling("ANTHROPIC", HAIKU, len(body) + 1000, 2048)
    cost = prices.cost("ANTHROPIC", HAIKU, Usage(**USAGE))
    assert cost == Decimal("0.0035") != prices.cost("ANTHROPIC", SONNET, Usage(**USAGE))
    [row] = ledger(migrator)[1]
    assert row[:3] == ("OK", ceiling, cost)
    logged = migrator.execute(
        "SELECT r.model, c.model FROM ai_budget_reservations r JOIN ai_llm_calls c"
        " ON c.reservation_id = r.id"
    ).fetchall()
    assert logged == [(HAIKU, HAIKU)]


def test_a_well_formed_model_without_a_price_fails_closed_before_the_key_and_the_reservation(
    world: SimpleNamespace, migrator: DB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lo que `crm_app` escriba con SQL directo (la base solo comprueba la forma) no llama: el
    precio es la lista permitida."""
    budget(migrator, world.a, "1")
    with tenant_scope(ctx(world.a)), connection.cursor() as cursor:
        cursor.execute("UPDATE ai_provider_accounts SET model = %s", [UNPRICED])
    opened: list[Any] = []

    def open_key(*args: Any) -> str:
        opened.append(args)
        return TEST_KEY

    monkeypatch.setattr(keys, "open_key", open_key)
    assert code(world) == Outcome.AI_NOT_CONFIGURED
    assert opened == [] and world.fake.calls == [] and ledger(migrator) == ([], [])
    assert migrator.execute("SELECT count(*) FROM ai_llm_calls").fetchone() == (0,)
