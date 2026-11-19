"""F8-05b3 (#952): `GET`/`PUT …/ai/budget/` y el comando `apps.ai_gateway.budgets` (ADR-034 §5).
Middleware, sesión, CSRF, motor de autorización y PostgreSQL con `crm_app`. Primero `ai.manage`,
después el segundo factor reciente; la organización, la de la ruta; importes validados que nunca
se repiten; auditoría con el antes y el después y nada más; y lo que `crm_app` puede escribir con
SQL directo: solo los dos topes de la fila de su organización (0005)."""

import logging
import re
import threading
import time
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from django.db import DatabaseError, connection
from django.test import Client

from apps.access.selectors import AccessDenied
from apps.ai_gateway import budgets
from apps.ai_gateway.api.budget import ABOVE, INVALID
from core.api.step_up import STEP_UP_REQUIRED
from core.tenancy.scope import tenant_scope
from tests.conftest import migrator_conninfo
from tests.test_ai_budget_ledger import RESERVE, SETTLE, refused, scalar
from tests.test_ai_provider_accounts import crm, real_stack
from tests.test_ai_provider_accounts_api import create
from tests.test_ai_provider_test_api import COUNTED
from tests.test_authorization import give
from tests.test_channel_accounts_api import signed
from tests.test_conversations_api import UNAUTHENTICATED
from tests.test_member_branch import DENIED
from tests.test_memberships import ctx
from tests.test_self_context import NOT_FOUND, reply
from tests.test_self_context import signed as plain
from tests.test_team_writes import send

__all__ = ["crm", "real_stack"]  # los fixtures, para pytest
pytestmark = pytest.mark.usefixtures("tenant_db")
DB = psycopg.Connection[Any]
BASE, B_BASE = "/api/v1/o/org-a/ai/budget/", "/api/v1/o/org-b/ai/budget/"
EMPTY = {"monthly_usd": None, "daily_usd": None}
WAITING = "SELECT count(*) FROM pg_locks WHERE NOT granted"
CAPS = "SELECT organization_id, monthly_usd, daily_usd FROM ai_budgets ORDER BY organization_id"


@pytest.fixture(autouse=True)
def roomy(settings: Any) -> None:
    settings.AI_ACCOUNT_THROTTLE = (60, 600)


def put(client: Any, monthly: Any = "100", daily: Any = "10", base: str = BASE, **over: Any) -> Any:
    return send(client, "put", base, {"monthly_usd": monthly, "daily_usd": daily} | over)


def limits(migrator: DB) -> list[tuple[Any, ...]]:
    return migrator.execute(
        "SELECT action, entity_id, changes, metadata, organization_id FROM audit_logs"
        " WHERE entity_type = 'ai_budget' ORDER BY occurred_at, id"
    ).fetchall()


def test_set_read_and_change_are_audited_with_before_and_after_only(
    crm: SimpleNamespace, migrator: DB, caplog: pytest.LogCaptureFixture
) -> None:
    ana = signed(crm.ana)
    with caplog.at_level(logging.DEBUG):
        assert ana.get(BASE).json() == EMPTY  # sin topes: la IA no llama
        made = put(ana, "100", "10.5", organization_id=str(crm.b), id=str(crm.b))  # se ignoran
        changed = put(ana, 50, "50")  # el diario igual al mensual vale
        shown = ana.get(BASE)
        half = send(ana, "put", BASE, {"daily_usd": "1"})  # los dos son obligatorios
    assert made.status_code == 200 and made.json() == {
        "monthly_usd": "100.000000", "daily_usd": "10.500000"}  # fmt: skip
    assert half.status_code == 400 and set(half.json()["fields"]) == {"monthly_usd"}
    assert changed.json() == shown.json() == {"monthly_usd": "50.000000", "daily_usd": "50.000000"}
    [(org, monthly, daily)] = migrator.execute(CAPS).fetchall()  # solo la de A
    assert (org, monthly, daily) == (crm.a, Decimal(50), Decimal(50))
    row_id = migrator.execute("SELECT id FROM ai_budgets").fetchone()[0]  # type: ignore[index]
    assert limits(migrator) == [
        ("ai.limits.changed", row_id, {"monthly_usd": [None, "100.000000"],
                                       "daily_usd": [None, "10.500000"]}, {}, crm.a),
        ("ai.limits.changed", row_id, {"monthly_usd": ["100.000000", "50.000000"],
                                       "daily_usd": ["10.500000", "50.000000"]}, {}, crm.a),
    ]  # fmt: skip
    # Importes, solo en auditoría. La duración de una petición (`duration_ms`) puede valer 10.5:
    # se quita antes de buscar, para que la prueba no dependa de cuánto tardó.
    logged = re.sub(r"""duration_ms['"]?: [0-9.]+""", "", caplog.text)
    assert "10.5" not in logged and "50.0" not in logged


