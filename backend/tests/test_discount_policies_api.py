"""F5-09b5: `…/quotes/discount-policies/` (`GET` y `PUT`) con el motor de autorización, el
segundo factor, la sesión y PostgreSQL con `crm_app`: el permiso antes que el step-up, el cuerpo
cerrado y estricto, las referencias ajenas como las inexistentes, la versión, las carreras y el
aislamiento."""

import logging
from decimal import Decimal as D
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from django.test import Client

from apps.access.models import Role
from apps.quotes.api.quotes import QuoteView
from apps.quotes.commands import NotAllowed
from apps.quotes.models import DiscountPolicy, Quote
from apps.quotes.seed import ensure_template_policies
from core.api.errors import exception_handler
from core.api.step_up import MAX_AGE
from core.tenancy.scope import tenant_scope
from tests import test_price_lists_api, test_self_context
from tests.factories import make_user
from tests.test_authorization import give
from tests.test_cost_routes import stepped, verified
from tests.test_inventory_reservations_api import at_once
from tests.test_member_branch import DENIED
from tests.test_memberships import ctx, join
from tests.test_prices import price_list
from tests.test_products import send
from tests.test_quote_guards import role
from tests.test_self_context import NOT_FOUND, reply, signed

shop, real_stack = test_price_lists_api.shop, test_self_context.real_stack
pytestmark = pytest.mark.usefixtures("tenant_db")
type DB = psycopg.Connection[Any]
URL = "/api/v1/o/{}/quotes/discount-policies/"
MANAGE = "discount_policies.manage"
FIELDS = "id role_id price_list_id max_discount_percent max_discount_amount currency version"
FIELDS += " created_at updated_at"


@pytest.fixture
def team(shop: SimpleNamespace, orgs: Any) -> SimpleNamespace:
    """Org A con sus plantillas y sus límites: Administrador 100 %, Supervisor 10 %, Vendedor 3 %
    (solo el Owner `a` y el Administrador `x` tienen `discount_policies.manage`)."""
    with tenant_scope(ctx(orgs["A"])) as tenant:
        ensure_template_policies(tenant)
    return SimpleNamespace(**vars(shop), org=orgs["A"], target=role(orgs["A"], "gerencia"))


def put(client: Client, org: str = "org-a", **body: Any) -> Any:
    return send(client, "put", URL.format(org), body)


def listed(client: Client, org: str = "org-a") -> Any:
    return client.get(URL.format(org))


def audits(migrator: DB) -> int:
    query = "SELECT count(*) FROM audit_logs WHERE action = 'discount_policy.changed'"
    return int(migrator.execute(query).fetchone()[0])  # type: ignore[index]


def rows(migrator: DB) -> list[tuple[Any, ...]]:
    query = "SELECT role_id, applies_to_price_list_id, max_discount_percent, max_discount_amount,"
    query += " currency, version FROM discount_policies ORDER BY id"
    return migrator.execute(query).fetchall()


def manager(team: SimpleNamespace, percent: str, **policy: Any) -> Client:
    """Alguien con `discount_policies.manage` (y `prices.view`: ve las listas), un rol propio con
    ese límite y el segundo factor."""
    user = make_user()
    made = give(team.org, join(team.org, user).pk, {MANAGE: None, "prices.view": None})
    with tenant_scope(ctx(team.org)):
        DiscountPolicy.objects.create(role_id=made.pk, max_discount_percent=D(percent), **policy)
    return verified(signed(user))


def code_of(team: SimpleNamespace, code: str) -> Any:
    with tenant_scope(ctx(team.org)):
        return Role.objects.get(code=code).pk


