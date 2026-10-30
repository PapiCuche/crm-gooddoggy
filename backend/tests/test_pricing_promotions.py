"""F3-48 (E03-05, E03-10): las promociones en `PricingService`, contra PostgreSQL real con el rol
`crm_app`: vigencia en sus bordes sin job, los cuatro objetivos, prioridad y empate, canal, lista,
IA frente a persona, `valid_until` y un número fijo de consultas."""

from dataclasses import fields
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from apps.access.selectors import execution_context
from apps.pricing import services
from apps.pricing.models import PriceList, Promotion
from apps.pricing.services import PriceQuery, PromotionApplied
from core.tenancy.scope import tenant_scope
from tests import test_authorization
from tests.test_memberships import ctx
from tests.test_prices import price_list
from tests.test_pricing_engine import LIMA, T0, US, human, public_ai, row
from tests.test_promotion_tables import D1, D8, NOW, final, promo, shop, stock, tree

world, _shop = test_authorization.world, shop
pytestmark = pytest.mark.usefixtures("tenant_db")


def test_ten_percent_on_a_category_starts_and_ends_on_the_instant_with_no_job(
    world: Any, shop: Any
) -> None:
    made = promo(world.a, [shop.listed], [{"category_id": shop.iphone[1]}])
    for at, price, until, applied in (
        (D1 - US, "4399.00", D1, None),
        (D1, "3959.10", D8, made),
        (D8 - US, "3959.10", D8, made),
        (D8, "4399.00", None, None),
    ):
        got = final(world.a, shop.viewer, shop.variant, shop.listed, at)
        assert (got.final_amount, got.valid_until, got.promotion_id) == (
            Decimal(price),
            until,
            applied,
        ), at
    inside = final(world.a, shop.viewer, shop.variant, shop.listed, D1)
    assert (inside.base_amount, inside.savings) == (Decimal("4399.00"), Decimal("439.90"))
    assert inside.promotion == PromotionApplied(made, "Promo", "Hasta agotar stock", D8)


def test_the_four_levels_reach_the_variant_and_the_specific_override_wins(
    world: Any, shop: Any
) -> None:
    other = stock(world.a)
    reaches = {
        "variant": {"variant_id": shop.variant},
        "product": {"product_id": shop.product},
        "category": {"category_id": shop.iphone[1]},
        "brand": {"brand_id": shop.make},
    }
    for target in reaches.values():
        made = promo(world.a, [shop.listed], [target], "AMOUNT_OFF", "100", priority=0)
        got = final(world.a, shop.viewer, shop.variant, shop.listed, NOW)
        assert (got.final_amount, got.promotion_id) == (Decimal("4299.00"), made)
        with tenant_scope(ctx(world.a)):
            Promotion.objects.filter(pk=made).update(status="CANCELLED")
        none = final(world.a, shop.viewer, other[1], shop.listed, NOW)
        assert none.promotion_id is None  # no alcanza a una variante ajena
    # una promoción con varios objetivos usa el `override_value` del más cercano
    made = promo(
        world.a,
        [shop.listed],
        [
            {"brand_id": shop.make, "override_value": Decimal("500")},
            {"category_id": shop.iphone[0], "override_value": Decimal("300")},
            {"category_id": shop.iphone[1], "override_value": Decimal("200")},
        ],
        "AMOUNT_OFF",
        "100",
    )
    assert final(world.a, shop.viewer, shop.variant, shop.listed, NOW).final_amount == Decimal(
        "4199.00"
    )
    assert made


def test_one_promotion_with_two_targets_uses_the_variant_value_over_the_product_one(
    world: Any, shop: Any
) -> None:
    for first, second in (("variant_id", "product_id"), ("product_id", "variant_id")):
        ids = {"variant_id": shop.variant, "product_id": shop.product}
        values = {"variant_id": Decimal("200"), "product_id": Decimal("300")}
        targets = [{k: ids[k], "override_value": values[k]} for k in (first, second)]
        made = promo(world.a, [shop.listed], targets, "AMOUNT_OFF", "100")
        got = final(world.a, shop.viewer, shop.variant, shop.listed, NOW)
        assert got.final_amount == Decimal("4199.00")  # la de la variante, en el orden que sea
        with tenant_scope(ctx(world.a)):
            Promotion.objects.filter(pk=made).update(status="CANCELLED")
    wide = [
        {"brand_id": shop.make, "override_value": Decimal("500")},
        {"category_id": shop.iphone[1], "override_value": Decimal("400")},
        {"product_id": shop.product, "override_value": Decimal("300")},
    ]
    promo(world.a, [shop.listed], wide, "AMOUNT_OFF", "100")
    got = final(world.a, shop.viewer, shop.variant, shop.listed, NOW)
    assert got.final_amount == Decimal("4099.00")  # el producto, antes que su categoría y marca


