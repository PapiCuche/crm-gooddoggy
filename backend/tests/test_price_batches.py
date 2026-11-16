"""F3-49 (E03-08): el lote de edición masiva de precios y su previsualización, contra PostgreSQL
real con el rol `crm_app`: el cálculo, la selección, el tope, el actor, RLS y lo que el runtime no
puede cambiar. Ningún precio cambia al previsualizar."""

from datetime import timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from django.db import DatabaseError, IntegrityError, connection
from django.utils import timezone

from apps.catalog.models import Category, ProductVariant
from apps.pricing import batches
from apps.pricing.models import PriceChangeBatch, PriceChangeBatchItem
from core.tenancy.context import ActorType, TenantContext
from core.tenancy.scope import tenant_scope
from tests import test_authorization
from tests.test_memberships import ctx
from tests.test_prices import logged, price_list
from tests.test_pricing_engine import person, row
from tests.test_promotion_tables import brand, stock, tree

world = test_authorization.world
pytestmark = pytest.mark.usefixtures("tenant_db")
PAST = timezone.now() - timedelta(days=30)
EDITOR: dict[str, str | None] = {"prices.view": None, "prices.bulk_update": None}
Line = tuple[Any, ...]  # estado, antes, después, variación y motivo


def sibling(org: UUID, parent: UUID, slug: str) -> UUID:
    """Una hija de `parent` (las de `tree` empiezan en la raíz)."""
    with tenant_scope(ctx(org)):
        node = Category.objects.get(pk=parent)
        made = Category.objects.create(
            name=slug, slug=slug, path=f"{node.path}{slug}/", depth=2, parent_id=parent
        )
    return UUID(str(made.pk))


@pytest.fixture
def shop(world: Any) -> Any:
    """Regular con «Celulares/iPhone» (y su hija «Pro»), sus hermanas «iphone-pro» e «iphone0»,
    cuyo `path` empieza igual y no son de su rama, y «Android»."""

    class Shop:
        editor = person(world, EDITOR)
        regular = price_list(world.a, is_default=True)
        phones, iphone, pro = tree(world.a, "celulares", "iphone", "pro")
        dash, zero = sibling(world.a, phones, "iphone-pro"), sibling(world.a, phones, "iphone0")
        android = tree(world.a, "android")[0]
        apple = brand(world.a)
        _, ip16 = stock(world.a, iphone, apple)
        _, ip16_pro = stock(world.a, pro, apple)
        _, ip15 = stock(world.a, iphone, apple)  # sin precio en Regular
        _, other = stock(world.a, dash, apple)
        _, other0 = stock(world.a, zero, apple)
        _, galaxy = stock(world.a, android)

    for variant, amount in [(Shop.ip16, "4399"), (Shop.ip16_pro, "5199.90"), (Shop.other, "100")]:
        row(world.a, (variant, Shop.regular), amount, PAST)
    row(world.a, (Shop.galaxy, Shop.regular), "999", PAST)
    return Shop


def create(world: Any, user: Any, listed: UUID, filters: Any, **fields: Any) -> PriceChangeBatch:
    wanted = {"operation": "PERCENT_DECREASE", "value": "5", "reason": "Campaña", **fields}
    with tenant_scope(ctx(world.a, user)) as tenant:
        return batches.create_batch(tenant, price_list_id=listed, filters=filters, **wanted)


def apple(world: Any, shop: Any) -> PriceChangeBatch:
    return create(world, shop.editor, shop.regular, {"brand_id": shop.apple})


def lines(world: Any, batch: PriceChangeBatch) -> dict[UUID, Line]:
    with tenant_scope(ctx(world.a)):
        found = PriceChangeBatchItem.objects.filter(batch=batch)
        fields = ("status", "old_amount", "new_amount", "delta_percent", "error")
        return {item.variant_id: tuple(getattr(item, f) for f in fields) for item in found}


def single(world: Any, old: str, **fields: Any) -> tuple[PriceChangeBatch, Line]:
    """Un lote de una variante con precio `old` en una lista nueva, y su línea."""
    user, listed = person(world, EDITOR), price_list(world.a)
    _, variant = stock(world.a)
    row(world.a, (variant, listed), old, PAST)
    batch = create(world, user, listed, {"variant_ids": [variant]}, **fields)
    return batch, lines(world, batch)[variant]


def audits(migrator: psycopg.Connection[Any]) -> list[tuple[Any, ...]]:
    return logged(migrator, "audit_logs", "action, entity_type, changes, metadata")


