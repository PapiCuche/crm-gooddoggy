"""F3-44: las rutas de los precios de una lista (`…/price-lists/{id}/prices/`). Middleware, sesión,
motor de autorización y PostgreSQL con el rol `crm_app`. Las vigencias y la concurrencia se prueban
en el servicio (`test_prices`) y el motor en `test_pricing_engine`."""

from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.accounts.models import User
from apps.pricing import prices
from tests import test_price_lists_api, test_self_context
from tests.factories import make_user
from tests.test_authorization import give
from tests.test_member_branch import DENIED
from tests.test_memberships import join
from tests.test_prices import price_list
from tests.test_pricing_engine import item
from tests.test_products import send
from tests.test_self_context import NOT_FOUND, reply, signed

shop, real_stack = test_price_lists_api.shop, test_self_context.real_stack
pytestmark = pytest.mark.usefixtures("tenant_db")
PRICE = "id variant_id price_list_id amount previous_amount currency tax_included starts_at ends_at"
PRICE += " change_reason created_at created_by_user_id"
ROW = {
    "variant": {"id", "sku", "name", "product_id", "product_name"},
    "current": {"price_id", "amount", "currency", "tax_included", "starts_at", "ends_at"},
    "next": {"price_id", "amount", "starts_at"},
    "resolution": {"found", "reason", "final_amount", "valid_until"},
}
MOTIVE = "Margen interno 7x"  # un motivo que ningún error debe repetir


@pytest.fixture
def team(shop: SimpleNamespace, orgs: dict[str, UUID]) -> SimpleNamespace:
    return SimpleNamespace(**vars(shop), org=orgs["A"])


def url(list_id: Any, org: str = "org-a") -> str:
    return f"/api/v1/o/{org}/pricing/price-lists/{list_id}/prices/"


def put(client: Client, list_id: Any, variant: Any, amount: Any, **body: Any) -> Any:
    wanted = {"variant_id": str(variant), "amount": amount, "reason": MOTIVE, **body}
    return send(client, "post", url(list_id), wanted)


def page(client: Client, list_id: Any, **query: Any) -> Any:
    response = client.get(url(list_id), query)
    assert response.status_code == 200, response.content
    return response.json()


def no_cost(body: Any) -> None:
    """ADR-006, Security implications: ninguna clave de costo ni de margen, a ninguna altura."""
    if isinstance(body, dict):
        assert not [k for k in body if "cost" in k or "margin" in k], body
    for value in (
        body.values() if isinstance(body, dict) else body if isinstance(body, list) else []
    ):
        no_cost(value)


def test_set_and_schedule_then_a_seller_reads_regular_but_cannot_set(team: Any) -> None:
    """La aceptación de F3-44 por HTTP (el historial, en F3-44b)."""
    regular = price_list(team.org, is_default=True)
    wholesale = price_list(team.org, "MAYO", required_permission="prices.view_wholesale")
    sku, tomorrow = item(team.org), (timezone.now() + timedelta(days=1)).isoformat()
    now = put(team.a, regular, sku, "4599")
    assert now.status_code == 201, now.content
    later = put(team.a, regular, sku, "4399.5", starts_at=tomorrow, reason="Campaña de octubre")
    assert later.status_code == 201, later.content
    farther = (timezone.now() + timedelta(days=2)).isoformat()  # `next` es el primero de los dos
    assert put(team.a, regular, sku, "4299", starts_at=farther).status_code == 201
    owner = User.objects.get(email="a@example.com").pk
    for made in (now.json(), later.json()):
        assert set(made) == set(PRICE.split()) and made["created_by_user_id"] == str(owner)
        no_cost(made)
    first = [now.json()[k] for k in ("amount", "previous_amount", "change_reason")]
    assert first == ["4599.00", None, MOTIVE]
    assert [later.json()[k] for k in ("amount", "previous_amount")] == ["4399.50", "4599.00"]
    rows = page(team.a, regular)
    assert rows["next"] is None and len(rows["results"]) == 1
    row = rows["results"][0]
    assert {k: set(v) for k, v in row.items()} == ROW
    no_cost(rows)
    assert row["current"]["amount"] == row["resolution"]["final_amount"] == "4599.00"
    ends, starts = row["current"]["ends_at"], later.json()["starts_at"]
    assert ends == row["resolution"]["valid_until"] == starts
    assert row["next"] == {"price_id": later.json()["id"], "amount": "4399.50", "starts_at": starts}
    assert row["variant"]["product_name"] == row["variant"]["sku"] == "IP16"
    assert page(team.v, regular) == rows  # la Vendedora lee la Regular, vigente y final
    assert reply(team.v.get(url(wholesale))) == NOT_FOUND  # la mayorista no existe para ella
    blind = make_user(email="pv@example.com")  # ve precios pero no el catálogo
    give(team.org, join(team.org, blind).pk, {"prices.view": None})
    assert reply(signed(blind).get(url(regular))) == DENIED
    for client in (team.v, team.s):  # sin `prices.manage` no fija precios
        assert reply(put(client, regular, sku, "1")) == DENIED
    assert put(team.x, wholesale, sku, "3999").status_code == 201  # el Administrador sí


