"""F5-09d2: el detalle de una cotización (`…/quotes/{id}/`): las líneas congeladas en orden, las
notas, el mismo 404 fuera de alcance y que nunca sale un costo ni un margen."""

import json
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from apps.quotes.models import Quote, QuoteItem
from core.tenancy.scope import tenant_scope
from tests.test_member_branch import DENIED
from tests.test_memberships import ctx
from tests.test_opportunities_tables import variant
from tests.test_quotes_read_api import QUOTES, UNAUTHENTICATED, board, crm, pipelines, real_stack
from tests.test_self_context import NOT_FOUND, reply, signed

__all__ = ["board", "crm", "pipelines", "real_stack"]
pytestmark = pytest.mark.usefixtures("tenant_db")
LINE = {
    "id", "sort_order", "variant_id", "product_name", "variant_name", "sku", "quantity",
    "list_unit_price", "promotion_id", "promotion_name", "unit_price", "discount_type",
    "discount_value", "discount_amount", "final_unit_price", "line_total", "currency",
}  # fmt: skip
_skus = iter(range(10_000))
TEXT = {"notes_to_customer", "internal_notes", "cancel_reason", "items"}


def add_line(org: UUID, quote: UUID, order: int, **over: Any) -> UUID:
    """Una línea de 2 unidades a 100,00, sin promoción, de una variante nueva."""
    with tenant_scope(ctx(org)):
        sku = f"S{order}X{next(_skus)}"
        fields: dict[str, Any] = {
            "quote_id": quote, "sort_order": order, "variant_id": variant(org, sku).pk,
            "product_name_snapshot": "iPhone", "variant_name_snapshot": "iPhone 16",
            "sku_snapshot": sku, "quantity": 2, "list_unit_price": Decimal("100"),
            "unit_price": Decimal("100"), "final_unit_price": Decimal("100"),
            "line_total": Decimal("200"), "currency": "PEN",
        }  # fmt: skip
        return QuoteItem.objects.create(**{**fields, **over}).pk  # type: ignore[no-any-return]


def detail(client: Client, quote: UUID) -> Any:
    return client.get(f"{QUOTES}{quote}/")


def test_the_detail_is_the_list_row_plus_the_text_and_the_lines(board: SimpleNamespace) -> None:
    pk = board.quotes["vera"]
    with tenant_scope(ctx(board.a)):
        Quote.objects.filter(pk=pk).update(notes_to_customer="Hola\nGracias", internal_notes="Ojo")
    response = detail(signed(board.ana), pk)
    assert response.status_code == 200
    row = response.json()
    listed = next(r for r in signed(board.ana).get(QUOTES).json()["results"] if r["id"] == str(pk))
    assert {k: v for k, v in row.items() if k not in TEXT} == listed
    assert row["notes_to_customer"] == "Hola\nGracias" and row["internal_notes"] == "Ojo"
    assert (row["cancel_reason"], row["items"]) == (None, [])


def test_the_lines_come_in_order_with_what_was_frozen(board: SimpleNamespace) -> None:
    pk, promo = board.quotes["vera"], uuid4()
    add_line(board.a, pk, 2, sku_snapshot="B-2", list_unit_price=Decimal("120"),
             promotion_id_snapshot=promo, promotion_name_snapshot="Promo",
             quantity=1, line_total=Decimal("100"))  # fmt: skip
    add_line(
        board.a, pk, 1, sku_snapshot="A-1", discount_type="PERCENT", discount_value=10,
        discount_amount=10, final_unit_price=Decimal("90"), line_total=Decimal("180"),
    )  # fmt: skip
    first, second = detail(signed(board.vera), pk).json()["items"]
    assert set(first) == LINE and [first["sort_order"], second["sort_order"]] == [1, 2]
    assert (first["sku"], first["quantity"], first["product_name"], first["variant_name"]) == (
        "A-1", 2, "iPhone", "iPhone 16",
    )  # fmt: skip
    assert (first["list_unit_price"], first["unit_price"], first["final_unit_price"]) == (
        "100.00", "100.00", "90.00",
    )  # fmt: skip
    assert (first["discount_type"], first["discount_value"], first["discount_amount"]) == (
        "PERCENT", "10.00", "10.00",
    )  # fmt: skip
    assert (first["line_total"], first["currency"], first["promotion_id"]) == (
        "180.00",
        "PEN",
        None,
    )
    assert (second["promotion_id"], second["promotion_name"]) == (str(promo), "Promo")
    assert second["list_unit_price"] == "120.00" and second["discount_type"] == "NONE"


def test_the_lines_of_a_quote_are_never_mixed_with_another(board: SimpleNamespace) -> None:
    add_line(board.a, board.quotes["vera"], 1)
    add_line(board.a, board.quotes["sam"], 1)
    add_line(board.a, board.quotes["sam"], 2)
    assert len(detail(signed(board.ana), board.quotes["vera"]).json()["items"]) == 1
    assert len(detail(signed(board.ana), board.quotes["sam"]).json()["items"]) == 2


def test_every_miss_is_the_same_404_and_the_permission_comes_first(board: SimpleNamespace) -> None:
    vera = signed(board.vera)
    misses = {
        reply(detail(vera, pk)) for pk in (board.quotes["sam"], board.quotes["b"], uuid4())
    }  # fuera de alcance, de B o inexistente: lo mismo
    assert misses == {NOT_FOUND}
    assert detail(signed(board.sam), board.quotes["vera"]).status_code == 200  # `TEAM`
    assert reply(detail(signed(board.bea), board.quotes["vera"])) == DENIED
    assert reply(detail(Client(), board.quotes["vera"])) == UNAUTHENTICATED
    assert reply(detail(signed(board.luis), board.quotes["vera"])) == NOT_FOUND  # no es de A


def test_the_detail_never_carries_a_cost_or_a_margin(board: SimpleNamespace) -> None:
    pk = board.quotes["vera"]
    add_line(board.a, pk, 1)
    body = json.dumps(detail(signed(board.ana), pk).json()).lower()
    for word in ("cost", "margin", "margen", "price_id"):
        assert word not in body, word


def test_the_detail_reads_its_lines_in_a_constant_number_of_queries(board: SimpleNamespace) -> None:
    ana, pk = signed(board.ana), board.quotes["vera"]

    def queries() -> int:
        with CaptureQueriesContext(connection) as seen:
            assert detail(ana, pk).status_code == 200
        return len(seen)

    add_line(board.a, pk, 1)
    one = queries()
    add_line(board.a, pk, 2)
    add_line(board.a, pk, 3)
    assert queries() == one  # las líneas, en una consulta aparte, sean 1 o 3
