"""F8-03d: `POST …/ai/provider-accounts/{id}/test/` y el tope por persona de las cinco rutas de
cuentas (ADR-034 §4, como F7-02e). Middleware, sesión, CSRF, motor de autorización y PostgreSQL con
`crm_app`; la API de Anthropic, simulada sin red (`tests.ai_sim`). Primero el tope, después
`ai.manage` y después el segundo factor reciente (D-F7-11); un solo 404; la respuesta lleva solo el
código de la prueba, nunca la clave ni el cuerpo del proveedor. Claves ficticias."""

import logging
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from django.test import Client

from core.api.step_up import STEP_UP_REQUIRED
from tests import ai_sim
from tests.ai_sim import BODY_MARK, REQUEST_ID, TEST_KEY, FakeAnthropic
from tests.test_ai_provider_accounts import OTHER_KEY, audit, crm, everything, real_stack
from tests.test_ai_provider_accounts_api import BASE, create, rotate
from tests.test_authorization import give
from tests.test_channel_accounts_api import signed
from tests.test_conversations_api import UNAUTHENTICATED
from tests.test_member_branch import DENIED
from tests.test_self_context import NOT_FOUND, reply
from tests.test_self_context import signed as plain
from tests.test_team_writes import send

__all__ = ["crm", "real_stack"]  # los fixtures, para pytest
pytestmark = pytest.mark.usefixtures("tenant_db")
DB = psycopg.Connection[Any]
B_BASE = "/api/v1/o/org-b/ai/provider-accounts/"
COUNTED = "SELECT key, failures FROM login_throttles WHERE key LIKE 'aik:%'"
LIMITED = {"code": "RATE_LIMITED"}