def test_who_may_read_and_change_the_limits_permission_first_then_the_second_factor(
    team: SimpleNamespace, migrator: DB
) -> None:
    body = {"role_id": str(team.target), "max_discount_percent": "5"}
    before = (rows(migrator), audits(migrator))
    anonymous = Client(enforce_csrf_checks=True)
    assert reply(listed(anonymous))[0] == 401 and put(anonymous, **body).status_code == 401
    assert reply(listed(signed(make_user()))) == NOT_FOUND  # no es miembro
    for client in (team.v, team.s, verified(team.v), verified(team.s)):  # sin el permiso
        assert reply(listed(client)) == DENIED and reply(put(client, **body)) == DENIED
        for method in ("patch", "delete", "post"):  # nunca STEP_UP_REQUIRED ni 405
            assert reply(send(client, method, URL.format("org-a"), body)) == DENIED
    for client in (team.a, team.x):  # con el permiso y sin segundo factor: leer sí, cambiar no
        assert listed(client).status_code == 200
        assert stepped(put(client, **body))
        assert stepped(put(verified(client, MAX_AGE + 60), **body))  # vencido
    assert (rows(migrator), audits(migrator)) == before
    admin = verified(team.x, MAX_AGE - 30)
    for method in ("patch", "delete"):
        assert reply(send(admin, method, URL.format("org-a"), body)) == (
            405,
            b'{"code":"METHOD_NOT_ALLOWED"}',
        )
    assert reply(send(admin, "post", URL.format("org-a"), body)) == DENIED  # no declarado
    assert put(admin, **body).status_code == 201


def test_the_acceptance_case_the_admin_sets_a_limit_reads_it_and_changes_it(
    team: SimpleNamespace, migrator: DB
) -> None:
    admin, usd = verified(team.x), price_list(team.org, "USD1", currency="USD")
    made = put(admin, role_id=str(team.target), max_discount_percent="7.5",
               max_discount_amount="120", currency="PEN")  # fmt: skip
    assert made.status_code == 201, made.content
    body = made.json()
    assert set(body) == set(FIELDS.split())
    assert [body[k] for k in FIELDS.split()[1:7]] == [
        str(team.target), None, "7.50", "120.00", "PEN", 1,
    ]  # fmt: skip
    listed_ = put(admin, role_id=str(team.target), price_list_id=str(usd), max_discount_percent="3")
    assert (listed_.status_code, listed_.json()["currency"]) == (201, "USD")  # la de la lista
    page = listed(team.a).json()  # el Owner lee sin segundo factor
    assert page["next"] is None and len(page["results"]) == 5  # las tres de plantilla y las dos
    assert body in page["results"]
    changed = put(admin, role_id=str(team.target), max_discount_percent="8", version=1)
    assert (changed.status_code, changed.json()["version"]) == (200, 2)
    assert changed.json()["max_discount_amount"] is None  # PUT: lo que no se envía se quita
    actor = migrator.execute(
        "SELECT DISTINCT actor_id FROM audit_logs WHERE action = 'discount_policy.changed'"
    ).fetchall()
    assert len(actor) == 1 and audits(migrator) == 3


@pytest.mark.parametrize(
    ("field", "wrong"),
    [
        ("max_discount_percent", {"max_discount_percent": "NaN"}),
        ("max_discount_percent", {"max_discount_percent": "Infinity"}),
        ("max_discount_percent", {"max_discount_percent": "1e2"}),
        ("max_discount_percent", {"max_discount_percent": "10.005"}),
        ("max_discount_percent", {"max_discount_percent": "100.01"}),
        ("max_discount_percent", {"max_discount_percent": "-1"}),
        ("max_discount_percent", {"max_discount_percent": 10.5}),
        ("max_discount_percent", {"max_discount_percent": 10}),
        ("max_discount_percent", {"max_discount_percent": True}),
        ("max_discount_percent", {"max_discount_percent": None}),
        ("max_discount_amount", {"max_discount_amount": "-0.01", "currency": "PEN"}),
        ("max_discount_amount", {"max_discount_amount": 1.5, "currency": "PEN"}),
        ("currency", {"max_discount_amount": "10"}),  # el tope sin su moneda
        ("currency", {"currency": "PEN"}),  # todas las listas: moneda sin tope
        ("currency", {"max_discount_amount": "10", "currency": "pen"}),
        ("currency", {"list": "usd", "currency": "PEN"}),  # no es la de la lista
        ("currency", {"max_discount_amount": "10", "currency": 604}),
        ("organization_id", {"organization_id": "B"}),
        ("id", {"id": str(uuid4())}),
        ("version", {"version": 0}),
        ("version", {"version": -1}),
        ("version", {"version": True}),
        ("version", {"version": "1"}),
        ("version", {"version": 1.0}),
        ("role_id", {"role_id": "abc"}),
        ("role_id", {"role_id": None}),
        ("price_list_id", {"price_list_id": 5}),
    ],
)
def test_invalid_input_is_a_field_error_and_writes_nothing(
    team: SimpleNamespace, migrator: DB, field: str, wrong: dict[str, Any]
) -> None:
    before = (rows(migrator), audits(migrator))
    body = {"role_id": str(team.target), "max_discount_percent": "5", **wrong}
    if body.pop("list", None):
        body["price_list_id"] = str(price_list(team.org, "USD1", currency="USD"))
    if body.get("organization_id") == "B":
        body["organization_id"] = str(team.b)
    answer = put(verified(team.x), **body)
    assert (answer.status_code, answer.json()["code"]) == (400, "VALIDATION_ERROR"), answer.content
    assert set(answer.json()["fields"]) == {field}
    sent = wrong.get(field)
    assert not (isinstance(sent, str) and len(sent) > 2 and sent in answer.text)  # no lo repite
    assert (rows(migrator), audits(migrator)) == before


