"""F3-09: el servicio de inventario (`apps.inventory.stock`) contra PostgreSQL real con el rol
`crm_app`, también con dos transacciones a la vez (un hilo y una conexión cada una)."""

import threading
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from django.db import DatabaseError, connection

from apps.catalog.models import Product, ProductVariant
from apps.inventory import stock
from apps.inventory.models import InventoryLevel, InventoryMovement, Warehouse
from apps.organizations.models import Branch
from core.tenancy.scope import tenant_scope
from tests import test_authorization
from tests.test_memberships import ctx

world = test_authorization.world
pytestmark = pytest.mark.usefixtures("tenant_db")


def cell(org: UUID, sku: str = "IP16", product_type: str = "PHYSICAL") -> tuple[UUID, UUID]:
    """Una variante y un almacén de la organización: la celda del stock."""
    with tenant_scope(ctx(org)):
        product = Product.objects.create(
            name=sku,
            slug=sku.lower(),
            product_type=product_type,
            tracks_inventory=product_type == "PHYSICAL",
        )
        variant = ProductVariant.objects.create(product=product, sku=sku, name=sku)
        branch = Branch.objects.get_or_create(code="LIM", defaults={"name": "Lima"})[0]
        warehouse = Warehouse.objects.get_or_create(branch=branch, code="ALM", name="Central")[0]
    return variant.pk, warehouse.pk


def wanted(where: tuple[UUID, UUID], kind: str, quantity: int, key: str = "") -> stock.Wanted:
    return stock.Wanted(*where, kind, quantity, " Conteo  de prueba ", key or uuid4().hex)


def move(org: UUID, user: Any, want: stock.Wanted) -> tuple[InventoryMovement, bool]:
    with tenant_scope(ctx(org, user)) as tenant:
        return stock.move(tenant, want)


def level(org: UUID, where: tuple[UUID, UUID]) -> tuple[int, int, int]:
    with tenant_scope(ctx(org)):
        row = InventoryLevel.objects.get(variant_id=where[0], warehouse_id=where[1])
        return row.on_hand, row.reserved, row.available


def test_receipt_issue_and_adjustment_leave_the_level_and_the_ledger(
    world: Any, migrator: psycopg.Connection[Any]
) -> None:
    here = cell(world.a)
    received, new = move(world.a, world.ana, wanted(here, "RECEIPT", 10))
    assert new and (received.quantity, received.on_hand_after) == (10, 10)
    assert (received.created_by_type, received.created_by_user_id) == ("USER", world.ana.pk)
    assert received.reason == "Conteo de prueba" and received.reference_type == "MANUAL"
    assert move(world.a, world.ana, wanted(here, "ISSUE", 3))[0].quantity == -3
    counted = move(world.a, None, wanted(here, "ADJUSTMENT", 5))[0]  # lo contado: -2
    assert (counted.quantity, counted.on_hand_after, counted.created_by_type) == (-2, 5, "SYSTEM")
    assert level(world.a, here) == (5, 0, 5)
    audit = "SELECT action, changes FROM audit_logs WHERE entity_type = 'inventory_movement'"
    assert sorted(migrator.execute(audit).fetchall()) == [
        ("inventory.adjusted", {"on_hand": [7, 5]}),
        ("inventory.issued", {"on_hand": [10, 7]}),
        ("inventory.received", {"on_hand": [0, 10]}),
    ]


def test_what_the_stock_cannot_do(world: Any) -> None:
    here, service, foreign = cell(world.a), cell(world.a, "SRV", "SERVICE"), cell(world.b, "B")
    with pytest.raises(stock.InsufficientStock):
        move(world.a, world.ana, wanted(here, "ISSUE", 1))
    move(world.a, world.ana, wanted(here, "RECEIPT", 2))
    with tenant_scope(ctx(world.a)):  # lo reservado no sale ni se ajusta por debajo
        InventoryLevel.objects.filter(variant_id=here[0]).update(reserved=2)
    for kind, quantity in (("ISSUE", 1), ("ADJUSTMENT", 1)):
        with pytest.raises(stock.InsufficientStock):
            move(world.a, world.ana, wanted(here, kind, quantity))
    for field, where in (
        ("variant_id", service),  # un servicio no lleva stock
        ("variant_id", (foreign[0], here[1])),  # de otra organización
        ("warehouse_id", (here[0], foreign[1])),
    ):
        with pytest.raises(stock.BadReference) as refused:
            move(world.a, world.ana, wanted(where, "RECEIPT", 1))
        assert refused.value.field == field
    for bad in (wanted(here, "RECEIPT", 0), wanted(here, "SALE", 1), wanted(here, "ISSUE", -1)):
        with pytest.raises(ValueError):
            move(world.a, world.ana, bad)
    with pytest.raises(ValueError):
        move(world.a, world.ana, wanted(here, "RECEIPT", 1, key="corta"))
    with tenant_scope(ctx(world.a)):
        Warehouse.objects.filter(pk=here[1]).update(is_active=False)
    with pytest.raises(stock.WarehouseInactive):
        move(world.a, world.ana, wanted(here, "RECEIPT", 1))
    assert level(world.a, here) == (2, 2, 0)
    with tenant_scope(ctx(world.a)):
        assert InventoryMovement.objects.count() == 1  # solo el primer ingreso