def test_minus_5_percent_to_iphone_in_regular_rounded_to_whole_soles_changes_no_price(
    world: Any, shop: Any, migrator: psycopg.Connection[Any]
) -> None:
    """La aceptación de F3-49: antes y después por variante, sin precio como `SKIPPED`."""
    prices_before = logged(migrator, "product_prices", "*")
    batch = create(
        world, shop.editor, shop.regular, {"category_id": shop.iphone}, rounding="INTEGER"
    )
    assert lines(world, batch) == {
        shop.ip16: ("PENDING", Decimal("4399.00"), Decimal("4179.00"), Decimal("-5.00"), ""),
        shop.ip16_pro: ("PENDING", Decimal("5199.90"), Decimal("4940.00"), Decimal("-5.00"), ""),
        shop.ip15: ("SKIPPED", None, None, None, batches.NO_PRICE),
    }
    seen = (batch.kind, batch.status, batch.reason, batch.effective_at, batch.created_by_user_id)
    assert seen == ("BULK_ADJUST", "PREVIEWED", "Campaña", None, shop.editor.pk)
    params = {"operation": "PERCENT_DECREASE", "value": "5.00", "rounding": "INTEGER"}
    assert batch.params == {**params, "filters": {"category_id": str(shop.iphone)}}
    counts = {"lines": 3, "changes": 2, "no_price": 1, "unchanged": 0, "errors": 0, "alerts": 0}
    assert batch.summary == {**counts, "currency": "PEN"}
    assert logged(migrator, "product_prices", "*") == prices_before  # ningún precio cambia
    assert logged(migrator, "outbox_events", "id") == []
    [(action, entity, changes, metadata)] = [a for a in audits(migrator) if a[0][:6] == "price."]
    assert (action, entity, changes) == ("price.bulk_previewed", "price_change_batch", {})
    shown = {"price_list_id": str(shop.regular), **params, "effective_at": None}
    assert metadata == {**shown, **counts, "currency": "PEN"}  # sin las líneas


@pytest.mark.parametrize(
    ("operation", "value", "rounding", "old", "new", "delta"),
    [
        ("PERCENT_INCREASE", "10", "CENTS", "4399", "4838.90", "10.00"),
        ("PERCENT_INCREASE", "2.5", "INTEGER", "10.00", "10.00", None),  # 10.25 → 10: igual
        ("PERCENT_DECREASE", "12.5", "CENTS", "19.99", "17.49", "-12.51"),
        ("PERCENT_DECREASE", "5", "INTEGER", "4399", "4179.00", "-5.00"),
        ("AMOUNT_INCREASE", "100.50", "CENTS", "4399", "4499.50", "2.28"),
        ("AMOUNT_INCREASE", "0.50", "INTEGER", "10.00", "11.00", "10.00"),  # 10.5 sube
        ("AMOUNT_DECREASE", "0.01", "CENTS", "4399", "4398.99", "-0.00"),
        ("AMOUNT_DECREASE", "0.60", "INTEGER", "10.00", "9.00", "-10.00"),
    ],
)
def test_the_four_operations_with_their_rounding(
    world: Any, operation: str, value: str, rounding: str, old: str, new: str, delta: str | None
) -> None:
    batch, (status, before, after, change, _) = single(
        world, old, operation=operation, value=value, rounding=rounding
    )
    assert (before, after) == (Decimal(old), Decimal(new))
    if delta is None:
        assert (status, change) == ("SKIPPED", Decimal("0.00"))
        assert (batch.summary["unchanged"], batch.summary["no_price"]) == (1, 0)
    else:
        assert (status, change) == ("PENDING", Decimal(delta))


@pytest.mark.parametrize(
    ("operation", "value", "new", "error"),
    [
        ("AMOUNT_DECREASE", "100", Decimal("0.00"), batches.NOT_POSITIVE),
        ("AMOUNT_DECREASE", "150", Decimal("-50.00"), batches.NOT_POSITIVE),
        ("PERCENT_DECREASE", "100", Decimal("0.00"), batches.NOT_POSITIVE),
        ("AMOUNT_INCREASE", "999999999999.99", None, batches.TOO_BIG),
    ],
)
def test_a_result_of_zero_or_less_or_too_big_is_an_error_line(
    world: Any, operation: str, value: str, new: Decimal | None, error: str
) -> None:
    batch, (status, before, after, _, why) = single(world, "100", operation=operation, value=value)
    assert (status, before, after, why) == ("ERROR", Decimal("100.00"), new, error)
    assert (batch.summary["errors"], batch.summary["changes"]) == (1, 0)


@pytest.mark.parametrize(
    ("operation", "value", "old", "alerts"),
    [
        ("AMOUNT_DECREASE", "30", "100", 0),  # 70.00: −30.00 %, justo el límite
        ("AMOUNT_DECREASE", "30", "76.90", 1),  # 46.90: −39.01 %
        ("AMOUNT_DECREASE", "30", "10", 1),  # −20.00: un error, y también −300 %
        ("PERCENT_INCREASE", "30", "100", 0),  # 130.00: +30.00 %
        ("PERCENT_INCREASE", "30.01", "100", 1),
    ],
)
def test_more_than_30_percent_either_way_is_an_alert(
    world: Any, operation: str, value: str, old: str, alerts: int
) -> None:
    batch, _ = single(world, old, operation=operation, value=value)
    assert batch.summary["alerts"] == alerts


