"""F8-03d: `POST …/ai/provider-accounts/{id}/test/` y el tope por persona de las escrituras de
las rutas de cuentas (ADR-034 §4, como F7-02e; F8-03e: GET y HEAD no cuentan). Middleware, sesión,
CSRF, motor de autorización y PostgreSQL con `crm_app`; la API de Anthropic, simulada sin red
(`tests.ai_sim`). Primero el tope, después `ai.manage` y después el segundo factor reciente
(D-F7-11); un solo 404; la respuesta lleva solo el código de la prueba, nunca la clave ni el cuerpo
del proveedor. Claves ficticias."""

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
CONTACTS = "/api/v1/o/org-a/contacts/"  # otra ruta de tenant con escritura


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


def test_the_limit_is_shared_by_the_four_writes_and_stops_a_probe_loop(
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
        ("post", BASE, {"provider": "ANTHROPIC", "api_key": OTHER_KEY}),
        ("post", f"{BASE}{account_id}/key/", {"api_key": OTHER_KEY}),
        ("delete", f"{BASE}{account_id}/", None), ("post", f"{BASE}{uuid4()}/test/", None),
    ):  # fmt: skip
        assert reply(send(ana, method, path, body)) == (429, b'{"code":"RATE_LIMITED"}'), path
    assert migrator.execute(COUNTED).fetchall() == [(f"aik:usr:{crm.ana.pk}", 9)]
    assert len(fake.calls) == 3 and signed(crm.luis).get(B_BASE).status_code == 200  # otra persona
    assert OTHER_KEY not in everything(migrator)


def test_a_member_without_the_permission_cannot_use_up_the_limit_of_another(
    crm: SimpleNamespace, fake: FakeAnthropic, settings: Any
) -> None:
    settings.AI_ACCOUNT_THROTTLE = (2, 600)
    vera = signed(crm.vera)  # sin `ai.manage`: el tope cuenta antes del permiso, y solo el suyo
    assert [create(vera).status_code for _ in range(4)] == [403, 403, 429, 429]
    assert create(signed(crm.ana)).status_code == 201


def test_a_write_refused_for_lack_of_a_recent_second_factor_still_counts(
    crm: SimpleNamespace, fake: FakeAnthropic, settings: Any, migrator: DB
) -> None:
    settings.AI_ACCOUNT_THROTTLE = (2, 600)
    late = plain(crm.ana)  # Owner sin segundo factor reciente
    answers = [send(late, "delete", f"{BASE}{uuid4()}/", None) for _ in range(3)]
    assert [(a.status_code, a.json()["code"]) for a in answers] == [
        (403, STEP_UP_REQUIRED), (403, STEP_UP_REQUIRED), (429, "RATE_LIMITED"),
    ]  # fmt: skip
    assert migrator.execute(COUNTED).fetchall() == [(f"aik:usr:{crm.ana.pk}", 3)]


def test_the_eleventh_write_in_ten_minutes_is_refused_with_the_default_limit_reads_in_between(
    crm: SimpleNamespace, fake: FakeAnthropic, migrator: DB
) -> None:
    """Las cuatro escrituras cuentan (guardar, rotar, probar y borrar), y leer entre ellas no."""
    ana = signed(crm.ana)
    account_id = create(ana).json()["id"]
    writes = [
        ("post", f"{BASE}{account_id}/key/", {"api_key": OTHER_KEY}),
        ("post", f"{BASE}{account_id}/test/", None),
        ("delete", f"{BASE}{account_id}/", None),
        *[("post", f"{BASE}{uuid4()}/test/", None)] * 6,
        ("post", BASE, {"provider": "ANTHROPIC", "api_key": TEST_KEY}),  # la undécima escritura
    ]
    answers = []
    for method, path, body in writes:
        assert ana.get(BASE).status_code == 200 and ana.head(BASE).status_code == 200
        answers.append(send(ana, method, path, body))
    assert [a.status_code for a in answers] == [200, 200, 204] + [404] * 6 + [429]
    assert answers[-1].json() == LIMITED and 0 < int(answers[-1]["Retry-After"]) <= 600
    assert migrator.execute(COUNTED).fetchall() == [(f"aik:usr:{crm.ana.pk}", 11)]
    assert len(fake.calls) == 1  # la prueba de conexión llamó una vez al proveedor


