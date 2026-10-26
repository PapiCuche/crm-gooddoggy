"""F3-44b: el historial de precios de una variante (`…/pricing/variants/{id}/price-history/`).
Middleware, sesión, motor de autorización y PostgreSQL con el rol `crm_app`. Los precios se fijan
y programan por las rutas de F3-44 (`put`)."""

from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.access.selectors import has_permission
from apps.accounts.models import User
from apps.catalog.models import ProductVariant
from apps.pricing import selectors
from apps.pricing.models import PriceList
from core.tenancy.scope import tenant_scope
from tests import test_prices_api
from tests.factories import make_user
from tests.test_authorization import give
from tests.test_member_branch import DENIED
from tests.test_memberships import ctx, join
from tests.test_prices import price_list
from tests.test_prices_api import MOTIVE, PRICE, no_cost, put
from tests.test_pricing_engine import item
from tests.test_self_context import NOT_FOUND, reply, signed

shop, real_stack, team = test_prices_api.shop, test_prices_api.real_stack, test_prices_api.team
pytestmark = pytest.mark.usefixtures("tenant_db")
LATER = "Campaña de octubre"


def url(variant: Any, org: str = "org-a") -> str:
    return f"/api/v1/o/{org}/pricing/variants/{variant}/price-history/"


def history(client: Any, variant: Any, **query: Any) -> Any:
    response = client.get(url(variant), query)
    assert response.status_code == 200, response.content
    body = response.json()
    no_cost(body)
    return body


def days(n: int) -> str:
    return (timezone.now() + timedelta(days=n)).isoformat()


def made(response: Any) -> Any:
    assert response.status_code == 201, response.content
    return response.json()


def test_set_and_schedule_then_the_history_shows_both_but_not_to_a_seller(team: Any) -> None:
    """La aceptación de F3-44b, por las rutas de F3-44."""
    regular = price_list(team.org, is_default=True)
    wholesale = price_list(team.org, "MAYO", required_permission="prices.view_wholesale")
    sku = item(team.org)
    now = made(put(team.a, regular, sku, "4599"))
    later = made(put(team.a, regular, sku, "4399", starts_at=days(1), reason=LATER))
    rows = history(team.a, sku)
    closed = now | {"ends_at": later["starts_at"]}  # programar otro cerró el primero
    assert rows["next"] is None and rows["results"] == [later, closed]  # la fila del `POST`
    owner = str(User.objects.get(email="a@example.com").pk)
    for row in rows["results"]:
        assert set(row) == set(PRICE.split()) and row["created_by_user_id"] == owner
    seen = [[r[k] for k in ("amount", "previous_amount", "change_reason")] for r in rows["results"]]
    assert seen == [["4399.00", "4599.00", LATER], ["4599.00", None, MOTIVE]]
    other = made(put(team.x, wholesale, sku, "3999"))  # el Administrador, en otra lista
    assert [r["id"] for r in history(team.x, sku)["results"]] == [
        other["id"],
        later["id"],
        now["id"],
    ]
    assert history(team.a, sku, price_list_id=regular)["results"] == [later, closed]
    assert history(team.a, sku, price_list_id=wholesale)["results"] == [other]
    for client in (team.v, team.s):  # la Vendedora y el Supervisor no tienen `prices.manage`
        assert reply(client.get(url(sku))) == DENIED
    blind = make_user(email="pm@example.com")  # gestiona precios pero no ve el catálogo
    give(team.org, join(team.org, blind).pk, {"prices.manage": None})
    assert reply(signed(blind).get(url(sku))) == DENIED


def test_what_is_not_there_or_not_seen_is_a_404_and_a_deleted_variant_keeps_it(
    team: Any, monkeypatch: Any
) -> None:
    regular, foreign_list = price_list(team.org, is_default=True), price_list(team.b)
    wholesale = price_list(team.org, "MAYO", required_permission="prices.view_wholesale")
    gone_list = price_list(team.org, "VIEJA")
    sku, foreign_sku = item(team.org), item(team.b, "AJENO")
    kept = made(put(team.a, regular, sku, "100"))
    hidden = made(put(team.a, wholesale, sku, "90"))
    made(put(team.a, gone_list, sku, "80"))
    with tenant_scope(ctx(team.org)):  # una lista borrada no existe para nadie
        PriceList.objects.filter(pk=gone_list).update(deleted_at=timezone.now())
        PriceList.objects.filter(pk=wholesale).update(is_active=False)
    assert history(team.a, sku)["results"] == [hidden, kept]  # la inactiva sigue en el historial
    assert history(team.a, sku, price_list_id=wholesale)["results"] == [hidden]
    for list_id in (foreign_list, uuid4(), gone_list):
        assert reply(team.a.get(url(sku), {"price_list_id": str(list_id)})) == NOT_FOUND
    refused = team.a.get(url(sku), {"price_list_id": "x"})
    assert refused.status_code == 400 and set(refused.json()["fields"]) == {"price_list_id"}
    for variant in (foreign_sku, uuid4()):
        assert reply(team.a.get(url(variant))) == NOT_FOUND
    with tenant_scope(ctx(team.org)):
        ProductVariant.objects.filter(pk=sku).update(deleted_at=timezone.now())
    assert history(team.a, sku)["results"] == [hidden, kept]  # borrada, con su historial
    # Hoy `prices.manage` ve todas las listas; si dejara de verlas, la mayorista se ocultaría.
    monkeypatch.setattr(
        selectors,
        "has_permission",
        lambda e, code: code != selectors.MANAGE and has_permission(e, code),
    )
    narrow = make_user(email="pn@example.com")
    grants: dict[str, str | None] = {
        "prices.manage": None,
        "products.view": None,
        "prices.view": None,
    }
    give(team.org, join(team.org, narrow).pk, grants)
    assert history(signed(narrow), sku)["results"] == [kept]
    assert reply(signed(narrow).get(url(sku), {"price_list_id": str(wholesale)})) == NOT_FOUND


def test_newest_first_by_cursor_at_the_same_cost_for_one_row_or_many(team: Any) -> None:
    lists = [price_list(team.org, is_default=True)] + [
        price_list(team.org, f"L{n}") for n in range(3)
    ]
    sku, other = item(team.org), item(team.org, "OTRA")
    made(put(team.a, lists[0], other, "5"))  # de otra variante: no sale

    def queries(**query: Any) -> int:
        with CaptureQueriesContext(connection) as captured:
            history(team.a, sku, **query)
        return len(captured)

    ids = [made(put(team.a, lists[0], sku, "100"))["id"]]
    one, one_filtered = queries(), queries(price_list_id=lists[0])
    for at, list_id in enumerate(lists):
        ids.append(made(put(team.a, list_id, sku, "90", starts_at=days(at + 1)))["id"])
    ids.append(made(put(team.a, lists[1], sku, "70"))["id"])
    assert queries() == one and queries(price_list_id=lists[0]) == one_filtered
    newest = ids[::-1]
    assert [r["id"] for r in history(team.a, sku)["results"]] == newest == sorted(ids)[::-1]
    walked, cursor = [], None
    while True:
        query: dict[str, Any] = {"limit": 2} | ({"cursor": cursor} if cursor else {})
        body = history(team.a, sku, **query)
        walked += [UUID(r["id"]) for r in body["results"]]
        assert len(body["results"]) <= 2
        if not (cursor := body["next"]):
            break
    assert walked == [UUID(i) for i in newest]
