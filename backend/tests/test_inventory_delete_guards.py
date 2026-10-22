"""F3-12: no se borra un producto ni una variante con stock, y `on_hand` no pasa del máximo de
INT. Servicios, rutas y dos transacciones a la vez contra PostgreSQL real con el rol `crm_app`."""

from typing import Any
from uuid import UUID

import psycopg
import pytest
from django.test import Client

from apps.catalog.events import VariantsHeld, variants_retiring
from apps.catalog.models import Product, ProductVariant
from apps.catalog.products import delete_product, delete_variant
from apps.inventory import stock
from apps.inventory.guards import refuse_with_stock
from apps.inventory.models import InventoryLevel, InventoryMovement
from core.tenancy.scope import tenant_scope
from tests import test_authorization, test_self_context
from tests.test_authorization import give
from tests.test_inventory_api import ADJUST, post
from tests.test_inventory_stock import cell, level, move, race, wanted
from tests.test_memberships import ctx
from tests.test_products import MANAGE, send, url
from tests.test_self_context import signed

world, real_stack = test_authorization.world, test_self_context.real_stack
pytestmark = pytest.mark.usefixtures("tenant_db")


def sibling(org: UUID, variant_id: UUID, sku: str = "OTRA") -> UUID:
    """Otra variante del mismo producto: la primera deja de ser la única."""
    with tenant_scope(ctx(org)):
        product_id = ProductVariant.objects.get(pk=variant_id).product_id
        made: ProductVariant = ProductVariant.objects.create(
            product_id=product_id, sku=sku, name=sku
        )
        return made.pk


def product_of(org: UUID, variant_id: UUID) -> UUID:
    with tenant_scope(ctx(org)):
        product_id: UUID = ProductVariant.objects.get(pk=variant_id).product_id
        return product_id


def live(org: UUID) -> tuple[int, int]:
    with tenant_scope(ctx(org)):
        products = Product.objects.filter(deleted_at__isnull=True).count()
        return products, ProductVariant.objects.filter(deleted_at__isnull=True).count()


def drop_product(product_id: UUID) -> Any:
    return lambda tenant: delete_product(tenant, product_id=product_id)


def drop_variant(product_id: UUID, variant_id: UUID) -> Any:
    return lambda tenant: delete_variant(tenant, product_id=product_id, variant_id=variant_id)


def run(org: UUID, user: Any, command: Any) -> Any:
    with tenant_scope(ctx(org, user)) as tenant:
        return command(tenant)


def test_stock_or_reservations_keep_a_product_and_its_variants(
    world: Any, migrator: psycopg.Connection[Any]
) -> None:
    here = cell(world.a)
    other = sibling(world.a, here[0])
    product = product_of(world.a, here[0])
    move(world.a, world.ana, wanted((other, here[1]), "RECEIPT", 2))  # solo la segunda tiene
    with pytest.raises(VariantsHeld):
        run(world.a, world.ana, drop_product(product))
    with pytest.raises(VariantsHeld):
        run(world.a, world.ana, drop_variant(product, other))
    with tenant_scope(ctx(world.a)):  # todo reservado: nada disponible, pero se conserva
        InventoryLevel.objects.filter(variant_id=other).update(reserved=2)
    for command in (drop_product(product), drop_variant(product, other)):
        with pytest.raises(VariantsHeld):
            run(world.a, world.ana, command)
    assert live(world.a) == (1, 2)
    deleted = "SELECT count(*) FROM audit_logs WHERE action LIKE '%.deleted'"
    assert migrator.execute(deleted).fetchone() == (0,)
    run(world.a, world.ana, drop_variant(product, here[0]))  # sin stock: se retira


def test_without_stock_the_delete_goes_and_the_ledger_stays(world: Any) -> None:
    here = cell(world.a)
    move(world.a, world.ana, wanted(here, "RECEIPT", 3))
    move(world.a, world.ana, wanted(here, "ISSUE", 3))
    run(world.a, world.ana, drop_product(product_of(world.a, here[0])))
    assert live(world.a) == (0, 0)
    with tenant_scope(ctx(world.a)):
        assert InventoryMovement.objects.count() == 2
    assert level(world.a, here) == (0, 0, 0)