def test_the_caps_open_and_cut_the_gateway_and_open_reservations_still_settle(
    crm: SimpleNamespace, migrator: DB
) -> None:
    ana = signed(crm.ana)
    with tenant_scope(ctx(crm.a, crm.ana)):
        assert refused(RESERVE, ["0.1", "SUGGEST"]) == "ai_budget_not_set"
    assert put(ana, "1", "0.5").status_code == 200
    with tenant_scope(ctx(crm.a, crm.ana)):
        held = scalar(RESERVE, ["0.4", "SUGGEST"])
    assert put(ana, "0.1", "0.1").status_code == 200  # por debajo de lo reservado
    with tenant_scope(ctx(crm.a, crm.ana)):
        assert scalar(RESERVE, ["0.000001", "SUGGEST"]) is None  # cortada por el mes (F8-05b4)
    with tenant_scope(ctx(crm.a)):
        assert scalar(SETTLE, [held, "OK", "0.3", 1, 1, ""]) == "SETTLED"  # la abierta, sí


def test_the_permission_goes_before_the_second_factor_and_nothing_changes_without_both(
    crm: SimpleNamespace, migrator: DB
) -> None:
    for method, body in (("get", None), ("put", {"monthly_usd": "5", "daily_usd": "1"}),
                         ("put", {"monthly_usd": "NaN"})):  # fmt: skip
        for vera in (plain(crm.vera), signed(crm.vera)):  # vendedora: sin `ai.manage`
            assert reply(send(vera, method, BASE, body)) == DENIED, (method, body)
        late = send(plain(crm.ana), method, BASE, body)  # Owner sin segundo factor reciente
        assert (late.status_code, late.json()["code"]) == (403, STEP_UP_REQUIRED)
        assert reply(send(Client(), method, BASE, body)) == UNAUTHENTICATED
    assert migrator.execute(CAPS).fetchall() == [] and limits(migrator) == []


def test_each_organization_sets_only_its_own_caps(crm: SimpleNamespace, migrator: DB) -> None:
    give(crm.b, crm.m_luis, {"ai.manage": None})
    luis = signed(crm.luis)
    assert put(signed(crm.ana), "100", "10").status_code == 200
    assert luis.get(B_BASE).json() == EMPTY  # los de A no existen para B
    assert put(luis, "7", "7", B_BASE, organization_id=str(crm.a)).status_code == 200
    assert reply(luis.get(BASE)) == NOT_FOUND and reply(put(luis, "1", "1")) == NOT_FOUND
    caps = {org: (m, d) for org, m, d in migrator.execute(CAPS).fetchall()}
    assert caps == {crm.a: (Decimal(100), Decimal(10)), crm.b: (Decimal(7), Decimal(7))}
    assert [row[4] for row in limits(migrator)] == [crm.a, crm.b]


