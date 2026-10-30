"""F3-48 (E03-05): las tablas de promociones contra PostgreSQL real con el rol `crm_app`: RLS,
FKs compuestas, restricciones, permisos mínimos y el redondeo. Aquí viven también los
ayudantes que usan los tests del motor (`test_pricing_promotions`)."""

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from django.db import DatabaseError, IntegrityError, connection

from apps.catalog.models import Brand, Category, Product, ProductVariant
from apps.pricing.models import Promotion, PromotionItem, PromotionPriceList
from apps.pricing.services import PriceQuery
from core.money import Rounding, round_amount
from core.tenancy.scope import tenant_scope
from tests import test_authorization
from tests.test_memberships import ctx
from tests.test_prices import price_list
from tests.test_pricing_engine import LIMA, T0, human, person, row

world = test_authorization.world
pytestmark = pytest.mark.usefixtures("tenant_db")
D1, D8 = datetime(2026, 4, 1, tzinfo=LIMA), datetime(2026, 4, 8, tzinfo=LIMA)
NOW = datetime(2026, 4, 3, 10, tzinfo=LIMA)
_n = iter(range(10_000))


def tree(org: UUID, *slugs: str) -> list[UUID]:
    """Una rama de categorías: cada una hija de la anterior."""
    made: list[UUID] = []
    path, parent = "/", None
    with tenant_scope(ctx(org)):
        for depth, slug in enumerate(slugs, 1):
            path += f"{slug}/"
            node = Category.objects.create(
                name=slug, slug=slug, path=path, depth=depth, parent_id=parent
            )
            parent = node.pk
            made.append(UUID(str(node.pk)))
    return made


def brand(org: UUID, slug: str = "apple") -> UUID:
    with tenant_scope(ctx(org)):
        return UUID(str(Brand.objects.create(name=slug, slug=slug).pk))


def stock(org: UUID, category: UUID | None = None, make: UUID | None = None) -> tuple[UUID, UUID]:
    """Un producto `ACTIVE` y su variante: (producto, variante)."""
    sku = f"S{next(_n)}"
    with tenant_scope(ctx(org)):
        product = Product.objects.create(
            name=sku,
            slug=sku.lower(),
            product_type="PHYSICAL",
            status="ACTIVE",
            category_id=category,
            brand_id=make,
        )
        one = ProductVariant.objects.create(product=product, sku=sku, name=sku)
    return UUID(str(product.pk)), UUID(str(one.pk))


def promo(
    org: UUID,
    lists: list[UUID],
    targets: list[dict[str, Any]],
    kind: str = "PERCENT_OFF",
    value: str = "10",
    starts: datetime = D1,
    ends: datetime = D8,
    **fields: Any,
) -> UUID:
    wanted = {
        "name": "Promo",
        "terms": "Hasta agotar stock",
        "status": "PUBLISHED",
        "priority": 0,
        "currency": "PEN",
    }
    with tenant_scope(ctx(org)):
        made = Promotion.objects.create(
            discount_type=kind,
            value=Decimal(value),
            starts_at=starts,
            ends_at=ends,
            **{**wanted, **fields},
        )
        for target in targets:
            PromotionItem.objects.create(promotion=made, **target)
        for listed in lists:
            PromotionPriceList.objects.create(promotion=made, price_list_id=listed)
    return UUID(str(made.pk))


def final(org: UUID, user: Any, v: UUID, listed: UUID, at: datetime, **query: Any) -> Any:
    return human(org, user, PriceQuery(v, listed, at, **query))


@pytest.fixture
def shop(world: Any) -> Any:
    class Shop:
        viewer = person(world, {"prices.view": None})
        listed = price_list(world.a, is_default=True)
        iphone = tree(world.a, "celulares", "iphone")
        make = brand(world.a)
        product, variant = stock(world.a, iphone[1], make)

    row(world.a, (Shop.variant, Shop.listed), "4399", T0 - timedelta(days=90))
    return Shop


@pytest.mark.parametrize(
    ("rule", "value", "expected"),
    [
        (Rounding.CENTS, "10.005", "10.01"),
        (Rounding.CENTS, "10.004", "10.00"),
        (Rounding.CENTS, "10.015", "10.02"),
        (Rounding.INTEGER, "10.5", "11.00"),
        (Rounding.INTEGER, "10.49", "10.00"),
        (Rounding.INTEGER, "11.5", "12.00"),
        (Rounding.INTEGER, "0.5", "1.00"),
    ],
)
def test_rounding_is_half_up_and_always_two_decimals(
    rule: Rounding, value: str, expected: str
) -> None:
    assert str(round_amount(Decimal(value), rule)) == expected