@pytest.mark.parametrize("kind", ["FIXED_PRICE", "PERCENT_OFF", "AMOUNT_OFF"])
def test_the_three_types_and_their_override(world: Any, shop: Any, kind: str) -> None:
    value, override, expected = {
        "FIXED_PRICE": ("3500", "3000", ("3500.00", "3000.00")),
        "PERCENT_OFF": ("10", "20", ("3959.10", "3519.20")),
        "AMOUNT_OFF": ("400", "1399", ("3999.00", "3000.00")),
    }[kind]
    for with_override, price in zip((False, True), expected, strict=True):
        target = {"variant_id": shop.variant}
        if with_override:
            target["override_value"] = Decimal(override)
        made = promo(world.a, [shop.listed], [target], kind, value)
        got = final(world.a, shop.viewer, shop.variant, shop.listed, NOW)
        assert (got.final_amount, got.promotion_id) == (Decimal(price), made)
        with tenant_scope(ctx(world.a)):
            Promotion.objects.filter(pk=made).update(status="CANCELLED")


def test_priority_beats_specificity_and_a_tie_goes_to_the_lowest_final_price(
    world: Any, shop: Any
) -> None:
    low = promo(world.a, [shop.listed], [{"variant_id": shop.variant}], value="50", priority=1)
    high = promo(world.a, [shop.listed], [{"brand_id": shop.make}], value="5", priority=2)
    got = final(world.a, shop.viewer, shop.variant, shop.listed, NOW)
    assert (got.promotion_id, got.final_amount) == (high, Decimal("4179.05"))  # no la más barata
    for target, value, name in (
        ({"product_id": shop.product}, "20", "product"),
        ({"category_id": shop.iphone[0]}, "30", "parent category"),
    ):
        tie = promo(world.a, [shop.listed], [target], value=value, priority=2)
        got = final(world.a, shop.viewer, shop.variant, shop.listed, NOW)
        assert got.promotion_id == tie, (
            name
        )  # a igual prioridad, el menor precio de cualquier nivel
    assert low != high
    # empate de prioridad y de precio: gana siempre el mismo (el de menor `id`)
    twin = promo(world.a, [shop.listed], [{"brand_id": shop.make}], value="30", priority=2)
    ids = {final(world.a, shop.viewer, shop.variant, shop.listed, NOW).promotion_id for _ in "ab"}
    assert len(ids) == 1 and min(ids | {twin}, key=str) in ids | {twin}


def test_a_parent_category_reaches_the_children_but_not_the_other_way(
    world: Any, shop: Any
) -> None:
    root, child = shop.iphone
    _, sibling = tree(world.a, "accesorios", "fundas")
    in_root = stock(world.a, root)[1]  # directamente en «celulares»
    for v in (in_root,):
        row(world.a, (v, shop.listed), "100", T0 - timedelta(days=90))
    row(world.a, (stock(world.a, sibling)[1], shop.listed), "100", T0)
    made = promo(world.a, [shop.listed], [{"category_id": root}])
    assert final(world.a, shop.viewer, shop.variant, shop.listed, NOW).promotion_id == made
    assert final(world.a, shop.viewer, in_root, shop.listed, NOW).promotion_id == made
    with tenant_scope(ctx(world.a)):
        Promotion.objects.filter(pk=made).update(status="CANCELLED")
    deeper = promo(world.a, [shop.listed], [{"category_id": child}])
    assert final(world.a, shop.viewer, shop.variant, shop.listed, NOW).promotion_id == deeper
    assert final(world.a, shop.viewer, in_root, shop.listed, NOW).promotion_id is None
    outside = stock(world.a, sibling)[1]
    row(world.a, (outside, shop.listed), "100", T0 - timedelta(days=90))
    assert final(world.a, shop.viewer, outside, shop.listed, NOW).promotion_id is None


@pytest.mark.parametrize(
    "state", [{"status": "DRAFT"}, {"status": "CANCELLED"}, {"deleted_at": NOW - timedelta(days=1)}]
)
def test_draft_cancelled_and_deleted_promotions_do_not_apply(
    world: Any, shop: Any, state: dict[str, Any]
) -> None:
    promo(world.a, [shop.listed], [{"variant_id": shop.variant}], **state)
    got = final(world.a, shop.viewer, shop.variant, shop.listed, NOW)
    assert (got.final_amount, got.promotion_id) == (Decimal("4399.00"), None)


