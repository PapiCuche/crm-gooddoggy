"""F3-46c: `…/pricing/variants/{id}/costs/`, con step-up, y la auditoría de costos que no se filtra.
Middleware, sesión, motor de autorización y PostgreSQL con el rol `crm_app`."""

import time
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from django.core.management import call_command
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import get_resolver
from drf_spectacular.generators import SchemaGenerator

from apps.access.checks import unenforced
from apps.accounts.models import User
from apps.audit.services import Entity, record
from core.api import step_up
from core.api.step_up import MAX_AGE, MFA_AT
from core.tenancy.scope import tenant_scope
from tests import test_price_lists_api, test_self_context
from tests.factories import make_user, sign_in
from tests.test_authorization import give
from tests.test_member_branch import DENIED
from tests.test_memberships import ctx, join
from tests.test_pricing_engine import item
from tests.test_products import send
from tests.test_self_context import NOT_FOUND, reply, signed

shop, real_stack = test_price_lists_api.shop, test_self_context.real_stack
pytestmark = pytest.mark.usefixtures("tenant_db")
STEP_UP = (403, step_up.MESSAGE)
COST = "id amount currency tax_included source change_reason starts_at ends_at created_by_user_id"
WHY = "Compra al por mayor"  # un motivo que ningún error debe repetir
BASE = {"amount": "3500", "reason": WHY}


@pytest.fixture
def team(shop: SimpleNamespace, orgs: Any) -> SimpleNamespace:
    return SimpleNamespace(**vars(shop), org=orgs["A"])


def url(variant: Any, org: str = "org-a") -> str:
    return f"/api/v1/o/{org}/pricing/variants/{variant}/costs/"


def verified(client: Client, ago: float = 0) -> Client:
    """Segundo factor verificado hace `ago` segundos (la sesión, no la cabecera)."""
    session = client.session
    session[MFA_AT] = int(time.time() - ago)
    session.save()
    return client


def put(client: Client, variant: Any, **body: Any) -> Any:
    return send(client, "post", url(variant), {**BASE, **body})


def got(client: Client, variant: Any, **query: Any) -> Any:
    response = client.get(url(variant), query)
    assert response.status_code == 200, response.content
    return response.json()


def stepped(reply_: Any) -> bool:
    body = reply_.json()
    return bool(
        reply_.status_code == 403
        and body == {"code": "STEP_UP_REQUIRED", "message": step_up.MESSAGE}
    )


def cost_rows(migrator: psycopg.Connection[Any]) -> list[tuple[Any, ...]]:
    query = "SELECT action, entity_type, changes, metadata FROM audit_logs "
    query += "WHERE action LIKE 'cost.%' ORDER BY id"
    return list(migrator.execute(query).fetchall())


def stored(migrator: psycopg.Connection[Any]) -> int:
    return int(migrator.execute("SELECT count(*) FROM variant_costs").fetchone()[0])  # type: ignore[index]


def custom(team: Any, email: str, grants: dict[str, str | None]) -> Client:
    user = make_user(email=email)
    give(team.org, join(team.org, user).pk, grants)
    return signed(user)


SEE: dict[str, str | None] = {"product_cost.view": None, "products.view": None}