def test_another_organization_promotion_never_applies_and_rls_hides_it(
    world: Any, shop: Any
) -> None:
    theirs = price_list(world.b, is_default=True)
    _, v = stock(world.b)
    row(world.b, (v, theirs), "100", T0 - timedelta(days=90))
    promo(world.b, [theirs], [{"variant_id": v}])
    mine = final(world.a, shop.viewer, shop.variant, shop.listed, NOW)
    assert mine.promotion_id is None
    with tenant_scope(ctx(world.a)):
        assert not Promotion.objects.exists()
        assert not PromotionItem.objects.exists()
        assert not PromotionPriceList.objects.exists()
    with tenant_scope(ctx(world.b)):
        assert Promotion.objects.count() == 1


def test_composite_foreign_keys_refuse_another_organizations_rows(world: Any, shop: Any) -> None:
    _, foreign = stock(world.b)
    mine = promo(world.a, [shop.listed], [{"variant_id": shop.variant}])
    other_list = price_list(world.b, "AJENA")
    cases = [
        (PromotionItem, {"promotion_id": mine, "variant_id": foreign}, "promo_items_variant_fk"),
        (PromotionItem, {"promotion_id": mine, "brand_id": brand(world.b)}, "promo_items_brand_fk"),
        (
            PromotionItem,
            {"promotion_id": mine, "category_id": tree(world.b, "x")[0]},
            "promo_items_category_fk",
        ),
        (
            PromotionPriceList,
            {"promotion_id": mine, "price_list_id": other_list},
            "promo_lists_price_list_fk",
        ),
    ]
    for model, values, name in cases:
        with pytest.raises(IntegrityError, match=name), tenant_scope(ctx(world.a)):
            model.objects.create(**values)


@pytest.mark.parametrize(
    ("values", "name"),
    [
        ({"discount_type": "PERCENT_OFF", "value": Decimal("100.01")}, "promotions_value_ck"),
        ({"discount_type": "PERCENT_OFF", "value": Decimal("0")}, "promotions_value_ck"),
        ({"discount_type": "AMOUNT_OFF", "value": Decimal("-1")}, "promotions_value_ck"),
        ({"ends_at": D1}, "promotions_validity_ck"),
        ({"stackable": True}, "promotions_stackable_ck"),
        ({"status": "ACTIVE"}, "promotions_status_ck"),
        ({"discount_type": "BOGO"}, "promotions_type_ck"),
        ({"currency": "pen"}, "promotions_currency_ck"),
    ],
)
def test_promotion_check_constraints(world: Any, values: dict[str, Any], name: str) -> None:
    base: dict[str, Any] = {
        "name": "P",
        "discount_type": "AMOUNT_OFF",
        "value": Decimal("1"),
        "currency": "PEN",
        "starts_at": D1,
        "ends_at": D8,
    }
    with pytest.raises(IntegrityError, match=name), tenant_scope(ctx(world.a)):
        Promotion.objects.create(**{**base, **values})


def test_a_percent_of_exactly_100_is_a_valid_row_and_an_item_needs_exactly_one_target(
    world: Any, shop: Any
) -> None:
    made = promo(world.a, [shop.listed], [], "PERCENT_OFF", "100")
    both = {"variant_id": shop.variant, "brand_id": shop.make}
    for values in ({}, both, {"variant_id": shop.variant, "override_value": Decimal("0")}):
        with pytest.raises(IntegrityError, match="promo_items_"), tenant_scope(ctx(world.a)):
            PromotionItem.objects.create(promotion_id=made, **values)
    with pytest.raises(IntegrityError, match="promo_lists_pair_uq"), tenant_scope(ctx(world.a)):
        PromotionPriceList.objects.create(promotion_id=made, price_list_id=shop.listed)
        PromotionPriceList.objects.create(promotion_id=made, price_list_id=shop.listed)


def test_the_runtime_role_cannot_delete_promotions_or_change_their_items(
    world: Any, shop: Any
) -> None:
    made = promo(world.a, [shop.listed], [{"variant_id": shop.variant}])
    attempts = [
        "DELETE FROM promotions",
        "DELETE FROM promotion_items",
        "UPDATE promotion_items SET override_value = 1",
        "DELETE FROM promotion_price_lists",
        "UPDATE promotion_price_lists SET price_list_id = price_list_id",
        "TRUNCATE promotions CASCADE",
    ]
    for sql in attempts:
        with (
            pytest.raises(DatabaseError, match="permission denied"),
            tenant_scope(ctx(world.a)),
            connection.cursor() as cursor,
        ):
            cursor.execute(sql)
    with tenant_scope(ctx(world.a)):  # y sí cambia de estado
        assert Promotion.objects.filter(pk=made).update(status="CANCELLED") == 1
