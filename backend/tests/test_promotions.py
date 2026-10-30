"""F3-48c (E03-05): los comandos de promociones contra PostgreSQL real con el rol `crm_app`:
crear en borrador, publicar y cancelar con su auditoría, los datos que se rechazan sin escribir
nada, lo que la base impide a una publicada (migración 0004), las carreras de dos transacciones y
el motor aplicando la promoción solo en su ventana."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from django.db import DatabaseError, IntegrityError, connection
from django.utils import timezone

from apps.pricing import promotions as commands
from apps.pricing import services
from apps.pricing.models import Promotion, PromotionItem, PromotionPriceList
from core.tenancy.scope import tenant_scope
from tests import test_authorization
from tests.test_inventory_stock import race
from tests.test_memberships import ctx
from tests.test_prices import price_list
from tests.test_pricing_engine import item, public_ai
from tests.test_promotion_tables import brand, final, shop, stock

world, _shop = test_authorization.world, shop
pytestmark = pytest.mark.usefixtures("tenant_db")
DAY, US = timedelta(days=1), timedelta(microseconds=1)


def create(world: Any, shop: Any, **fields: Any) -> Promotion:
    now = timezone.now()
    wanted: dict[str, Any] = {
        "name": " Campaña  de octubre ",
        "discount_type": "PERCENT_OFF",
        "value": "10",
        "starts_at": now + 2 * DAY,
        "ends_at": now + 5 * DAY,
        "price_list_ids": [shop.listed],
        "items": [{"category_id": shop.iphone[1]}],
        **fields,
    }
    with tenant_scope(ctx(world.a, world.ana)) as tenant:
        return commands.create_promotion(tenant, **wanted)


def move(world: Any, command: Any, made: Promotion, org: UUID | None = None) -> Promotion:
    with tenant_scope(ctx(org or world.a, world.ana)) as tenant:
        return command(tenant, promotion_id=made.pk)  # type: ignore[no-any-return]


def state(world: Any, made: Promotion) -> tuple[str, int]:
    with tenant_scope(ctx(world.a)):
        status, version = Promotion.objects.values_list("status", "version").get(pk=made.pk)
    return str(status), int(version)


def test_a_draft_is_created_published_and_cancelled_with_its_audit(
    world: Any, shop: Any, migrator: psycopg.Connection[Any]
) -> None:
    other = stock(world.a)[1]
    made = create(
        world,
        shop,
        terms="Hasta agotar stock",
        channels=[" WEB "],
        items=[{"variant_id": other, "override_value": "5"}, {"brand_id": shop.make}],
    )
    assert (made.name, made.status, made.version, made.channels) == (
        "Campaña de octubre",
        "DRAFT",
        1,
        ["WEB"],
    )
    assert (made.value, made.created_by_user_id, made.stackable) == (
        Decimal("10.00"),
        world.ana.pk,
        False,
    )
    with tenant_scope(ctx(world.a)):
        targets = PromotionItem.objects.filter(promotion=made)
        found = {(i.variant_id, i.brand_id, i.override_value) for i in targets}
        assert found == {(other, None, Decimal("5.00")), (None, shop.make, None)}
        assert list(PromotionPriceList.objects.values_list("price_list_id", flat=True)) == [
            shop.listed
        ]
    assert move(world, commands.publish_promotion, made).status == "PUBLISHED"
    assert move(world, commands.cancel_promotion, made).status == "CANCELLED"
    assert state(world, made) == ("CANCELLED", 3)
    rows = migrator.execute(
        "SELECT action, changes, actor_id FROM audit_logs WHERE entity_type = 'promotion' "
        "ORDER BY occurred_at, id"
    ).fetchall()
    assert [r[0] for r in rows] == [
        "promotion.created",
        "promotion.published",
        "promotion.cancelled",
    ]
    assert rows[0][1]["value"] == [None, "10.00"] and rows[0][1]["name"] == [
        None,
        "Campaña de octubre",
    ]
    assert rows[1][1] == {"status": ["DRAFT", "PUBLISHED"]}
    assert rows[2][1] == {"status": ["PUBLISHED", "CANCELLED"]} and rows[2][2] == world.ana.pk


def test_the_engine_applies_the_promotion_exactly_in_its_window_after_publishing(
    world: Any, shop: Any
) -> None:
    made = create(world, shop)
    start, end = made.starts_at, made.ends_at

    def price(at: datetime) -> tuple[Decimal, UUID | None]:
        got = final(world.a, shop.viewer, shop.variant, shop.listed, at)
        return got.final_amount, got.promotion_id

    inside = start + DAY
    assert price(inside) == (Decimal("4399.00"), None)  # un borrador no se aplica
    move(world, commands.publish_promotion, made)
    for at, expected in (
        (start - US, None),
        (start, made.pk),
        (end - US, made.pk),
        (end, None),
    ):
        assert price(at) == (Decimal("3959.10") if expected else Decimal("4399.00"), expected), at
    move(world, commands.cancel_promotion, made)
    assert price(inside) == (Decimal("4399.00"), None)  # cancelada: deja de aplicarse al instante


Bad = Callable[[Any], dict[str, Any]]
LATER = datetime(2030, 1, 1, tzinfo=UTC)
CASES: list[tuple[str, Bad]] = [
    ("value", lambda s: {"value": "100.01"}),
    ("value", lambda s: {"discount_type": "AMOUNT_OFF", "value": "0"}),
    ("value", lambda s: {"value": "10.001"}),
    ("discount_type", lambda s: {"discount_type": "PERCENT"}),
    ("name", lambda s: {"name": "  "}),
    ("terms", lambda s: {"terms": "<script>alert(1)</script>"}),
    ("currency", lambda s: {"currency": "pen"}),
    ("ends_at", lambda s: {"starts_at": LATER, "ends_at": LATER}),
    ("ends_at", lambda s: {"starts_at": LATER.replace(tzinfo=None)}),
    ("priority", lambda s: {"priority": 10_001}),
    ("priority", lambda s: {"priority": -10_001}),
    ("channels", lambda s: {"channels": []}),
    ("channels", lambda s: {"channels": ["WEB", "WEB"]}),
    ("channels", lambda s: {"channels": ["A\x00"]}),
    ("channels", lambda s: {"channels": ["W" * 31]}),
    ("channels", lambda s: {"channels": [" "]}),
    ("channels", lambda s: {"channels": [5]}),
    ("items", lambda s: {"items": []}),
    ("items", lambda s: {"items": [{"variant_id": uuid4()}]}),
    ("items", lambda s: {"items": [{"category_id": s.iphone[1], "brand_id": s.make}]}),
    ("items", lambda s: {"items": [{"override_value": "5"}]}),
    ("items", lambda s: {"items": [{"brand_id": s.make, "cost": "5"}]}),
    ("items", lambda s: {"items": [{"brand_id": s.make, "override_value": "100.01"}]}),
    ("items", lambda s: {"items": [{"brand_id": s.make, "override_value": "0"}]}),
    ("items", lambda s: {"items": [{"brand_id": s.make}, {"brand_id": s.make}]}),
    ("price_list_ids", lambda s: {"price_list_ids": []}),
    ("price_list_ids", lambda s: {"price_list_ids": [uuid4()]}),
    ("price_list_ids", lambda s: {"price_list_ids": [s.listed, s.listed]}),
]


@pytest.mark.parametrize(("field", "bad"), CASES)
def test_bad_data_is_refused_with_its_field_and_nothing_is_written(
    world: Any, shop: Any, field: str, bad: Bad
) -> None:
    with pytest.raises(commands.Invalid) as error:
        create(world, shop, **bad(shop))
    assert error.value.field == field
    with tenant_scope(ctx(world.a)):
        assert not Promotion.objects.exists() and not PromotionItem.objects.exists()
        assert not PromotionPriceList.objects.exists()


def test_a_percent_override_of_exactly_100_is_valid_and_each_lists_currency_must_match(
    world: Any, shop: Any
) -> None:
    assert create(world, shop, items=[{"brand_id": shop.make, "override_value": "100"}]).pk
    assert create(world, shop, priority=10_000).priority == 10_000  # el borde de arriba vale
    usd = price_list(world.a, "DOLARES", currency="USD")
    for lists in ([usd], [shop.listed, usd]):
        with pytest.raises(commands.Invalid, match="moneda") as error:
            create(world, shop, price_list_ids=lists)
        assert error.value.field == "price_list_ids"
    assert create(world, shop, currency="USD", price_list_ids=[usd]).currency == "USD"


def test_another_organizations_rows_are_not_found_and_cannot_be_moved(
    world: Any, shop: Any
) -> None:
    foreign, foreign_list = stock(world.b, None, brand(world.b)), price_list(world.b, "AJENA")
    gone = item(world.a, "GONE", deleted="variant")
    for items, lists in (
        ([{"variant_id": foreign[1]}], [shop.listed]),
        ([{"product_id": foreign[0]}], [shop.listed]),
        ([{"variant_id": gone}], [shop.listed]),
        ([{"variant_id": shop.variant}], [foreign_list]),
        ([{"variant_id": shop.variant}], [shop.listed, foreign_list]),
    ):
        with pytest.raises(commands.Invalid, match="no existe"):
            create(world, shop, items=items, price_list_ids=lists)
    made = create(world, shop)
    for command in (commands.publish_promotion, commands.cancel_promotion):
        with pytest.raises(Promotion.DoesNotExist):
            move(world, command, made, org=world.b)
    assert state(world, made) == ("DRAFT", 1)


def test_the_status_rules_and_their_conflicts(world: Any, shop: Any) -> None:
    pub, can = commands.publish_promotion, commands.cancel_promotion
    made = create(world, shop)
    move(world, pub, made)
    with pytest.raises(commands.PromotionStateConflict) as error:
        move(world, pub, made)  # publicar solo sale de un borrador
    assert error.value.status == "PUBLISHED"
    move(world, can, made)
    for command in (pub, can):  # una cancelada no vuelve ni se cancela otra vez
        with pytest.raises(commands.PromotionStateConflict) as error:
            move(world, command, made)
        assert error.value.status == "CANCELLED"
    assert state(world, made) == ("CANCELLED", 3)  # los rechazos no suben la versión
    assert move(world, can, create(world, shop)).status == "CANCELLED"  # un borrador también
    old = timezone.now() - DAY
    ended = create(world, shop, starts_at=old - DAY, ends_at=old)  # un borrador puede ser pasado
    with pytest.raises(commands.PromotionEnded):
        move(world, pub, ended)
    assert state(world, ended) == ("DRAFT", 1)
    with pytest.raises(Promotion.DoesNotExist):
        move(world, pub, Promotion(pk=uuid4()))


def test_the_runtime_role_cannot_rewrite_or_reopen_a_promotion_nor_add_to_a_published_one(
    world: Any, shop: Any, migrator: psycopg.Connection[Any]
) -> None:
    made = create(world, shop)
    for stage in ("DRAFT", "PUBLISHED", "CANCELLED"):
        if stage != "DRAFT":
            move(
                world,
                commands.publish_promotion if stage == "PUBLISHED" else commands.cancel_promotion,
                made,
            )
        for column in ("value = 1", "name = 'x'", "channels = NULL", "organization_id = id"):
            with (
                pytest.raises(DatabaseError, match="permission denied"),
                tenant_scope(ctx(world.a)),
            ):
                run(f"UPDATE promotions SET {column}")  # noqa: S608
        if stage == "DRAFT":
            continue
        for back in ("DRAFT", "PUBLISHED") if stage == "CANCELLED" else ("DRAFT",):
            with (
                pytest.raises(IntegrityError, match="promotions_guard"),
                tenant_scope(ctx(world.a)),
            ):
                run(f"UPDATE promotions SET status = '{back}'")  # noqa: S608
        for model, values in (
            (PromotionItem, {"brand_id": shop.make}),
            (PromotionPriceList, {"price_list_id": price_list(world.a, f"L{stage}")}),
        ):
            with pytest.raises(IntegrityError, match="promotion_children_guard"):
                with tenant_scope(ctx(world.a)):
                    model.objects.create(promotion_id=made.pk, **values)
    assert state(world, made) == ("CANCELLED", 3)
    # el guardián vale para todos los roles: ni el migrador reescribe los términos de una publicada
    for column in ("value = 1", "channels = ARRAY['X']", "ends_at = ends_at + interval '1 day'"):
        with pytest.raises(psycopg.errors.CheckViolation, match="promotions_guard"):
            migrator.execute(f"UPDATE promotions SET {column}")  # noqa: S608
    assert migrator.execute("UPDATE promotions SET deleted_at = now()").rowcount == 1


def run(sql: str) -> None:
    with connection.cursor() as cursor:
        cursor.execute(sql)


def test_two_commands_at_once_leave_one_clear_outcome(world: Any, shop: Any) -> None:
    def go(command: Any, made: Promotion) -> Any:
        return lambda tenant: command(tenant, promotion_id=made.pk)

    pub, can = commands.publish_promotion, commands.cancel_promotion
    both = create(world, shop)
    first, second = race(world.a, world.ana, [go(pub, both), go(pub, both)])
    assert first.status == "PUBLISHED" and second == "PromotionStateConflict"
    assert state(world, both) == ("PUBLISHED", 2)
    mixed = create(world, shop)  # publicar y cancelar: la que espera ve el estado que dejó la otra
    first, second = race(world.a, world.ana, [go(pub, mixed), go(can, mixed)])
    assert (first.status, second.status) == ("PUBLISHED", "CANCELLED")
    assert state(world, mixed) == ("CANCELLED", 3)
    late = create(world, shop)
    first, second = race(world.a, world.ana, [go(can, late), go(pub, late)])
    assert first.status == "CANCELLED" and second == "PromotionStateConflict"
    assert state(world, late) == ("CANCELLED", 2)
    twice = create(world, shop)
    assert race(world.a, world.ana, [go(can, twice), go(can, twice)])[1] == "PromotionStateConflict"
    assert state(world, twice) == ("CANCELLED", 2)


def test_a_channel_is_text_without_nul_or_outer_spaces_when_stored_and_when_asked(
    world: Any, shop: Any
) -> None:
    for good in ("WEB", "W" * 30, "tienda online"):
        assert services.check_channel(good) == good
    for bad in ("", " WEB", "WEB ", "A\x00B", "W" * 31, 5, b"WEB"):
        with pytest.raises(ValueError, match="channel"):
            services.check_channel(bad)
    for bad in ("A\x00B", "", " WEB"):  # el motor lo rechaza antes de tocar la base
        with pytest.raises(ValueError, match="channel"):
            final(world.a, shop.viewer, shop.variant, shop.listed, timezone.now(), channel=bad)
        with pytest.raises(ValueError, match="channel"):
            public_ai(world.a, services.PriceQuery(shop.variant, shop.listed, channel=bad))