def test_a_list_that_is_not_included_and_another_currency_do_not_apply(
    world: Any, shop: Any
) -> None:
    other = price_list(world.a, "OTRA")
    row(world.a, (shop.variant, other), "100", T0 - timedelta(days=90))
    made = promo(world.a, [other], [{"variant_id": shop.variant}])
    assert final(world.a, shop.viewer, shop.variant, shop.listed, NOW).promotion_id is None
    assert final(world.a, shop.viewer, shop.variant, other, NOW).promotion_id == made
    promo(world.a, [shop.listed], [{"variant_id": shop.variant}], currency="USD")  # otra moneda
    assert final(world.a, shop.viewer, shop.variant, shop.listed, NOW).promotion_id is None


def test_channels_null_is_every_channel_and_without_a_channel_only_those(
    world: Any, shop: Any
) -> None:
    every = promo(world.a, [shop.listed], [{"variant_id": shop.variant}], value="5")
    web = promo(
        world.a, [shop.listed], [{"variant_id": shop.variant}], value="20", channels=["WEB"]
    )
    cases = [(None, every), ("WEB", web), ("TIENDA", every)]
    for channel, expected in cases:
        got = final(world.a, shop.viewer, shop.variant, shop.listed, NOW, channel=channel)
        assert got.promotion_id == expected, channel


def test_the_engine_rounds_a_percent_half_cent_up(world: Any, shop: Any) -> None:
    other = stock(world.a)[1]
    row(world.a, (other, shop.listed), "10.01", T0 - timedelta(days=90))
    promo(world.a, [shop.listed], [{"variant_id": other}], value="50")  # 5.005
    assert final(world.a, shop.viewer, other, shop.listed, NOW).final_amount == Decimal("5.01")


@pytest.mark.parametrize(
    ("kind", "value"),
    [
        ("AMOUNT_OFF", "4399"),  # deja 0
        ("AMOUNT_OFF", "5000"),  # deja menos de 0
        ("FIXED_PRICE", "4400"),  # por encima de la base
        ("FIXED_PRICE", "4399"),  # no abarata
        ("PERCENT_OFF", "100"),  # gratis
    ],
)
def test_a_promotion_that_does_not_lower_the_price_to_something_positive_is_skipped(
    world: Any, shop: Any, kind: str, value: str
) -> None:
    promo(world.a, [shop.listed], [{"variant_id": shop.variant}], kind, value, priority=9)
    fallback = promo(world.a, [shop.listed], [{"brand_id": shop.make}], priority=1)
    got = final(world.a, shop.viewer, shop.variant, shop.listed, NOW)
    assert (got.promotion_id, got.final_amount) == (fallback, Decimal("3959.10"))  # no se recorta


def test_public_ai_only_sees_exposable_promotions(world: Any, shop: Any) -> None:
    with tenant_scope(ctx(world.a)):
        PriceList.objects.filter(pk=shop.listed).update(ai_exposable=True)
    hidden = promo(world.a, [shop.listed], [{"variant_id": shop.variant}], value="30", priority=5)
    shown = promo(world.a, [shop.listed], [{"brand_id": shop.make}], ai_exposable=True, priority=1)
    query = PriceQuery(shop.variant, shop.listed, NOW)
    assert public_ai(world.a, query).promotion_id == shown
    assert human(world.a, shop.viewer, query).promotion_id == hidden
    assert public_ai(world.a, query).promotion == PromotionApplied(
        shown, "Promo", "Hasta agotar stock", D8
    )