def test_the_selection_by_brand_product_variants_and_combined(world: Any, shop: Any) -> None:
    def chosen(**filters: Any) -> set[UUID]:
        return set(lines(world, create(world, shop.editor, shop.regular, filters)))

    all_apple = {shop.ip16, shop.ip16_pro, shop.ip15, shop.other, shop.other0}
    assert chosen(brand_id=shop.apple) == chosen(category_id=shop.phones) == all_apple
    assert chosen(category_id=shop.iphone) == {shop.ip16, shop.ip16_pro, shop.ip15}
    assert chosen(category_id=shop.pro) == {shop.ip16_pro}
    assert chosen(category_id=shop.dash, brand_id=shop.apple) == {shop.other}
    with tenant_scope(ctx(world.a)):
        product = ProductVariant.objects.get(pk=shop.galaxy).product_id
        ProductVariant.objects.filter(pk=shop.ip15).update(deleted_at=timezone.now())
    assert chosen(product_id=product) == {shop.galaxy}
    assert chosen(category_id=shop.iphone) == {shop.ip16, shop.ip16_pro}  # solo las vivas
    assert chosen(variant_ids=[shop.ip16, shop.galaxy], brand_id=shop.apple) == {shop.ip16}


@pytest.mark.parametrize(
    ("fields", "field"),
    [
        ({"operation": "PERCENT_OFF"}, "operation"),
        ({"rounding": "0.90"}, "rounding"),
        ({"value": "0"}, "value"),
        ({"value": "100.01"}, "value"),
        ({"value": "1.234"}, "value"),
        ({"value": 5.0}, "value"),
        ({"reason": "no"}, "reason"),
        ({"reason": "<b>Campaña</b>"}, "reason"),
        ({"effective_at": timezone.now() - timedelta(minutes=1)}, "effective_at"),
        ({"effective_at": timezone.now().replace(tzinfo=None) + timedelta(days=1)}, "effective_at"),
        ({"filters": {}}, "filters"),
        ({"filters": {"sku": "IP16"}}, "filters"),
        ({"filters": {"category_id": uuid4()}}, "category_id"),
        ({"filters": {"brand_id": uuid4()}}, "brand_id"),
        ({"filters": {"product_id": uuid4()}}, "product_id"),
        ({"filters": {"variant_ids": []}}, "variant_ids"),
        ({"filters": {"variant_ids": "IP16"}}, "variant_ids"),
        ({"filters": {"variant_ids": ["IP16"]}}, "variant_ids"),
        ({"filters": {"variant_ids": [uuid4()]}}, "variant_ids"),
        ({"price_list_id": uuid4()}, "price_list_id"),
    ],
)
def test_invalid_data_is_refused_on_its_field_and_writes_nothing(
    world: Any, shop: Any, fields: dict[str, Any], field: str, migrator: psycopg.Connection[Any]
) -> None:
    wanted = {"listed": shop.regular, "filters": {"category_id": shop.iphone}, **fields}
    wanted["listed"] = wanted.pop("price_list_id", wanted["listed"])
    with pytest.raises(batches.Invalid) as refused:
        create(world, shop.editor, **wanted)
    assert refused.value.field == field
    assert logged(migrator, "price_change_batches", "id") == []


def test_lists_and_targets_the_editor_does_not_see_are_not_found(world: Any, shop: Any) -> None:
    wholesale = price_list(world.a, "MAYORISTA", required_permission="prices.view_wholesale")
    inactive = price_list(world.a, "VIEJA", is_active=False)
    _, foreign = stock(world.b)
    iphone = {"category_id": shop.iphone}
    cases: list[tuple[UUID, dict[str, Any], str]] = [
        (wholesale, iphone, "price_list_id"),
        (inactive, iphone, "price_list_id"),
        (price_list(world.b), iphone, "price_list_id"),
        (shop.regular, {"brand_id": brand(world.b)}, "brand_id"),
        (shop.regular, {"category_id": tree(world.b, "x")[0]}, "category_id"),
        (shop.regular, {"variant_ids": [foreign]}, "variant_ids"),
        (shop.regular, {"category_id": shop.android, "brand_id": shop.apple}, "filters"),
    ]
    for listed, filters, field in cases:
        with pytest.raises(batches.Invalid) as refused:
            create(world, shop.editor, listed, filters)
        assert refused.value.field == field
    with pytest.raises(batches.Invalid, match="distintas"):  # no «no existe»
        create(world, shop.editor, shop.regular, {"variant_ids": [shop.ip16, shop.ip16]})
    manager = person(world, {"prices.manage": None})  # ve todas las listas
    assert create(world, manager, wholesale, {"brand_id": shop.apple})


