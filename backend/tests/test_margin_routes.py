"""F3-46d: `…/pricing/price-lists/{id}/margins/`, la única vista que junta precio y costo. Con
step-up, sesión, motor de autorización y PostgreSQL con el rol `crm_app`."""

from datetime import timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import get_resolver
from django.utils import timezone

from apps.access.checks import unenforced
from apps.audit.selectors import entries
from apps.pricing.api import costs as cost_api
from apps.pricing.api.margins import MarginsView, margin
from apps.pricing.models import PriceList, VariantCost
from core.api.step_up import MFA_AT
from core.tenancy.scope import tenant_scope
from tests import test_cost_routes
from tests.test_cost_routes import SEE, cost_rows, custom, stepped, team, verified
from tests.test_cost_routes import put as put_cost
from tests.test_member_branch import DENIED
from tests.test_memberships import ctx
from tests.test_prices import price_list
from tests.test_prices_api import put as put_price
from tests.test_pricing_engine import item
from tests.test_promotion_tables import promo
from tests.test_self_context import NOT_FOUND, reply

shop, real_stack = test_cost_routes.shop, test_cost_routes.real_stack
assert team  # las fixtures, de las rutas de costos
pytestmark = pytest.mark.usefixtures("tenant_db")
ROW = {"variant", "price", "cost", "margin", "reason"}
GRANTS: dict[str, str | None] = {**SEE, "prices.view": None}


def url(list_id: Any, org: str = "org-a") -> str:
    return f"/api/v1/o/{org}/pricing/price-lists/{list_id}/margins/"


def margins(client: Client, list_id: Any, **query: Any) -> Any:
    response = client.get(url(list_id), query)
    assert response.status_code == 200, response.content
    return response.json()


def priced(team: Any, listed: Any, sku: str, price: str | None, cost: str | None) -> Any:
    made = item(team.org, sku)
    if price:
        assert put_price(team.a, listed, made, price).status_code == 201
    if cost:
        assert put_cost(verified(team.a), made, amount=cost).status_code == 201
    return made


def stored_cost(team: Any, variant: Any, **fields: Any) -> None:
    with tenant_scope(ctx(team.org)):
        VariantCost.objects.create(
            variant_id=variant,
            amount=Decimal("100"),
            starts_at=timezone.now() - timedelta(days=1),
            source="IMPORT",
            change_reason="Semilla del test",
            **{"currency": "PEN", "tax_included": True, **fields},
        )


def test_the_acceptance_case_899_is_20_4_percent_of_4399(
    team: Any, migrator: psycopg.Connection[Any]
) -> None:
    listed = price_list(team.org, is_default=True)
    sku = priced(team, listed, "IP16", "4399", "3500")
    body = margins(verified(team.a), listed)
    assert body["next"] is None and len(body["results"]) == 1
    row = body["results"][0]
    assert set(row) == ROW and row["variant"]["id"] == str(sku)
    assert row["price"] == {"amount": "4399.00", "currency": "PEN", "tax_included": True}
    assert row["cost"] == {"amount": "3500.00", "currency": "PEN", "tax_included": True}
    assert row["margin"] == {"amount": "899.00", "percent": "20.4"} and row["reason"] is None
    assert [margin(Decimal(p), Decimal(c))["percent"] for p, c in (("3", "2"), ("7", "6"))] == [
        "33.3",
        "14.3",
    ]
    assert margin(Decimal("100"), Decimal("120")) == {"amount": "-20.00", "percent": "-20.0"}
    assert margin(Decimal("200.50"), Decimal("100")) == {"amount": "100.50", "percent": "50.1"}


def test_rows_without_a_margin_say_why_and_a_promotion_moves_the_price(team: Any) -> None:
    listed = price_list(team.org, is_default=True)
    ok = priced(team, listed, "OK", "4399", "3500")
    no_cost = priced(team, listed, "NOCOST", "100", None)
    no_price = priced(team, listed, "NOPRICE", None, "50")
    away = item(team.org, "AWAY", status="INACTIVE")
    assert put_price(team.a, listed, away, "10").status_code == 201
    usd, tax = item(team.org, "USD"), item(team.org, "TAX")
    for variant, extra in ((usd, {"currency": "USD"}), (tax, {"tax_included": False})):
        assert put_price(team.a, listed, variant, "300").status_code == 201
        stored_cost(team, variant, **extra)
    rows = {r["variant"]["sku"]: r for r in margins(verified(team.a), listed)["results"]}
    why = {sku: (r["reason"], r["margin"]) for sku, r in rows.items()}
    assert why == {
        "OK": (None, {"amount": "899.00", "percent": "20.4"}),
        "NOCOST": ("NO_COST", None),
        "NOPRICE": ("NO_PRICE", None),
        "AWAY": ("VARIANT_UNAVAILABLE", None),
        "USD": ("CURRENCY_MISMATCH", None),
        "TAX": ("TAX_MISMATCH", None),
    }
    assert rows["NOCOST"]["cost"] is None and rows["NOCOST"]["price"]["amount"] == "100.00"
    assert rows["NOPRICE"]["price"] is None and rows["NOPRICE"]["cost"]["amount"] == "50.00"
    assert rows["USD"]["cost"]["currency"] == "USD" and rows["AWAY"]["price"] is None
    assert rows["TAX"]["cost"]["tax_included"] is False
    now = timezone.now()
    promo(team.org, [listed], [{"variant_id": ok}], value="10", starts=now - timedelta(days=1),
          ends=now + timedelta(days=5))  # fmt: skip
    again = {r["variant"]["sku"]: r for r in margins(verified(team.a), listed)["results"]}
    assert again["OK"]["price"]["amount"] == "3959.10"  # lo que paga el cliente
    assert again["OK"]["margin"] == {"amount": "459.10", "percent": "11.6"}
    assert again["NOCOST"]["margin"] is None and no_cost and no_price