def test_a_receipt_up_to_the_int_limit_and_not_one_more(world: Any) -> None:
    here = cell(world.a)
    move(world.a, world.ana, wanted(here, "RECEIPT", 1))
    with tenant_scope(ctx(world.a)):
        InventoryLevel.objects.update(on_hand=stock.ON_HAND_MAX - 5)
    assert move(world.a, world.ana, wanted(here, "RECEIPT", 5))[0].on_hand_after == 2**31 - 1
    with pytest.raises(stock.StockLimit):
        move(world.a, world.ana, wanted(here, "RECEIPT", 1))
    assert level(world.a, here)[0] == stock.ON_HAND_MAX
    with tenant_scope(ctx(world.a)):
        assert InventoryMovement.objects.count() == 2


@pytest.fixture
def ana(world: Any) -> Client:
    give(world.a, world.membership, {**MANAGE, **ADJUST})
    return signed(world.ana)


def test_each_refusal_has_its_code_and_offers_deactivation(ana: Client, world: Any) -> None:
    here = cell(world.a)
    other = sibling(world.a, here[0])
    product = f"{url()}{product_of(world.a, here[0])}/"
    post(ana, here, "RECEIPT")
    for path, code, offer in (
        (product, "PRODUCT_HAS_STOCK", "DISCONTINUED"),
        (f"{product}variants/{here[0]}/", "VARIANT_HAS_STOCK", "INACTIVE"),
    ):
        refused = send(ana, "delete", path)
        assert (refused.status_code, refused.json()["code"]) == (409, code)
        assert offer in refused.json()["message"]
    assert send(ana, "patch", product, {"status": "DISCONTINUED"}).status_code == 200
    with tenant_scope(ctx(world.a)):
        InventoryLevel.objects.update(on_hand=stock.ON_HAND_MAX)
    full = post(ana, here, "RECEIPT")
    assert (full.status_code, full.json()["code"]) == (409, "STOCK_LIMIT")
    assert level(world.a, here)[0] == stock.ON_HAND_MAX
    assert send(ana, "delete", f"{product}variants/{other}/").status_code == 204


def test_a_delete_waits_for_a_receipt_in_flight_and_sees_its_stock(world: Any) -> None:
    """El borrado bloquea la variante como el movimiento: espera a que confirme y mira el stock
    que deja. Sin el bloqueo leía el saldo de antes (cero) y borraba una variante con stock."""
    here = cell(world.a)
    other = sibling(world.a, here[0])
    product = product_of(world.a, here[0])
    outcome = race(world.a, world.ana, [wanted(here, "RECEIPT", 1), drop_variant(product, here[0])])
    assert outcome[1] == "VariantsHeld"
    move(world.a, world.ana, wanted(here, "ISSUE", 1))  # otra vez sin stock
    receipt = wanted((other, here[1]), "RECEIPT", 1)
    assert race(world.a, world.ana, [receipt, drop_product(product)])[1] == "VariantsHeld"
    assert live(world.a) == (1, 2) and level(world.a, (other, here[1])) == (1, 0, 1)


def test_a_receipt_waits_for_a_delete_in_flight_and_finds_nothing(world: Any) -> None:
    here = cell(world.a)
    product = product_of(world.a, here[0])
    other = sibling(world.a, here[0])
    receipt = wanted((other, here[1]), "RECEIPT", 1)
    outcome = race(world.a, world.ana, [drop_variant(product, other), receipt])
    assert outcome[1] == "BadReference"
    outcome = race(world.a, world.ana, [drop_product(product), wanted(here, "RECEIPT", 1)])
    assert outcome[1] == "BadReference" and live(world.a) == (0, 0)
    with tenant_scope(ctx(world.a)):
        assert not InventoryMovement.objects.exists()


def test_the_guard_is_wired_and_without_it_nothing_is_deleted(world: Any) -> None:
    """`inventory` conecta su veto al arrancar; si faltara, el catálogo no borra (falla cerrado)."""
    assert variants_retiring.has_listeners(ProductVariant)
    here = cell(world.a)
    other = sibling(world.a, here[0])
    product = product_of(world.a, here[0])
    assert variants_retiring.disconnect(dispatch_uid="inventory.refuse_with_stock")
    try:
        for command in (drop_variant(product, other), drop_product(product)):
            with pytest.raises(RuntimeError):
                run(world.a, world.ana, command)
    finally:
        variants_retiring.connect(refuse_with_stock, dispatch_uid="inventory.refuse_with_stock")
    assert live(world.a) == (1, 2)