def test_with_the_limit_used_up_by_writes_a_read_still_answers(
    crm: SimpleNamespace, fake: FakeAnthropic, settings: Any
) -> None:
    settings.AI_ACCOUNT_THROTTLE = (2, 600)
    ana = signed(crm.ana)
    account = create(ana).json()
    assert rotate(ana, uuid4()).status_code == 404 and probe(ana, account["id"]).status_code == 429
    listed = ana.get(BASE)
    assert listed.status_code == 200 and [a["id"] for a in listed.json()] == [account["id"]]
    assert (ana.head(BASE).status_code, ana.head(BASE).content) == (200, b"")


def test_eleven_reads_in_a_row_are_never_refused_and_count_nothing(
    crm: SimpleNamespace, fake: FakeAnthropic, migrator: DB
) -> None:
    ana = signed(crm.ana)
    assert [ana.get(BASE).status_code for _ in range(11)] == [200] * 11
    assert migrator.execute(COUNTED).fetchall() == []


def test_a_head_counts_nothing(crm: SimpleNamespace, fake: FakeAnthropic, migrator: DB) -> None:
    ana = signed(crm.ana)
    assert [ana.head(BASE).status_code for _ in range(11)] == [200] * 11
    assert migrator.execute(COUNTED).fetchall() == []


def test_options_is_still_denied_and_counts_nothing(
    crm: SimpleNamespace, fake: FakeAnthropic, migrator: DB
) -> None:
    ana = signed(crm.ana)
    account_id = create(ana).json()["id"]
    for path in (
        BASE, f"{BASE}{account_id}/", f"{BASE}{account_id}/key/", f"{BASE}{account_id}/test/",
    ):  # fmt: skip
        answers = [send(ana, "options", path, None) for _ in range(11)]
        assert {reply(a) for a in answers} == {DENIED}, path
    assert migrator.execute(COUNTED).fetchall() == [(f"aik:usr:{crm.ana.pk}", 1)]  # el alta


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


def test_any_method_that_is_not_a_read_counts_even_one_no_view_declares(
    crm: SimpleNamespace, fake: FakeAnthropic, settings: Any, migrator: DB
) -> None:
    """Falla cerrado: un PUT o un PATCH (hoy denegados por las vistas) cuentan como escrituras."""
    settings.AI_ACCOUNT_THROTTLE = (2, 600)
    ana = signed(crm.ana)
    path = f"{BASE}{uuid4()}/"
    answers = [send(ana, method, path, {}) for method in ("patch", "put", "patch")]
    assert [reply(a) for a in answers[:2]] == [DENIED, DENIED]  # ninguna vista los declara
    assert reply(answers[2]) == (429, b'{"code":"RATE_LIMITED"}')
    assert migrator.execute(COUNTED).fetchall() == [(f"aik:usr:{crm.ana.pk}", 3)]


def test_writes_on_other_tenant_routes_never_touch_this_limit(
    crm: SimpleNamespace, fake: FakeAnthropic, migrator: DB
) -> None:
    """El tope es solo de las rutas de cuentas de IA: escribir en el resto del CRM no lo gasta."""
    ana = signed(crm.ana)
    made = [send(ana, "post", CONTACTS, {"display_name": f"Cliente {i}"}) for i in range(11)]
    assert [a.status_code for a in made] == [201] * 11
    assert migrator.execute(COUNTED).fetchall() == []
    assert create(ana).status_code == 201  # y su tope de IA sigue entero