def test_the_matrix_permissions_step_up_visibility_and_tenants(team: Any) -> None:
    listed = price_list(team.org, is_default=True)
    wholesale = price_list(team.org, "MAYO", required_permission="prices.view_wholesale")
    off = price_list(team.org, "OFF")
    priced(team, listed, "IP16", "4399", "3500")
    session = team.a.session  # `priced` verificó al Owner: se le quita el segundo factor
    del session[MFA_AT]
    session.save()
    assert stepped(team.a.get(url(listed)))  # el Owner sin segundo factor
    verified(team.a, 15 * 60 + 60)
    assert stepped(team.a.get(url(listed)))  # vencido
    verified(team.a, 60)
    assert team.a.get(url(listed)).status_code == 200
    for client in (team.x, verified(team.x), team.v, verified(team.v), verified(team.s)):
        assert reply(client.get(url(listed))) == DENIED  # sin `product_cost.view`
    for n, lacks in enumerate(("product_cost.view", "prices.view", "products.view")):
        grants = {k: v for k, v in GRANTS.items() if k != lacks}
        client = verified(custom(team, f"lacks{n}@example.com", grants))
        assert reply(client.get(url(listed))) == DENIED
    allowed = verified(custom(team, "ok@example.com", GRANTS))
    assert allowed.get(url(listed)).status_code == 200
    assert stepped(custom(team, "mfa@example.com", GRANTS).get(url(listed)))  # sin MFA
    assert Client().get(url(listed)).status_code == 401
    assert reply(allowed.get(url(wholesale))) == NOT_FOUND  # no la ve
    assert verified(team.a).get(url(wholesale)).status_code == 200
    assert reply(allowed.get(url(uuid4()))) == NOT_FOUND
    assert reply(verified(team.a).get(url(listed, "org-b"))) == NOT_FOUND
    with tenant_scope(ctx(team.org)):
        PriceList.objects.filter(pk=off).update(is_active=False)
    assert reply(verified(team.a).get(url(off))) == NOT_FOUND
    assert allowed.get(url(listed), {"limit": 0}).status_code == 400
    assert allowed.get(url(listed), {"product_id": "x", "extra": "1"}).status_code == 400
    assert allowed.get(url(listed), {"extra": "1"}).status_code == 200  # lo desconocido se ignora


def test_pages_by_cursor_and_a_constant_number_of_queries(
    team: Any, migrator: psycopg.Connection[Any]
) -> None:
    listed = price_list(team.org, is_default=True)
    owner = verified(team.a)
    made = [priced(team, listed, f"V{n:02}", "100", "60") for n in range(2)]

    def queries() -> int:
        margins(owner, listed)  # la primera lectura de la sesión audita; esta ya no
        with CaptureQueriesContext(connection) as captured:
            margins(owner, listed, limit=100)
        return len(captured)

    two = queries()
    made += [priced(team, listed, f"W{n:02}", "100", "60") for n in range(48)]
    assert queries() == two
    walked, cursor = [], None
    while True:
        page = margins(owner, listed, limit=20, **({"cursor": cursor} if cursor else {}))
        walked += [r["variant"]["id"] for r in page["results"]]
        if not (cursor := page["next"]):
            break
    assert walked == [str(v) for v in made] and len(set(walked)) == 50
    assert owner.get(url(listed), {"cursor": "x"}).status_code == 400
    rows = [r for r in cost_rows(migrator) if r[0] == "cost.viewed"]
    assert [r[1] for r in rows] == ["price_list"] and rows[0][2:] == (
        {},
        {},
    )  # una vez, sin importes


def test_the_audit_of_a_read_stays_under_the_cost_prefix_and_is_hidden_by_default(
    team: Any, monkeypatch: Any, migrator: psycopg.Connection[Any]
) -> None:
    listed = price_list(team.org, is_default=True)
    priced(team, listed, "IP16", "4399", "3500")
    margins(verified(team.a), listed)
    with tenant_scope(ctx(team.org)):
        assert not entries().filter(action__startswith="cost.").exists()
        assert not entries().filter(entity_type="variant_cost").exists()
        assert entries(with_costs=True).filter(action="cost.viewed").count() == 1
    monkeypatch.setattr(cost_api, "SEEN_MAX", 1)
    owner = verified(team.a)
    other = priced(team, listed, "ZZ", "10", "5")  # otra entidad: la lista ya ocupa el único lugar
    for _ in range(3):
        assert owner.get(f"/api/v1/o/org-a/pricing/variants/{other}/costs/").status_code == 200
    kinds = [r[1] for r in cost_rows(migrator) if r[0] == "cost.viewed"]
    assert kinds.count("variant") == 3 and len(owner.session[cost_api.SEEN]) == 1


def test_the_step_up_check_stays_green_and_the_schema_gates_margins() -> None:
    assert MarginsView.step_up_methods == {"GET"} and MarginsView not in unenforced(
        get_resolver().url_patterns
    )