def test_the_acceptance_case_owner_sets_three_thousand_five_hundred_and_reads_it(
    team: Any, migrator: psycopg.Connection[Any]
) -> None:
    sku = item(team.org)
    owner = verified(team.a)
    made = put(owner, sku)
    assert made.status_code == 201, made.content
    body = made.json()
    assert set(body) == set(COST.split()) and body["amount"] == "3500.00"
    assert (body["currency"], body["tax_included"], body["source"]) == ("PEN", True, "MANUAL")
    assert body["change_reason"] == WHY and body["ends_at"] is None
    assert body["created_by_user_id"] == str(User.objects.get(email="a@example.com").pk)
    page = got(owner, sku)
    assert page == {"current": body, "results": [body], "next": None}
    assert got(owner, sku) == page  # otra lectura de la misma sesión
    rows = cost_rows(migrator)
    assert [r[:2] for r in rows] == [("cost.changed", "variant_cost"), ("cost.viewed", "variant")]
    assert rows[0][2] == {"amount": [None, "3500.00"]} and rows[1][2:] == ({}, {})  # sin importes
    other = item(team.org, "OTRA")
    got(owner, other)  # otra variante: otra fila
    again = Client()
    sign_in(again, User.objects.get(email="a@example.com"))
    got(verified(again), sku)  # otra sesión: otra fila
    assert [a for a, *_ in cost_rows(migrator)].count("cost.viewed") == 3
    changed = put(owner, sku, amount="3600.5", reason="Subió el proveedor")
    assert changed.status_code == 201
    page = got(owner, sku)
    assert [r["amount"] for r in page["results"]] == ["3600.50", "3500.00"]
    assert page["current"]["amount"] == "3600.50" and page["results"][1]["ends_at"]
    assert [a for a, *_ in cost_rows(migrator)].count("cost.viewed") == 3  # misma sesión


def test_the_matrix_step_up_permissions_and_anonymous(
    team: Any, migrator: psycopg.Connection[Any]
) -> None:
    sku = item(team.org)
    assert stepped(team.a.get(url(sku))) and stepped(put(team.a, sku))  # el Owner sin factor
    verified(team.a, MAX_AGE + 60)
    assert stepped(team.a.get(url(sku))) and stepped(put(team.a, sku))  # vencido
    verified(team.a, MAX_AGE - 30)
    assert team.a.get(url(sku)).status_code == 200  # dentro de la ventana
    for client in (team.x, verified(team.x)):  # el Administrador no tiene `product_cost.*`
        assert reply(client.get(url(sku))) == DENIED and reply(put(client, sku)) == DENIED
    for client in (team.v, verified(team.s)):
        assert reply(client.get(url(sku))) == DENIED and reply(put(client, sku)) == DENIED
    manage_only = verified(
        custom(team, "m@example.com", {"product_cost.manage": None, "products.view": None})
    )
    assert reply(put(manage_only, sku)) == DENIED and reply(manage_only.get(url(sku))) == DENIED
    both: dict[str, str | None] = {"product_cost.manage": None, "product_cost.view": None}
    blind = verified(custom(team, "b@example.com", both))  # sin `products.view`
    assert reply(put(blind, sku)) == DENIED and reply(blind.get(url(sku))) == DENIED
    viewer = verified(custom(team, "w@example.com", SEE))
    assert viewer.get(url(sku)).status_code == 200 and reply(put(viewer, sku)) == DENIED
    assert Client().get(url(sku)).status_code == 401 and Client().post(url(sku)).status_code == 401
    assert stored(migrator) == 0 and not [r for r in cost_rows(migrator) if r[0] == "cost.changed"]
    unfactored = custom(team, "n@example.com", {**SEE, "product_cost.manage": None})
    assert stepped(unfactored.get(url(sku))) and stepped(put(unfactored, sku))  # sin MFA
    assert stored(migrator) == 0


def test_what_is_not_there_is_the_same_404_for_every_reason(
    team: Any, migrator: psycopg.Connection[Any]
) -> None:
    mine, foreign = item(team.org), item(team.b, "AJENO")
    gone = item(team.org, "GONE", deleted="variant")
    owner = verified(team.a)
    for variant in (foreign, uuid4(), gone):
        assert reply(owner.get(url(variant))) == NOT_FOUND
        assert reply(put(owner, variant)) == NOT_FOUND
    assert reply(owner.get(url(mine, "org-b"))) == NOT_FOUND
    assert stored(migrator) == 0 and cost_rows(migrator) == []


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"amount": 3500}, "amount"),
        ({"amount": "0"}, "amount"),
        ({"amount": "-5"}, "amount"),
        ({"amount": "1.234"}, "amount"),
        ({"amount": "1e3"}, "amount"),
        ({"amount": " 3500 "}, "amount"),
        ({"amount": "1" * 13}, "amount"),
        ({"reason": 12345}, "reason"),
        ({"reason": "ab"}, "reason"),
        ({"reason": "x" * 501}, "reason"),
        ({"reason": "<b>costo</b>"}, "reason"),
        ({"reason": "costo‮txt"}, "reason"),
        ({"extra": 1}, "extra"),
        ({"currency": "USD"}, "currency"),
        ({"starts_at": "2030-01-01T00:00:00Z"}, "starts_at"),
    ],
)
def test_strict_inputs_are_a_400_in_their_field_and_write_nothing(
    team: Any, migrator: psycopg.Connection[Any], body: Any, field: str
) -> None:
    sku = item(team.org)
    response = put(verified(team.a), sku, **body)
    assert response.status_code == 400, response.content
    assert field in response.json()["fields"]
    assert WHY not in response.content.decode() and "3500" not in response.content.decode()
    assert stored(migrator) == 0 and cost_rows(migrator) == []