def test_a_foreign_a_missing_and_the_owner_role_are_the_same_byte_identical_400(
    team: SimpleNamespace, migrator: DB
) -> None:
    admin, before = verified(team.x), (rows(migrator), audits(migrator))
    roles = [uuid4(), role(team.b), code_of(team, "owner")]
    answers = {reply(put(admin, role_id=str(r), max_discount_percent="5")) for r in roles}
    assert len(answers) == 1 and next(iter(answers))[0] == 400
    lists = [uuid4(), price_list(team.b, "DE-B")]
    answers = {
        reply(put(admin, role_id=str(team.target), price_list_id=str(p), max_discount_percent="5"))
        for p in lists
    }
    assert len(answers) == 1 and next(iter(answers))[0] == 400
    assert (rows(migrator), audits(migrator)) == before


def test_who_does_not_cover_the_limit_gets_a_bare_403_and_learns_nothing(
    team: SimpleNamespace, migrator: DB
) -> None:
    """Un 20 % solo para la lista AQUI: lo que pida más allá (otra lista que exista o no, todas,
    el rol Owner o un rol que no existe) es el mismo 403, antes de mirar las referencias."""
    here, other = price_list(team.org, "AQUI"), price_list(team.org, "OTRA")
    narrow = manager(team, "20", applies_to_price_list_id=here, currency="PEN")
    low = manager(team, "10")  # un 10 % para todas no rebaja al Administrador (100 %)
    capped = manager(team, "50", max_discount_amount=D(10), currency="PEN")  # tope en PEN
    before = (rows(migrator), audits(migrator))
    asks = [
        {"role_id": str(team.target), "price_list_id": str(other)},
        {"role_id": str(team.target), "price_list_id": str(uuid4())},
        {"role_id": str(team.target)},  # todas las listas
        {"role_id": str(code_of(team, "owner")), "price_list_id": str(uuid4())},
        {"role_id": str(uuid4()), "price_list_id": str(other)},
        {"role_id": str(team.target), "price_list_id": str(here), "max_discount_percent": "21"},
    ]
    for ask in asks:
        assert reply(put(narrow, **{"max_discount_percent": "5", **ask})) == DENIED, ask
    for extra in ({}, {"max_discount_amount": "5", "currency": "USD"}):  # sin tope; otra moneda
        for target in (uuid4(), code_of(team, "owner"), team.target):
            ask = {"role_id": str(target), "max_discount_percent": "5", **extra}
            assert reply(put(capped, **ask)) == DENIED, ask
    admin_role = str(code_of(team, "admin"))
    assert reply(put(low, role_id=admin_role, max_discount_percent="1", version=1)) == DENIED
    assert reply(put(low, role_id=admin_role, max_discount_percent="1", version=9)) == DENIED
    assert (rows(migrator), audits(migrator)) == before
    made = put(narrow, role_id=str(team.target), price_list_id=str(here), max_discount_percent="20")
    assert made.status_code == 201, made.content  # dentro de lo que tiene, sí