BAD = [  # (mensual, diario) -> el campo que lo rechaza; ningún valor vuelve en la respuesta
    (("NaN", "1"), "monthly_usd"), (("1", "Infinity"), "daily_usd"), (("-1", "1"), "monthly_usd"),
    (("0", "1"), "monthly_usd"), (("1", "0.0000001"), "daily_usd"), (("1.1234567", "1"),
    "monthly_usd"), (("1000000", "1"), "monthly_usd"), (("1", "abc"), "daily_usd"),
    ((None, "1"), "monthly_usd"), (("10", "10.000001"), "daily_usd"), (("1", -0.5), "daily_usd"),
]  # fmt: skip


@pytest.mark.parametrize(("amounts", "field"), BAD)
def test_bad_amounts_are_a_400_on_their_field_that_never_echoes_them(
    crm: SimpleNamespace, migrator: DB, amounts: tuple[Any, Any], field: str
) -> None:
    refusal = put(signed(crm.ana), *amounts)
    assert refusal.status_code == 400 and set(refusal.json()["fields"]) == {field}
    above = amounts == ("10", "10.000001")  # el mensaje propio, el de la ruta
    assert [e["message"] for e in refusal.json()["fields"][field]] == [ABOVE if above else INVALID]
    for value in amounts:
        assert value is None or str(value) not in refusal.content.decode()
    assert migrator.execute(CAPS).fetchall() == [] and limits(migrator) == []


@pytest.mark.parametrize(
    ("monthly", "daily", "field"),
    [("NaN", "1", "monthly_usd"), ("1", "Infinity", "daily_usd"), ("-1", "1", "monthly_usd"),
     ("0", "1", "monthly_usd"), ("2", "1.0000001", "daily_usd"), ("1000000", "1", "monthly_usd"),
     ("1", "1.000001", "daily_usd")],
)  # fmt: skip
def test_the_command_checks_the_permission_then_the_amounts(
    crm: SimpleNamespace, migrator: DB, monthly: str, daily: str, field: str
) -> None:
    caps = {"monthly_usd": Decimal(monthly), "daily_usd": Decimal(daily)}
    with tenant_scope(ctx(crm.a, crm.vera)) as vera:  # sin `ai.manage`: ni lee ni escribe
        with pytest.raises(AccessDenied):
            budgets.set_caps(vera, **caps)
        with pytest.raises(AccessDenied):
            budgets.read(vera)
    with tenant_scope(ctx(crm.a, crm.ana)) as ana, pytest.raises(budgets.InvalidCap) as caught:
        budgets.set_caps(ana, **caps)
    assert caught.value.field == field and str(caught.value) == field
    assert migrator.execute(CAPS).fetchall() == [] and limits(migrator) == []


@pytest.mark.parametrize("existing", [False, True])
def test_two_puts_at_once_make_one_row_and_chain_their_audit(
    crm: SimpleNamespace, migrator: DB, existing: bool
) -> None:
    """Sin fila, la segunda espera a que la primera la inserte; con fila, a su bloqueo: el antes de
    cada una es lo que dejó la otra."""
    if existing:
        migrator.execute("INSERT INTO ai_budgets VALUES (%s, uuidv7(), 9, 9)", [crm.a])
    errors = list[BaseException]()

    def run(amount: str) -> None:
        try:
            with tenant_scope(ctx(crm.a, crm.ana)) as ana:
                budgets.set_caps(ana, monthly_usd=Decimal(amount), daily_usd=Decimal(amount))
        except BaseException as error:  # noqa: BLE001 — se comprueba abajo
            errors.append(error)
        finally:
            connection.close()

    threads = [threading.Thread(target=run, args=(a,)) for a in ("1", "2")]
    with psycopg.connect(migrator_conninfo()) as holder:  # las dos esperan a la vez
        holder.execute("SELECT 1 FROM ai_budgets FOR UPDATE" if existing
                       else "LOCK TABLE ai_budgets IN SHARE MODE")  # fmt: skip
        for thread in threads:
            thread.start()
        for _ in range(200):
            if migrator.execute(WAITING).fetchone() == (2,):
                break
            time.sleep(0.05)
        holder.rollback()
    for thread in threads:
        thread.join(timeout=30)
    assert errors == [] and len(migrator.execute(CAPS).fetchall()) == 1
    pairs = [tuple(row[2]["monthly_usd"]) for row in limits(migrator)]
    [head] = [pair for pair in pairs if pair[0] == ("9.000000" if existing else None)]
    [tail] = [pair for pair in pairs if pair != head]
    assert tail[0] == head[1] != tail[1]