def test_what_the_engine_says_beside_the_rows_and_the_product_filter(team: Any) -> None:
    regular = price_list(team.org, is_default=True)
    off, bare = item(team.org, "OFF", status="INACTIVE"), item(team.org, "BARE")
    gone = item(team.org, "GONE", deleted="variant")
    assert put(team.a, regular, off, "10").status_code == 201
    rows = {r["variant"]["sku"]: r for r in page(team.a, regular)["results"]}
    assert list(rows) == ["OFF", "BARE"]  # la borrada no sale
    assert rows["OFF"]["current"]["amount"] == "10.00"  # el precio sigue en la lista
    unavailable = {"found": False, "reason": "VARIANT_UNAVAILABLE"}
    assert rows["OFF"]["resolution"] == unavailable | {"final_amount": None, "valid_until": None}
    assert rows["BARE"]["current"] is None and rows["BARE"]["next"] is None
    assert rows["BARE"]["resolution"]["reason"] == "NO_PRICE"
    product = rows["BARE"]["variant"]["product_id"]
    only = page(team.a, regular, product_id=product)["results"]
    assert [r["variant"]["id"] for r in only] == [str(bare)]
    assert page(team.a, regular, product_id=uuid4())["results"] == []
    refused = team.a.get(url(regular), {"product_id": "x"})
    assert refused.status_code == 400 and set(refused.json()["fields"]) == {"product_id"}
    first = page(team.a, regular, limit=1)
    assert [r["variant"]["sku"] for r in first["results"]] == ["OFF"] and first["next"]
    second = page(team.a, regular, limit=1, cursor=first["next"])
    assert [r["variant"]["sku"] for r in second["results"]] == ["BARE"] and not second["next"]
    assert reply(put(team.a, regular, gone, "5")) == NOT_FOUND


def test_a_page_costs_the_same_queries_with_one_row_or_many(team: Any) -> None:
    regular, tomorrow = price_list(team.org, is_default=True), timezone.now() + timedelta(days=1)

    def queries() -> int:
        with CaptureQueriesContext(connection) as captured:
            page(team.v, regular)
        return len(captured)

    def priced(sku: str) -> None:
        made = item(team.org, sku)
        assert put(team.a, regular, made, "100").status_code == 201
        assert put(team.a, regular, made, "90", starts_at=tomorrow.isoformat()).status_code == 201

    priced("ONE")
    one = queries()
    for index in range(6):
        priced(f"SKU{index}")
    item(team.org, "OFF", status="INACTIVE")
    assert queries() == one
    assert len(page(team.v, regular)["results"]) == 8


def test_the_errors_of_the_contract(team: Any, monkeypatch: Any) -> None:
    regular, sku = price_list(team.org, is_default=True), item(team.org)
    past = (timezone.now() - timedelta(hours=1)).isoformat()
    # 4399.5 es un número JSON: ya perdió los céntimos antes de llegar
    amounts: list[dict[str, Any]] = [
        {"amount": a} for a in (4399.5, "4399.999", "0", "-1", "1e3", " 4399 ", "4399\n")
    ]
    reasons = [{"reason": r} for r in ("<b>Oferta</b>", "ab")]
    starts = [{"starts_at": t} for t in (past, "2030-01-01T00:00:00")]  # pasado, sin zona
    for bad in [*amounts, *reasons, *starts, {"variant_id": "x"}]:
        refused = put(team.a, regular, sku, **{"amount": "10", **bad})
        assert refused.status_code == 400 and set(refused.json()["fields"]) == set(bad), bad
        assert MOTIVE.encode() not in refused.content and b"Oferta" not in refused.content
    missing = send(team.a, "post", url(regular), {})
    assert set(missing.json()["fields"]) == {"variant_id", "amount", "reason"}
    later = (timezone.now() + timedelta(days=2)).isoformat()
    assert put(team.a, regular, sku, "10").status_code == 201
    assert put(team.a, regular, sku, "12", starts_at=later).status_code == 201
    for amount, extra in (("10.00", {}), ("13", {"starts_at": later})):
        response = put(team.a, regular, sku, amount, **extra)
        code = "PRICE_ALREADY_SCHEDULED" if extra else "PRICE_UNCHANGED"
        assert response.status_code == 409 and response.json()["code"] == code
        assert MOTIVE.encode() not in response.content

    def conflict(*args: Any, **kwargs: Any) -> Any:
        raise prices.PriceConflict(sku)

    monkeypatch.setattr(prices, "set_price", conflict)
    response = put(team.a, regular, sku, "14")
    assert response.status_code == 409 and response.json()["code"] == "PRICE_CONFLICT"


def test_what_is_not_there_or_not_seen_is_a_404(team: Any) -> None:
    regular, off = price_list(team.org, is_default=True), price_list(team.org, "X", is_active=False)
    foreign_list = price_list(team.b, is_default=True)
    internal = price_list(team.org, "INTERNA", required_permission="prices.view_internal")
    # Sin variantes aún: el 404 lo da la ruta, no el motor. La inactiva no se lee ni se le fijan
    # precios, como en `set_price`.
    for client, list_id in ((team.a, foreign_list), (team.a, uuid4()), (team.a, off)):
        assert reply(client.get(url(list_id))) == NOT_FOUND
    assert reply(team.s.get(url(internal))) == NOT_FOUND
    sku, foreign_sku = item(team.org), item(team.b, "AJENO")
    for list_id, variant in ((foreign_list, sku), (off, sku), (regular, foreign_sku)):
        assert reply(put(team.a, list_id, variant, "5")) == NOT_FOUND
    assert reply(put(team.a, regular, uuid4(), "5")) == NOT_FOUND