@pytest.fixture
def fake(crm: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> FakeAnthropic:
    """La API falsa servida por `core.http` y `ai.manage` para luis (de B); ana es Owner de A."""
    give(crm.b, crm.m_luis, {"ai.manage": None})
    return ai_sim.install(monkeypatch, FakeAnthropic())


def probe(client: Any, account_id: Any, base: str = BASE) -> Any:
    return send(client, "post", f"{base}{account_id}/test/", None)


def audited(migrator: DB) -> list[tuple[Any, ...]]:
    return [row[1:] for row in audit(migrator) if row[0] == "ai.account.tested"]


@pytest.mark.parametrize(
    ("outcome", "code"),
    [
        ("ok", "OK"),
        ("unauthorized", "CREDENTIALS_REJECTED"),
        ("forbidden", "CREDENTIALS_REJECTED"),
        ("billing", "PROVIDER_BILLING"),
        ("spend_limit", "PROVIDER_BILLING"),
        ("tier_limit", "PROVIDER_BILLING"),
        ("rate_limit", "PROVIDER_UNAVAILABLE"),
        ("overloaded", "PROVIDER_UNAVAILABLE"),
        ("garbage", "PROVIDER_UNAVAILABLE"),
        ("timeout", "PROVIDER_UNAVAILABLE"),
    ],
)
def test_the_route_answers_only_the_closed_code_and_audits_it(
    crm: SimpleNamespace,
    fake: FakeAnthropic,
    migrator: DB,
    caplog: pytest.LogCaptureFixture,
    outcome: str,
    code: str,
) -> None:
    ana = signed(crm.ana)
    account = create(ana).json()
    fake.outcome = outcome
    with caplog.at_level(logging.DEBUG):
        answer = probe(ana, account["id"])
    assert (answer.status_code, answer.json()) == (200, {"result": code})
    assert len(fake.calls) == 1 and fake.retries[0].total == 0  # una sola llamada, sin reintentos
    result = "SUCCESS" if code == "OK" else "FAILED"
    assert audited(migrator) == [(result, {}, {"provider": "ANTHROPIC", "result": code})]
    seen = answer.content.decode() + caplog.text + everything(migrator)
    assert TEST_KEY not in seen and BODY_MARK not in seen
    assert (
        REQUEST_ID not in answer.content.decode()
    )  # ni siquiera el id de la petición del proveedor


def test_the_permission_goes_before_the_second_factor_and_nobody_reaches_the_provider(
    crm: SimpleNamespace, fake: FakeAnthropic, migrator: DB
) -> None:
    account_id = create(signed(crm.ana)).json()["id"]
    for vera in (plain(crm.vera), signed(crm.vera)):  # vendedora: sin `ai.manage`
        assert reply(probe(vera, account_id)) == DENIED
    late = probe(plain(crm.ana), account_id)  # Owner sin segundo factor reciente
    assert (late.status_code, late.json()["code"]) == (403, STEP_UP_REQUIRED)
    assert reply(probe(Client(), account_id)) == UNAUTHENTICATED
    assert fake.calls == [] and audited(migrator) == []


def test_another_organizations_unknown_or_disabled_account_is_a_single_404(
    crm: SimpleNamespace, fake: FakeAnthropic, migrator: DB
) -> None:
    ana, luis = signed(crm.ana), signed(crm.luis)
    mine = create(ana).json()["id"]
    theirs = create(luis, OTHER_KEY, base=B_BASE).json()["id"]
    for target in (mine, uuid4()):  # el id de A nunca llega a la cuenta de B
        assert reply(probe(luis, target, B_BASE)) == NOT_FOUND
    assert send(ana, "delete", f"{BASE}{mine}/", None).status_code == 204
    assert reply(probe(ana, mine)) == NOT_FOUND  # desactivada: el mismo 404
    assert reply(probe(ana, theirs)) == NOT_FOUND
    assert fake.calls == [] and audited(migrator) == []
    fake.key = OTHER_KEY
    assert probe(luis, theirs, B_BASE).json() == {"result": "OK"}  # la suya, sí


def test_the_limit_is_shared_by_the_five_routes_and_stops_a_probe_loop(
    crm: SimpleNamespace, fake: FakeAnthropic, migrator: DB, settings: Any
) -> None:
    settings.AI_ACCOUNT_THROTTLE = (4, 600)
    ana = signed(crm.ana)
    account_id = create(ana).json()["id"]
    answers = [probe(ana, account_id) for _ in range(4)]
    assert [a.status_code for a in answers] == [200, 200, 200, 429]
    assert answers[-1].json() == LIMITED and 0 < int(answers[-1]["Retry-After"]) <= 600
    assert len(fake.calls) == 3 and len(audited(migrator)) == 3  # el cuarto no llegó al proveedor
    for method, path, body in (
        ("get", BASE, None), ("post", BASE, {"provider": "ANTHROPIC", "api_key": OTHER_KEY}),
        ("post", f"{BASE}{account_id}/key/", {"api_key": OTHER_KEY}),
        ("delete", f"{BASE}{account_id}/", None), ("post", f"{BASE}{uuid4()}/test/", None),
    ):  # fmt: skip
        assert reply(send(ana, method, path, body)) == (429, b'{"code":"RATE_LIMITED"}'), path
    assert migrator.execute(COUNTED).fetchall() == [(f"aik:usr:{crm.ana.pk}", 10)]
    assert len(fake.calls) == 3 and signed(crm.luis).get(B_BASE).status_code == 200  # otra persona
    assert OTHER_KEY not in everything(migrator)


def test_a_member_without_the_permission_cannot_use_up_the_limit_of_another(
    crm: SimpleNamespace, fake: FakeAnthropic, settings: Any
) -> None:
    settings.AI_ACCOUNT_THROTTLE = (2, 600)
    vera = signed(crm.vera)  # sin `ai.manage`: el tope cuenta antes del permiso, y solo el suyo
    assert [vera.get(BASE).status_code for _ in range(4)] == [403, 403, 429, 429]
    assert create(signed(crm.ana)).status_code == 201


def test_the_eleventh_request_in_ten_minutes_is_refused_with_the_default_limit(
    crm: SimpleNamespace, fake: FakeAnthropic
) -> None:
    ana = signed(crm.ana)
    answers = [probe(ana, uuid4()) for _ in range(11)]
    assert [a.status_code for a in answers] == [404] * 10 + [429]
    assert answers[-1].json() == LIMITED and answers[-1]["Retry-After"]


def test_a_request_the_csrf_check_refuses_or_an_anonymous_one_never_uses_the_limit(
    crm: SimpleNamespace, fake: FakeAnthropic, settings: Any, migrator: DB
) -> None:
    """Una página ajena que fuerza peticiones con la sesión de la víctima no le gasta el tope."""
    settings.AI_ACCOUNT_THROTTLE = (2, 600)
    ana = signed(crm.ana)
    for _ in range(4):  # sin la cabecera del CSRF: la rechaza el CSRF, antes del tope
        forged = ana.post(f"{BASE}{uuid4()}/test/", None, "application/json")
        assert reply(forged) == (403, b'{"code":"CSRF_FAILED"}')
    anonymous = Client(enforce_csrf_checks=True)
    assert [probe(anonymous, uuid4()).status_code for _ in range(4)] == [401] * 4
    assert migrator.execute(COUNTED).fetchall() == []  # nada contó
    assert create(ana).status_code == 201 and rotate(ana, uuid4()).status_code == 404
    assert migrator.execute(COUNTED).fetchall() == [(f"aik:usr:{crm.ana.pk}", 2)]