def test_the_write_counts_in_the_shared_limit_and_reads_do_not(
    crm: SimpleNamespace, migrator: DB, settings: Any
) -> None:
    settings.AI_ACCOUNT_THROTTLE = (2, 600)
    ana = signed(crm.ana)
    assert [ana.get(BASE).status_code for _ in range(3)] == [200] * 3
    answers = [put(ana), put(ana), put(ana)]
    assert [a.status_code for a in answers] == [200, 200, 429]
    assert answers[-1].json() == {"code": "RATE_LIMITED"} and int(answers[-1]["Retry-After"]) > 0
    assert create(ana).status_code == 429  # las rutas de la clave conservan su tope
    assert [create(signed(crm.vera)).status_code for _ in range(3)] == [403, 403, 429]
    assert migrator.execute(COUNTED + " ORDER BY key").fetchall() == sorted(
        [(f"aik:usr:{crm.ana.pk}", 4), (f"aik:usr:{crm.vera.pk}", 3)])  # fmt: skip
    assert len(limits(migrator)) == 2


DENIED_SQL = (  # `crm_app`, en el scope de A: ni otra columna ni borrar
    "UPDATE ai_budgets SET organization_id = %s",
    "UPDATE ai_budgets SET id = uuidv7()",
    "INSERT INTO ai_budgets (id, organization_id, monthly_usd, daily_usd)"
    " VALUES (uuidv7(), %s, 1, 1)",
    "DELETE FROM ai_budgets",
    "TRUNCATE ai_budgets",
)


def test_crm_app_writes_only_the_two_caps_of_its_own_row(
    crm: SimpleNamespace, migrator: DB
) -> None:
    for org in (crm.b, None):  # otra organización o ninguna: RLS
        with tenant_scope(ctx(crm.a)):
            with pytest.raises(DatabaseError, match="row-level security|null value"):
                connection.cursor().execute(
                    "INSERT INTO ai_budgets (organization_id, monthly_usd, daily_usd)"
                    " VALUES (%s, 1, 1)", [org])  # fmt: skip
    assert migrator.execute(CAPS).fetchall() == []
    insert = "INSERT INTO ai_budgets VALUES (%s, uuidv7(), %s, %s)"
    migrator.execute(insert, [crm.b, 5, 5])
    migrator.execute(insert, [crm.a, 9, None])  # un mensual sin diario (F8-05b2) sigue valiendo
    for statement in DENIED_SQL:
        with tenant_scope(ctx(crm.a)), pytest.raises(DatabaseError, match="permission denied"):
            connection.cursor().execute(statement, [crm.b] if "%s" in statement else None)
    with tenant_scope(ctx(crm.a)):
        cursor = connection.cursor()
        cursor.execute("UPDATE ai_budgets SET monthly_usd = 3, daily_usd = 2")
        assert cursor.rowcount == 1
    with tenant_scope(ctx(crm.a)):
        assert refused("UPDATE ai_budgets SET daily_usd = monthly_usd + 1", []) == (
            "ai_budgets_daily_le_monthly")  # fmt: skip
    caps = {org: (m, d) for org, m, d in migrator.execute(CAPS).fetchall()}
    assert caps == {crm.a: (Decimal(3), Decimal(2)), crm.b: (Decimal(5), Decimal(5))}