def test_a_version_to_change_one_policy_per_role_and_list_and_the_same_body_twice(
    team: SimpleNamespace, migrator: DB
) -> None:
    admin = verified(team.x)
    body = {"role_id": str(team.target), "max_discount_percent": "5"}
    assert put(admin, **body).status_code == 201
    taken = put(admin, **body)  # sin versión no se pisa
    assert (taken.status_code, taken.json()["code"]) == (409, "DISCOUNT_POLICY_EXISTS")
    stale = put(admin, **body, version=2)
    assert (stale.status_code, stale.json()["code"]) == (409, "STALE_VERSION")
    gone = put(admin, role_id=str(role(team.org)), max_discount_percent="5", version=1)
    assert (gone.status_code, gone.json()["code"]) == (409, "STALE_VERSION")  # no existe
    count = audits(migrator)
    same = [put(admin, **body, version=1) for _ in "ab"]  # lo mismo dos veces: idempotente
    assert [(s.status_code, s.json()["version"]) for s in same] == [(200, 1), (200, 1)]
    assert same[0].content == same[1].content and audits(migrator) == count


def test_two_changes_with_the_same_version_at_once_one_wins_and_one_audit_row(
    team: SimpleNamespace, migrator: DB
) -> None:
    admin, owner = verified(team.x), verified(team.a)
    assert put(admin, role_id=str(team.target), max_discount_percent="5").status_code == 201
    count = audits(migrator)
    outcome = at_once(
        lambda: put(admin, role_id=str(team.target), max_discount_percent="6", version=1),
        lambda: put(owner, role_id=str(team.target), max_discount_percent="7", version=1),
    )
    assert sorted((status, why) for status, _, why in outcome) == [
        (200, None),
        (409, "STALE_VERSION"),
    ]
    assert audits(migrator) == count + 1
    fresh = str(role(team.org))  # dos creaciones a la vez: una fila y un 409 limpio
    made = at_once(*[lambda: put(admin, role_id=fresh, max_discount_percent="1")] * 2)
    assert sorted((status, why) for status, _, why in made) == [
        (201, None),
        (409, "DISCOUNT_POLICY_EXISTS"),
    ]
    assert audits(migrator) == count + 2


def test_organizations_do_not_see_or_touch_each_others_policies(
    team: SimpleNamespace, migrator: DB
) -> None:
    other = role(team.b)
    with tenant_scope(ctx(team.b)):
        foreign = DiscountPolicy.objects.create(role_id=other, max_discount_percent=D("50"))
    ids = {row["id"] for row in listed(team.x).json()["results"]}
    assert str(foreign.pk) not in ids and len(ids) == 3
    answer = put(verified(team.x), role_id=str(other), max_discount_percent="1", version=1)
    assert answer.status_code == 400  # su rol no existe aquí: ni 409 ni su versión
    assert reply(listed(team.x, "org-b")) == NOT_FOUND  # no es miembro de B
    assert migrator.execute(
        "SELECT max_discount_percent, version FROM discount_policies WHERE id = %s", [foreign.pk]
    ).fetchone() == (D("50.00"), 1)


def test_a_missing_row_on_a_tenant_route_is_the_usual_404_and_a_quote_refusal_a_bare_403(
    team: SimpleNamespace, caplog: pytest.LogCaptureFixture
) -> None:
    """`core.api.errors`: `DoesNotExist` de un comando es el 404 de siempre, solo dentro de una
    ruta de tenant (fuera sigue siendo un fallo); `NotAllowed` de las cotizaciones, un 403 sin
    motivo. Otra ruta de tenant no cambia su 404."""
    caplog.set_level(logging.INFO, logger="core.api.errors")
    secret = str(uuid4())  # un id en el texto de la excepción no llega al registro
    with tenant_scope(ctx(team.org)):
        missing = exception_handler(Quote.DoesNotExist(secret), {"view": QuoteView()})
        refused = exception_handler(NotAllowed("quotes.approve"), {})
    assert missing is not None and refused is not None
    assert (missing.status_code, missing.data) == (404, {"code": "NOT_FOUND"})
    (logged,) = [r for r in caplog.records if r.name == "core.api.errors"]  # una línea, sin traza
    assert (logged.levelname, logged.exc_info) == ("INFO", None)
    assert logged.getMessage() == "404 por DoesNotExist en QuoteView" and secret not in caplog.text
    assert (refused.status_code, refused.data) == (403, {"code": "PERMISSION_DENIED"})
    assert exception_handler(Quote.DoesNotExist(), {}) is None  # sin tenant: 500, sin detalle
    assert reply(team.a.get(f"/api/v1/o/org-a/quotes/{uuid4()}/")) == NOT_FOUND