def test_the_same_key_registers_once(world: Any) -> None:
    here = cell(world.a)
    first, new = move(world.a, world.ana, wanted(here, "RECEIPT", 4, key="pedido-0001"))
    again, repeated = move(world.a, world.ana, wanted(here, "RECEIPT", 4, key="pedido-0001"))
    assert (new, repeated, again.pk) == (True, False, first.pk)
    for other in (
        wanted(here, "RECEIPT", 5, "pedido-0001"),
        wanted(here, "ISSUE", 4, "pedido-0001"),
    ):
        with pytest.raises(stock.KeyReused):
            move(world.a, world.ana, other)
    counted = wanted(here, "ADJUSTMENT", 9, key="conteo-0001")  # repetir un conteo no lo repite
    assert move(world.a, world.ana, counted)[0].quantity == 5
    assert move(world.a, world.ana, counted)[1] is False
    assert level(world.a, here) == (9, 0, 9)


def race(org: UUID, user: Any, wants: list[Any], then: str = "") -> list[Any]:
    """El primero registra su movimiento (o ejecuta su SQL) y retiene la transacción; el segundo
    tiene que esperar su bloqueo, y sigue cuando el primero confirma (tras ejecutar `then`)."""
    wrote, release, outcome = threading.Event(), threading.Event(), list[Any]([None, None])

    def run(index: int) -> None:
        try:
            with tenant_scope(ctx(org, user)) as tenant:
                if isinstance(wants[index], str):
                    outcome[index] = connection.cursor().execute(wants[index])
                else:
                    outcome[index] = stock.move(tenant, wants[index])
                if index == 0:
                    wrote.set()
                    release.wait(timeout=10)
                    if then:
                        connection.cursor().execute(then)
        except (stock.InsufficientStock, stock.KeyReused, DatabaseError) as error:
            outcome[index] = type(error).__name__
        finally:
            wrote.set()
            connection.close()

    first, second = (threading.Thread(target=run, args=(i,)) for i in (0, 1))
    first.start()
    assert wrote.wait(timeout=10)
    second.start()
    second.join(timeout=1)
    waited = second.is_alive()
    release.set()
    for thread in (first, second):
        thread.join(timeout=10)
    assert waited, "el segundo no esperó al primero"
    return outcome


def test_two_issues_of_the_last_unit_at_once_only_one_goes_out(world: Any) -> None:
    here = cell(world.a)
    move(world.a, world.ana, wanted(here, "RECEIPT", 1))
    first, second = race(world.a, world.ana, [wanted(here, "ISSUE", 1) for _ in "ab"])
    assert first[1] is True and second == "InsufficientStock"
    assert level(world.a, here) == (0, 0, 0)


def test_a_movement_waits_for_whoever_locked_the_level_first(world: Any) -> None:
    """Quien bloqueó el saldo y aún no lo ha cambiado (`FOR UPDATE` sin escribir): el movimiento
    espera y lee lo que deja al confirmar, no lo de antes."""
    here = cell(world.a)
    move(world.a, world.ana, wanted(here, "RECEIPT", 1))
    lock = "SELECT 1 FROM inventory_levels FOR UPDATE"
    first, second = race(
        world.a,
        world.ana,
        [lock, wanted(here, "ISSUE", 1)],
        then="UPDATE inventory_levels SET on_hand = 0",
    )
    assert second == "InsufficientStock" and level(world.a, here) == (0, 0, 0)


def test_two_receipts_at_once_both_count_and_share_one_level_row(world: Any) -> None:
    here = cell(world.a)  # sin fila de saldo: las dos la crean a la vez
    outcome = race(world.a, world.ana, [wanted(here, "RECEIPT", 2), wanted(here, "RECEIPT", 3)])
    assert [new for _, new in outcome] == [True, True]
    assert level(world.a, here) == (5, 0, 5)
    assert sorted(m.on_hand_after for m, _ in outcome) == [2, 5]


def test_the_same_key_at_once_registers_one_movement(world: Any) -> None:
    here, there = cell(world.a), cell(world.a, "OTRA")
    same = wanted(here, "RECEIPT", 2, key="clave-0001")
    first, second = race(world.a, world.ana, [same, same])
    assert first[0].pk == second[0].pk and (first[1], second[1]) == (True, False)
    # la misma clave, a la vez, para otra celda: la decide el índice único
    elsewhere = wanted(there, "RECEIPT", 2, key="clave-0002")
    outcome = race(world.a, world.ana, [wanted(here, "RECEIPT", 2, key="clave-0002"), elsewhere])
    assert outcome[1] == "KeyReused"
    with tenant_scope(ctx(world.a)):
        assert InventoryMovement.objects.count() == 2
    assert level(world.a, here) == (4, 0, 4)


def test_the_ledger_is_append_only_and_the_level_never_negative(world: Any) -> None:
    here = cell(world.a)
    move(world.a, world.ana, wanted(here, "RECEIPT", 1))
    for change in (
        "UPDATE inventory_movements SET quantity = 9",
        "DELETE FROM inventory_movements",
    ):
        with pytest.raises(DatabaseError, match="permission denied"), tenant_scope(ctx(world.a)):
            connection.cursor().execute(change)
    with (
        pytest.raises(DatabaseError, match="inventory_levels_stock_ck"),
        tenant_scope(ctx(world.a)),
    ):
        InventoryLevel.objects.update(on_hand=-1)