def test_a_future_date_reads_the_price_that_will_rule_then(world: Any, shop: Any) -> None:
    later = timezone.now() + timedelta(days=7)
    row(world.a, (shop.ip15, shop.regular), "3000", later - timedelta(days=1))
    batch = create(
        world, shop.editor, shop.regular, {"variant_ids": [shop.ip15]}, effective_at=later
    )
    assert batch.effective_at == later
    assert lines(world, batch)[shop.ip15][:3] == ("PENDING", Decimal("3000.00"), Decimal("2850.00"))


def test_more_lines_than_the_cap_is_refused_before_writing(
    world: Any, shop: Any, monkeypatch: pytest.MonkeyPatch, migrator: psycopg.Connection[Any]
) -> None:
    assert (batches.LINES_MAX, batches.ALERT_PERCENT) == (2000, Decimal(30))
    monkeypatch.setattr(batches, "LINES_MAX", 3)
    with pytest.raises(batches.TooManyLines) as refused:
        apple(world, shop)
    assert refused.value.count == 5
    assert logged(migrator, "price_change_batches", "id") == []
    batch = create(world, shop.editor, shop.regular, {"category_id": shop.iphone})
    assert batch.summary["lines"] == 3  # justo el tope


@pytest.mark.parametrize(
    "actor", [ActorType.AI_AGENT, ActorType.INTEGRATION, ActorType.SYSTEM, "MISMATCH"]
)
def test_only_a_person_creates_a_batch_refused_before_any_query(
    world: Any, shop: Any, actor: str, django_assert_num_queries: Any
) -> None:
    user = shop.editor.pk  # el tipo solo ya rechaza: `actor_id` es la misma persona
    kind, actor_id = (ActorType.USER, uuid4()) if actor == "MISMATCH" else (ActorType(actor), user)
    wanted: dict[str, Any] = {"operation": "PERCENT_DECREASE", "value": "5", "reason": "Campaña"}
    with (
        tenant_scope(TenantContext(world.a, "test", user, kind, actor_id)) as scoped,
        django_assert_num_queries(0),
    ):
        with pytest.raises(batches.ActorRefused):
            batches.create_batch(scoped, price_list_id=shop.regular, filters={}, **wanted)
    assert not issubclass(batches.ActorRefused, ValueError)


def test_the_tables_refuse_foreign_rows_and_the_runtime_cannot_rewrite_a_line(
    world: Any, shop: Any
) -> None:
    batch = apple(world, shop)
    _, foreign = stock(world.b)
    other_list, theirs = price_list(world.a, "EFECTIVO"), price_list(world.b, "B")
    base: dict[str, Any] = {
        "batch_id": batch.pk,
        "price_list_id": shop.regular,
        "status": "SKIPPED",
    }
    cases = [
        ({**base, "variant_id": foreign}, "price_batch_items_variant_fk"),
        ({**base, "variant_id": shop.galaxy, "price_list_id": other_list}, "items_batch_fk"),
        ({**base, "variant_id": shop.galaxy, "status": "PENDING"}, "price_batch_items_pending_ck"),
        ({**base, "variant_id": shop.ip16}, "price_batch_items_pair_uq"),
        ({**base, "variant_id": shop.galaxy, "status": "DONE"}, "price_batch_items_status_ck"),
    ]
    for values, name in cases:
        with pytest.raises(IntegrityError, match=name), tenant_scope(ctx(world.a)):
            PriceChangeBatchItem.objects.create(**values)
    wanted = {"kind": "BULK_ADJUST", "status": "DRAFT", "params": {}, "reason": "Otra"}
    with pytest.raises(IntegrityError, match="batches_price_list_fk"), tenant_scope(ctx(world.a)):
        PriceChangeBatch.objects.create(price_list_id=theirs, **wanted)
    for sql in (
        "DELETE FROM price_change_batches",
        "DELETE FROM price_change_batch_items",
        "TRUNCATE price_change_batches",
        "TRUNCATE price_change_batch_items",
        "UPDATE price_change_batch_items SET new_amount = 1",
        "UPDATE price_change_batch_items SET old_amount = 1",
    ):
        with (
            pytest.raises(DatabaseError, match="permission denied"),
            tenant_scope(ctx(world.a)),
            connection.cursor() as cursor,
        ):
            cursor.execute(sql)
    with tenant_scope(ctx(world.a)):  # aplicar solo pondrá el estado y el motivo
        assert PriceChangeBatchItem.objects.filter(batch=batch).update(status="STALE") == 5
    with tenant_scope(ctx(world.b)):  # RLS: ni el lote ni sus líneas
        assert not PriceChangeBatch.objects.exists()
        assert not PriceChangeBatchItem.objects.exists()