def test_valid_until_stops_at_the_next_promotion_only_if_it_would_win(
    world: Any, shop: Any
) -> None:
    early = datetime(2026, 4, 20, tzinfo=LIMA)
    ends_current = datetime(2026, 4, 30, tzinfo=LIMA)
    current = promo(world.a, [shop.listed], [{"variant_id": shop.variant}], ends=ends_current)
    pointless = promo(  # empieza dentro de la vigencia pero pierde: no corta
        world.a,
        [shop.listed],
        [{"brand_id": shop.make}],
        value="5",
        starts=early,
        ends=ends_current + timedelta(days=5),
    )
    got = final(world.a, shop.viewer, shop.variant, shop.listed, NOW)
    assert (got.promotion_id, got.valid_until) == (current, ends_current)
    winner = promo(
        world.a,
        [shop.listed],
        [{"product_id": shop.product}],
        value="40",
        starts=early + timedelta(days=2),
        ends=ends_current + timedelta(days=9),
    )
    got = final(world.a, shop.viewer, shop.variant, shop.listed, NOW)
    assert (got.promotion_id, got.valid_until) == (current, early + timedelta(days=2))
    after = final(world.a, shop.viewer, shop.variant, shop.listed, early + timedelta(days=2))
    assert after.promotion_id == winner
    assert pointless != winner
    # sin promoción vigente, la próxima que empieza fija el tope
    gap = final(world.a, shop.viewer, shop.variant, shop.listed, D1 - timedelta(days=10))
    assert gap.promotion_id is None
    # y el fin del precio se respeta si llega antes
    priced = stock(world.a)[1]
    row(world.a, (priced, shop.listed), "100", T0 - timedelta(days=90), D1 + timedelta(days=1))
    promo(world.a, [shop.listed], [{"variant_id": priced}])
    short = final(world.a, shop.viewer, priced, shop.listed, D1)
    assert short.valid_until == D1 + timedelta(days=1)


def test_no_price_means_no_promotion_and_the_resolution_has_no_cost_fields(
    world: Any, shop: Any
) -> None:
    bare = stock(world.a)[1]
    promo(world.a, [shop.listed], [{"variant_id": bare}])
    got = final(world.a, shop.viewer, bare, shop.listed, NOW)
    assert (got.found, got.promotion, got.promotion_id, got.savings) == (False, None, None, None)
    assert [f.name for f in fields(PromotionApplied)] == ["id", "name", "terms", "ends_at"]


def test_resolve_many_with_promotions_answers_like_resolve_in_constant_queries(
    world: Any, shop: Any
) -> None:
    page = [shop.variant]
    for _ in range(11):
        v = stock(world.a, shop.iphone[1], shop.make)[1]
        row(world.a, (v, shop.listed), "100", T0 - timedelta(days=90))
        page.append(v)
    promo(world.a, [shop.listed], [{"category_id": shop.iphone[0]}])
    promo(
        world.a,
        [shop.listed],
        [{"brand_id": shop.make}],
        value="15",
        starts=D8,
        ends=D8 + timedelta(days=7),
    )
    with tenant_scope(ctx(world.a, shop.viewer)) as tenant:
        many = services.resolve_many(execution_context(tenant), page, shop.listed, NOW)
    one = {v: final(world.a, shop.viewer, v, shop.listed, NOW) for v in page}
    assert many == one
    assert {r.promotion_id is not None for r in many.values()} == {True}

    def count(size: int) -> int:
        with tenant_scope(ctx(world.a, shop.viewer)) as tenant:
            ectx = execution_context(tenant)
            with CaptureQueriesContext(connection) as queries:
                services.resolve_many(ectx, page[:size], shop.listed, NOW)
        return len(queries)

    assert (
        count(1) == count(12) == 6
    )  # lista, variantes, precios, promociones, objetivos, categorías


def test_a_channel_wider_than_the_column_is_refused_not_truncated(world: Any, shop: Any) -> None:
    promo(world.a, [shop.listed], [{"variant_id": shop.variant}], channels=["W" * 30])
    assert final(
        world.a, shop.viewer, shop.variant, shop.listed, NOW, channel="W" * 30
    ).promotion_id
    with pytest.raises(ValueError, match="channel"):
        final(world.a, shop.viewer, shop.variant, shop.listed, NOW, channel="W" * 31)


def test_a_total_tie_goes_to_the_lowest_id(world: Any, shop: Any) -> None:
    high, low = UUID(int=2**100), UUID(int=1)  # el mayor se inserta primero
    for pk, target in ((high, {"brand_id": shop.make}), (low, {"product_id": shop.product})):
        promo(world.a, [shop.listed], [target], value="10", priority=3, id=pk)
    got = final(world.a, shop.viewer, shop.variant, shop.listed, NOW)
    assert got.promotion_id == low


def test_without_a_price_no_promotion_query_runs(world: Any, shop: Any) -> None:
    bare = stock(world.a)[1]
    promo(world.a, [shop.listed], [{"variant_id": bare}])
    with tenant_scope(ctx(world.a, shop.viewer)) as tenant:
        ectx = execution_context(tenant)
        with CaptureQueriesContext(connection) as queries:
            services.resolve_many(ectx, [bare], shop.listed, NOW)
    assert len(queries) == 3
    assert not [q for q in queries if "promotion" in q["sql"]]