def test_a_conflict_has_a_stable_code_and_never_repeats_the_input(
    team: Any, migrator: psycopg.Connection[Any], monkeypatch: Any
) -> None:
    sku, owner = item(team.org), verified(team.a)
    assert put(owner, sku, amount="30").status_code == 201
    same = put(owner, sku, amount="30.00", reason="Otro motivo secreto")
    assert same.status_code == 409 and same.json()["code"] == "COST_UNCHANGED"
    assert "secreto" not in same.content.decode() and stored(migrator) == 1
    from apps.pricing import costs

    for error, status, code in (
        (costs.CostConflict(), 409, "COST_CONFLICT"),
        (costs.CostTooSoon(), 409, "COST_TOO_SOON"),
        (costs.CostRejected(), 400, "COST_REJECTED"),
    ):

        def boom(*a: Any, error: Exception = error, **k: Any) -> Any:
            raise error

        monkeypatch.setattr(costs, "set_cost", boom)
        failed = put(owner, sku, amount="31", reason="Motivo reservado")
        assert (failed.status_code, failed.json()["code"]) == (status, code)
        assert "reservado" not in failed.content.decode()


def test_the_history_is_paginated_newest_first_at_a_constant_cost(team: Any) -> None:
    sku, owner = item(team.org), verified(team.a)
    made = [put(owner, sku, amount=str(10 + n)).json()["id"] for n in range(5)]
    got(owner, sku)  # la primera lectura audita; las siguientes no

    def queries(**query: Any) -> int:
        with CaptureQueriesContext(connection) as captured:
            got(owner, sku, **query)
        return len(captured)

    assert queries(limit=1) == queries(limit=5) == queries()
    walked, cursor = [], None
    while True:
        body = got(owner, sku, limit=2, **({"cursor": cursor} if cursor else {}))
        assert body["current"]["id"] == made[-1] and len(body["results"]) <= 2
        walked += [r["id"] for r in body["results"]]
        if not (cursor := body["next"]):
            break
    assert walked == made[::-1]
    assert owner.get(url(sku), {"limit": 0}).status_code == 400
    assert owner.get(url(sku), {"cursor": "x"}).status_code == 400
    other = item(team.org, "OTRA")
    assert got(owner, other) == {"current": None, "results": [], "next": None}  # sin mezclar


def test_audit_readers_do_not_see_costs_without_product_cost_view(
    team: Any, migrator: psycopg.Connection[Any]
) -> None:
    sku, owner = item(team.org), verified(team.a)
    cost_id = put(owner, sku).json()["id"]
    got(owner, sku)
    with tenant_scope(ctx(team.org)):
        record(ctx(team.org), "widget.updated", Entity("widget", uuid4(), "W"), {"n": [1, 2]})
        # una acción sin el prefijo `cost.` sobre la entidad de costos también se oculta
        record(ctx(team.org), "stock.adjusted", Entity("variant_cost", uuid4()), {"n": [1, 2]})
    audit: dict[str, str | None] = {"audit.view": None}
    plain = verified(custom(team, "au@example.com", audit))
    both = custom(team, "ab@example.com", {**audit, "product_cost.view": None})
    owner_audit = verified(team.a)

    def read(client: Client, **query: Any) -> list[dict[str, Any]]:
        out, cursor = [], None
        while True:
            response = client.get(
                "/api/v1/o/org-a/audit/",
                {"limit": 1, **query, **({"cursor": cursor} if cursor else {})},
            )
            assert response.status_code == 200, response.content
            out += response.json()["results"]
            if not (cursor := response.json()["next"]):
                return out

    seen = read(plain)
    assert seen and all(
        r["entity_type"] != "variant_cost" and not r["action"].startswith("cost.") for r in seen
    )
    for query in ("action=cost.changed", "action=cost.viewed", "entity_type=variant_cost"):
        assert read(plain, **dict([query.split("=")])) == []
    assert read(plain, entity_id=cost_id) == []
    assert any(r["action"] == "widget.updated" for r in seen)
    stale = Client()  # el Owner, en otra sesión sin segundo factor
    sign_in(stale, User.objects.get(email="a@example.com"))
    for client in (both, stale):  # con el permiso, pero sin segundo factor reciente: tampoco
        assert not [r for r in read(client) if r["action"].startswith("cost.")]
    visible = read(verified(both))
    actions = [r["action"] for r in visible if r["action"].startswith("cost.")]
    assert sorted(actions) == ["cost.changed", "cost.viewed"]
    assert any(r["action"] == "stock.adjusted" for r in visible)
    assert not any(r["action"] == "stock.adjusted" for r in seen)
    changed = next(r for r in visible if r["action"] == "cost.changed")
    assert changed["changes"] == {"amount": [None, "3500.00"]} and changed["entity_id"] == cost_id
    assert changed["id"] in [r["id"] for r in read(owner_audit, entity_type="variant_cost")]
    assert len(cost_rows(migrator)) == 2


def test_the_openapi_contract_shows_costs_only_in_the_cost_routes() -> None:
    schema = SchemaGenerator().get_schema(public=True)
    components = schema["components"]["schemas"]

    def names(node: Any, seen: frozenset[str] = frozenset()) -> set[str]:
        found: set[str] = set()
        if isinstance(node, dict):
            if "$ref" in node:
                ref = node["$ref"].rsplit("/", 1)[-1]
                return found if ref in seen else names(components[ref], seen | {ref})
            for key, value in node.items():
                if key == "properties":
                    found |= set(value)
                if key == "name" and isinstance(value, str):
                    found.add(value)  # un parámetro
                found |= names(value, seen)
        elif isinstance(node, list):
            for value in node:
                found |= names(value, seen)
        return found

    exposing: dict[str, set[str]] = {}
    for path, operations in schema["paths"].items():
        for method, operation in operations.items():
            found = names(operation)
            exposing[f"{method} {path}"] = found
            leaked = {
                n
                for n in found
                if any(w in n.lower() for w in ("cost", "costo", "margin", "margen", "purchase"))
            }
            if path.endswith(("/costs/", "/margins/")):  # las únicas rutas que juntan o dan costos
                continue
            assert not leaked, (path, method, leaked)
            assert not path.endswith("/margins/")
    assert {k for k, v in exposing.items() if "change_reason" in v and k.endswith("/costs/")} == {
        "get /api/v1/o/{org_slug}/pricing/variants/{variant_id}/costs/",
        "post /api/v1/o/{org_slug}/pricing/variants/{variant_id}/costs/",
    }
    # Los precios también muestran su `change_reason` (el del precio, no el del costo).
    assert {
        k for k, v in exposing.items() if "change_reason" in v and not k.endswith("/costs/")
    } == {k for k in exposing if "price" in k and "change_reason" in exposing[k]}
    assert set(components["Cost"]["properties"]) == set(COST.split())
    margins = {k for k, v in exposing.items() if "margin" in v}
    assert margins == {"get /api/v1/o/{org_slug}/pricing/price-lists/{price_list_id}/margins/"}


def test_step_up_is_enforced_on_the_cost_view_and_the_check_stays_green() -> None:
    from apps.pricing.api.costs import CostsView

    assert CostsView.step_up_methods == {"GET", "POST"}
    assert CostsView not in unenforced(get_resolver().url_patterns)
    call_command("check")
