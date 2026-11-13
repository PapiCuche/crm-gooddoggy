"""F3-30: transferencias entre almacenes (`apps.inventory.transfers`) contra PostgreSQL real con el
rol `crm_app`, también con dos transacciones a la vez (un hilo y una conexión cada una)."""

import threading
from datetime import timedelta
from importlib import import_module
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from django.core.management import call_command
from django.db import DatabaseError, IntegrityError, connection
from django.utils import timezone

from apps.inventory import reservations as res
from apps.inventory import stock, transfers, warehouses
from apps.inventory.models import InventoryLevel, Warehouse
from core.tenancy.scope import tenant_scope
from tests import test_authorization
from tests.test_inventory_stock import cell, level, move, race, wanted
from tests.test_memberships import ctx
from tests.test_pipelines_tables import as_migrator

world = test_authorization.world
pytestmark = pytest.mark.usefixtures("tenant_db")
MIGRATION = import_module("apps.inventory.migrations.0004_transfer_checks")


def site(org: UUID, here: tuple[UUID, UUID], code: str) -> UUID:
    """Otro almacén de la organización, en la sucursal del primero."""
    with tenant_scope(ctx(org)):
        branch = Warehouse.objects.get(pk=here[1]).branch_id
        return Warehouse.objects.create(branch_id=branch, code=code, name=code).pk  # type: ignore[no-any-return]


def asked(variant: Any, source: Any, target: Any, quantity: Any, key: str = "") -> Any:
    return transfers.Wanted(
        variant, source, target, quantity, " Reponer  tienda ", key or uuid4().hex
    )


def send(org: UUID, user: Any, want: Any) -> tuple[transfers.Legs, bool]:
    with tenant_scope(ctx(org, user)) as tenant:
        return transfers.transfer(tenant, want)


def does(want: Any) -> Any:
    return lambda tenant: transfers.transfer(tenant, want)


def stocked(org: UUID, units: int, *codes: str) -> tuple[UUID, UUID, list[UUID]]:
    here = cell(org)
    move(org, None, wanted(here, "RECEIPT", units))
    return here[0], here[1], [site(org, here, code) for code in codes]


def written(org: UUID) -> tuple[int, int]:
    """Movimientos de transferencia y auditorías `inventory.transferred` de la organización."""
    with tenant_scope(ctx(org)), connection.cursor() as cursor:
        cursor.execute(
            "SELECT (SELECT count(*) FROM inventory_movements WHERE transfer_group_id IS NOT NULL),"
            " (SELECT count(*) FROM audit_logs WHERE action = 'inventory.transferred')"
        )
        row = cursor.fetchone()
    return row[0], row[1]


def on_hand(org: UUID, variant: UUID, *places: UUID) -> list[int]:
    """`on_hand` de la variante en cada almacén; 0 si nunca tuvo saldo."""
    with tenant_scope(ctx(org)):
        rows = InventoryLevel.objects.filter(variant_id=variant)
        found = dict(rows.values_list("warehouse_id", "on_hand"))
    return [found.get(place, 0) for place in places]


def test_a_transfer_leaves_two_legs_the_levels_and_one_audit(
    world: Any, migrator: psycopg.Connection[Any]
) -> None:
    variant, origin, (shop,) = stocked(world.a, 10, "TDA")
    (out, entry), new = send(world.a, world.ana, asked(variant, origin, shop, 4, "traslado-01"))
    assert new and out.transfer_group_id == entry.transfer_group_id == out.reference_id
    assert [
        (m.movement_type, m.warehouse_id, m.quantity, m.on_hand_after) for m in (out, entry)
    ] == [
        ("TRANSFER_OUT", origin, -4, 6),
        ("TRANSFER_IN", shop, 4, 4),
    ]
    assert (out.idempotency_key, entry.idempotency_key) == (
        "traslado-01",
        f"T/{out.transfer_group_id}/in",
    )
    assert {(m.reference_type, m.reason, m.created_by_user_id) for m in (out, entry)} == {
        ("TRANSFER", "Reponer tienda", world.ana.pk)
    }
    assert on_hand(world.a, variant, origin, shop) == [6, 4]
    with tenant_scope(ctx(world.a)):
        assert sorted(InventoryLevel.objects.values_list("version", flat=True)) == [2, 3]
    audit = "SELECT entity_type, entity_id, entity_label, changes, metadata FROM audit_logs"
    rows = migrator.execute(f"{audit} WHERE action = 'inventory.transferred'").fetchall()  # noqa: S608
    assert rows == [
        (
            "inventory_transfer",
            out.transfer_group_id,
            "IP16: ALM → TDA",
            {"from_on_hand": [10, 6], "to_on_hand": [0, 4]},
            {
                "variant_id": str(variant),
                "from_warehouse_id": str(origin),
                "to_warehouse_id": str(shop),
                "quantity": 4,
            },
        )
    ]


def test_what_a_transfer_refuses_writes_nothing(world: Any) -> None:
    variant, origin, (shop, closed) = stocked(world.a, 3, "TDA", "CERR")
    service = cell(world.a, "SRV", "SERVICE")[0]
    alien_variant, alien_place = cell(world.b, "B")
    for bad in (
        asked(variant, origin, origin, 1),
        *(asked(variant, origin, shop, q) for q in (0, -1, stock.QUANTITY_MAX + 1, True, 2.0, "2")),
        asked(variant, origin, shop, 1, "corta"),
        asked(variant, origin, shop, 1, "import:12345678"),
        asked(variant, origin, shop, 1, "con/barra-01"),
        transfers.Wanted(variant, origin, shop, 1, "   ", uuid4().hex),
    ):
        with pytest.raises(ValueError):  # noqa: PT011 — cada uno dice su campo
            send(world.a, world.ana, bad)
    with pytest.raises(ValueError, match="from_warehouse_id"):
        asked(variant, "no-es-uuid", shop, 1)
    refused = []
    for field, want in (
        ("variant_id", asked(service, origin, shop, 1)),
        ("variant_id", asked(alien_variant, origin, shop, 1)),
        ("variant_id", asked(uuid4(), origin, shop, 1)),
        ("from_warehouse_id", asked(variant, alien_place, shop, 1)),
        ("from_warehouse_id", asked(variant, uuid4(), shop, 1)),
        ("to_warehouse_id", asked(variant, origin, alien_place, 1)),
        ("to_warehouse_id", asked(variant, origin, uuid4(), 1)),
    ):
        with pytest.raises(stock.BadReference) as error:
            send(world.a, world.ana, want)
        assert error.value.field == field
        refused.append((field, error.value.args))
    assert refused[3][1] == refused[4][1] and refused[5][1] == refused[6][1]  # ajeno = inventado
    with tenant_scope(ctx(world.a)):
        Warehouse.objects.filter(pk=closed).update(is_active=False)
        InventoryLevel.objects.filter(warehouse_id=origin).update(reserved=2)
    for field, want in (
        ("to_warehouse_id", asked(variant, origin, closed, 1)),
        ("from_warehouse_id", asked(variant, closed, origin, 1)),
    ):
        with pytest.raises(transfers.WarehouseInactive) as inactive:
            send(world.a, world.ana, want)
        assert inactive.value.field == field
    with pytest.raises(stock.InsufficientStock):  # 3 en mano, 2 reservadas: sale 1 como mucho
        send(world.a, world.ana, asked(variant, origin, shop, 2))
    with tenant_scope(ctx(world.a)):
        InventoryLevel.objects.create(
            variant_id=variant, warehouse_id=shop, on_hand=stock.ON_HAND_MAX - 1
        )
        InventoryLevel.objects.filter(warehouse_id=origin).update(reserved=0)
    with pytest.raises(stock.StockLimit):  # el origen no se toca
        send(world.a, world.ana, asked(variant, origin, shop, 2))
    assert level(world.a, (variant, origin)) == (3, 0, 3) and written(world.a) == (0, 0)
    assert send(world.a, world.ana, asked(variant, origin, shop, 1))[1]  # justo hasta el tope
    assert on_hand(world.a, variant, origin, shop) == [2, stock.ON_HAND_MAX]


def test_the_same_key_returns_the_same_legs_and_nothing_else(world: Any) -> None:
    variant, origin, (shop, other) = stocked(world.a, 9, "TDA", "OTRO")
    same = asked(variant, origin, shop, 2, "traslado-02")
    legs, new = send(world.a, world.ana, same)
    as_text = asked(str(variant), str(origin), str(shop), 2, "traslado-02")
    for again in (same, as_text):
        repeated, fresh = send(world.a, world.ana, again)
        assert (new, fresh, [m.pk for m in repeated]) == (True, False, [m.pk for m in legs])
    move(world.a, None, wanted((variant, origin), "RECEIPT", 1, "ingreso-0001"))
    for other_ask in (
        asked(variant, origin, other, 2, "traslado-02"),
        asked(variant, shop, origin, 2, "traslado-02"),
        asked(variant, origin, shop, 3, "traslado-02"),
        transfers.Wanted(variant, origin, shop, 2, "Otro motivo", "traslado-02"),
        asked(variant, origin, shop, 1, "ingreso-0001"),  # la clave de un ingreso
    ):
        with pytest.raises(stock.KeyReused):
            send(world.a, world.ana, other_ask)
    with tenant_scope(ctx(world.a)):  # repetirla tras desactivar un almacén no cambia nada
        Warehouse.objects.filter(pk=shop).update(is_active=False)
    assert send(world.a, world.ana, same)[1] is False
    assert on_hand(world.a, variant, origin, shop) == [8, 2] and written(world.a) == (2, 1)


def test_a_failure_after_the_first_leg_leaves_nothing(
    world: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    variant, origin, (shop,) = stocked(world.a, 5, "TDA")

    def broken(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("auditoría caída")

    monkeypatch.setattr(transfers, "record", broken)
    with pytest.raises(RuntimeError):
        send(world.a, world.ana, asked(variant, origin, shop, 2))
    assert on_hand(world.a, variant, origin) == [5] and written(world.a) == (0, 0)
    with tenant_scope(ctx(world.a)):
        assert not InventoryLevel.objects.filter(warehouse_id=shop, on_hand__gt=0).exists()


def test_the_database_refuses_a_malformed_transfer_leg(world: Any) -> None:
    variant, origin, _ = stocked(world.a, 5)
    malformed_legs_are_refused(world.a, variant, origin)
    for change in (
        "UPDATE inventory_movements SET quantity = 9",
        "DELETE FROM inventory_movements",
    ):
        with pytest.raises(DatabaseError, match="permission denied"), tenant_scope(ctx(world.a)):
            connection.cursor().execute(change)


def malformed_legs_are_refused(org: UUID, variant: UUID, origin: UUID) -> None:
    """Como `crm_app`, cada fila mala la rechaza su `CHECK` (por su nombre); la buena entra."""
    group, other = uuid4(), uuid4()
    insert = (
        "INSERT INTO inventory_movements (organization_id, id, variant_id, warehouse_id,"
        " movement_type, quantity, on_hand_after, reserved_after, reference_type, reference_id,"
        " transfer_group_id, reason, idempotency_key, created_by_type, created_at)"
        " VALUES (app_current_tenant(), uuidv7(), %s, %s, %s, %s, 0, 0, %s, %s, %s, 'x', %s,"
        " 'SYSTEM', now())"
    )
    sign, link = "inventory_movements_sign_ck", "inventory_movements_transfer_ck"
    for violated, row in (
        (sign, ("TRANSFER_OUT", 1, "TRANSFER", group, group)),
        (sign, ("TRANSFER_IN", -1, "TRANSFER", group, group)),
        (link, ("TRANSFER_OUT", -1, "TRANSFER", group, None)),
        (link, ("TRANSFER_IN", 1, "MANUAL", group, group)),
        (link, ("TRANSFER_IN", 1, "TRANSFER", None, group)),
        (link, ("TRANSFER_IN", 1, "TRANSFER", other, group)),
        (link, ("RECEIPT", 1, "MANUAL", None, group)),
        (link, ("RECEIPT", 1, "TRANSFER", None, None)),
    ):
        with pytest.raises(IntegrityError) as error, tenant_scope(ctx(org)):
            connection.cursor().execute(insert, [variant, origin, *row, uuid4().hex])
        assert error.value.__cause__.diag.constraint_name == violated  # type: ignore[union-attr]
    with tenant_scope(ctx(org)):
        connection.cursor().execute(
            insert, [variant, origin, "TRANSFER_IN", 1, "TRANSFER", group, group, uuid4().hex]
        )


VALID = (
    "SELECT conname, convalidated FROM pg_constraint WHERE conname IN "
    "('inventory_movements_sign_ck', 'inventory_movements_transfer_ck') ORDER BY conname"
)
LEGACY = (  # filas que el `CHECK` de `0003` admitía y los de `0004` no
    "INSERT INTO inventory_movements (organization_id, variant_id, warehouse_id, movement_type,"
    " quantity, on_hand_after, reserved_after, reference_type, reason, idempotency_key,"
    " created_by_type, created_at) SELECT organization_id, variant_id, warehouse_id, m.t, 1, 0,"
    " 0, 'MANUAL', 'x', 'k-' || m.t || warehouse_id, 'SYSTEM', now() FROM inventory_levels,"
    " (VALUES ('TRANSFER_OUT'), ('TRANSFER_IN')) m(t)"
)


def test_the_migrations_refuse_bad_rows_with_a_count_and_validate_both_checks(
    world: Any, migrator: psycopg.Connection[Any]
) -> None:
    variant, origin, (shop,) = stocked(world.a, 2, "TDA")
    send(world.a, world.ana, asked(variant, origin, shop, 1))
    assert migrator.execute(VALID).fetchall() == [
        ("inventory_movements_sign_ck", True), ("inventory_movements_transfer_ck", True)
    ]  # fmt: skip
    with pytest.raises(RuntimeError, match="BYPASSRLS"):  # `crm_app`, bajo RLS: no vería filas
        MIGRATION.refuse_violations(None, SimpleNamespace(connection=connection))
    with as_migrator():  # atrás con transferencias en el ledger: no se pierde nada
        call_command("migrate", "inventory", "0003", verbosity=0)
    assert [r[0] for r in migrator.execute(VALID).fetchall()] == ["inventory_movements_sign_ck"]
    migrator.execute(LEGACY)
    with as_migrator(), pytest.raises(RuntimeError, match="0004: 4 inventory movements"):
        call_command("migrate", "inventory", verbosity=0)
    migrator.execute("DELETE FROM inventory_movements WHERE idempotency_key LIKE 'k-%'")
    with as_migrator():
        call_command("migrate", "inventory", "0004", verbosity=0)
    assert {v for _, v in migrator.execute(VALID).fetchall()} == {False}  # rigen, sin recorrer
    with as_migrator():
        call_command("migrate", "inventory", verbosity=0)
        with connection.cursor() as cursor:  # sin `lock_timeout` colgado en la conexión
            cursor.execute("SHOW lock_timeout")
            assert cursor.fetchone() == ("0",)
    assert {v for _, v in migrator.execute(VALID).fetchall()} == {True}
    assert written(world.a) == (2, 1)
    malformed_legs_are_refused(world.a, variant, origin)  # los que deja la migración, de nuevo


def test_the_last_unit_sent_to_two_shops_at_once_leaves_once(world: Any) -> None:
    variant, origin, (shop, other) = stocked(world.a, 1, "TDA", "OTRO")
    wants = [does(asked(variant, origin, place, 1)) for place in (shop, other)]
    first, second = race(world.a, world.ana, wants)
    assert first[1] is True and second == "InsufficientStock"
    assert sorted(on_hand(world.a, variant, origin, shop, other)) == [0, 0, 1]
    assert written(world.a) == (2, 1)


def test_opposite_transfers_at_once_both_finish_without_deadlock(world: Any) -> None:
    variant, origin, (shop,) = stocked(world.a, 5, "TDA")
    send(world.a, world.ana, asked(variant, origin, shop, 5))
    move(world.a, None, wanted((variant, origin), "RECEIPT", 5))
    wants = [does(asked(variant, origin, shop, 2)), does(asked(variant, shop, origin, 3))]
    outcome = race(world.a, world.ana, wants)
    assert [new for _, new in outcome] == [True, True]
    assert on_hand(world.a, variant, origin, shop) == [6, 4]  # el total se conserva: 10


@pytest.mark.parametrize("transfer_first", [True, False])
def test_a_transfer_and_an_issue_of_the_last_unit_only_one_goes(
    world: Any, transfer_first: bool
) -> None:
    variant, origin, (shop,) = stocked(world.a, 1, "TDA")
    issue = wanted((variant, origin), "ISSUE", 1)
    wants = [does(asked(variant, origin, shop, 1)), lambda t: stock.move(t, issue)]
    outcome = race(world.a, world.ana, wants if transfer_first else wants[::-1])
    assert outcome[1] == "InsufficientStock"
    assert on_hand(world.a, variant, origin, shop) == ([0, 1] if transfer_first else [0, 0])


def test_the_same_key_at_once_writes_two_legs_not_four(world: Any) -> None:
    variant, origin, (shop,) = stocked(world.a, 4, "TDA")
    same = does(asked(variant, origin, shop, 2, "traslado-03"))
    first, second = race(world.a, world.ana, [same, same])
    assert [m.pk for m in first[0]] == [m.pk for m in second[0]] and (first[1], second[1]) == (
        True,
        False,
    )
    assert on_hand(world.a, variant, origin, shop) == [2, 2] and written(world.a) == (2, 1)


def test_the_same_key_at_once_for_another_variant_is_refused_by_the_index(world: Any) -> None:
    variant, origin, (shop,) = stocked(world.a, 4, "TDA")
    twin = cell(world.a, "OTRA")[0]
    move(world.a, None, wanted((twin, origin), "RECEIPT", 4))
    wants = [does(asked(v, origin, shop, 1, "traslado-04")) for v in (variant, twin)]
    outcome = race(world.a, world.ana, wants)  # el segundo espera el índice único, no la variante
    assert outcome[0][1] is True and outcome[1] == "KeyReused"
    assert on_hand(world.a, twin, origin, shop) == [4, 0] and written(world.a) == (2, 1)


@pytest.mark.parametrize("delete_first", [True, False])
def test_deleting_the_destination_meanwhile_never_strands_stock(
    world: Any, delete_first: bool
) -> None:
    """Borrar toma el almacén `FOR UPDATE`, que choca con el `FOR KEY SHARE` de la transferencia:
    la que llega segunda espera. Si borra primero, la transferencia ya no lo encuentra; si
    transfiere primero, el borrado ve el stock y se niega."""
    variant, origin, (shop,) = stocked(world.a, 3, "TDA")
    wants = [
        lambda t: warehouses.delete_warehouse(t, warehouse_id=shop),
        does(asked(variant, origin, shop, 1)),
    ]
    first, second = race(world.a, world.ana, wants if delete_first else wants[::-1])
    if delete_first:
        assert second == "BadReference" and written(world.a) == (0, 0)
        assert on_hand(world.a, variant, origin) == [3]
    else:
        assert second == "WarehouseHasStock" and on_hand(world.a, variant, shop) == [1]
        with tenant_scope(ctx(world.a)):
            assert Warehouse.objects.get(pk=shop).deleted_at is None


def test_deactivating_a_warehouse_meanwhile_is_ordered_after_the_transfer(world: Any) -> None:
    """Desactivar no es exclusivo con mover stock (`FOR NO KEY UPDATE` contra `FOR KEY SHARE`,
    como en `stock.move`): una transferencia que ve el almacén activo no espera y queda antes de la
    desactivación, que tampoco exige un almacén vacío. Con la desactivación confirmada, se niega."""
    variant, origin, (shop,) = stocked(world.a, 3, "TDA")
    held, done = threading.Event(), threading.Event()

    def deactivate() -> None:
        with tenant_scope(ctx(world.a, world.ana)):
            connection.cursor().execute(
                "UPDATE warehouses SET is_active = false WHERE id = %s", [shop]
            )
            held.set()
            done.wait(timeout=10)
        connection.close()

    other = threading.Thread(target=deactivate)
    other.start()
    assert held.wait(timeout=10)
    try:
        assert send(world.a, world.ana, asked(variant, origin, shop, 1))[1] is True
    finally:
        done.set()
        other.join(timeout=10)
    with pytest.raises(transfers.WarehouseInactive):
        send(world.a, world.ana, asked(variant, origin, shop, 1))
    assert on_hand(world.a, variant, origin, shop) == [2, 1]


@pytest.mark.parametrize("transfer_first", [True, False])
def test_reserving_at_the_origin_meanwhile_never_moves_what_is_reserved(
    world: Any, transfer_first: bool
) -> None:
    variant, origin, (shop,) = stocked(world.a, 3, "TDA")
    hold = res.Wanted(
        variant, origin, 2, timezone.now() + timedelta(hours=1), "Apartado", "r-0001ab"
    )
    wants = [does(asked(variant, origin, shop, 2)), lambda t: res.reserve(t, hold)]
    outcome = race(world.a, world.ana, wants if transfer_first else wants[::-1])
    assert outcome[1] == "InsufficientStock"
    on, reserved, _ = level(world.a, (variant, origin))
    assert (on, reserved) == ((1, 0) if transfer_first else (3, 2))


@pytest.mark.parametrize("transfer_first", [True, False])
def test_expiring_at_the_origin_meanwhile_both_finish(
    world: Any, monkeypatch: pytest.MonkeyPatch, transfer_first: bool
) -> None:
    variant, origin, (shop,) = stocked(world.a, 3, "TDA")
    with tenant_scope(ctx(world.a, world.ana)) as tenant:
        hold = res.Wanted(variant, origin, 2, timezone.now() + timedelta(hours=1), "Ap", "r-0002ab")
        pk = res.reserve(tenant, hold)[0].pk
    real = timezone.now
    monkeypatch.setattr(timezone, "now", lambda: real() + timedelta(hours=2))  # ya venció
    wants = [does(asked(variant, origin, shop, 1)), lambda t: res.expire(t, pk)]
    outcome = race(world.a, world.ana, wants if transfer_first else wants[::-1])
    assert all(not isinstance(result, str) for result in outcome)  # sin error ni interbloqueo
    on, reserved, available = level(world.a, (variant, origin))
    assert (on, reserved, available) == (2, 0, 2) and on_hand(world.a, variant, shop) == [1]


def test_whoever_holds_the_destination_level_makes_the_transfer_wait(world: Any) -> None:
    """Quien bloquea el saldo del destino (`FOR UPDATE` sin escribir): la transferencia espera y lee
    lo que deja al confirmar."""
    variant, origin, (shop,) = stocked(world.a, 2, "TDA")
    with tenant_scope(ctx(world.a)):
        InventoryLevel.objects.create(variant_id=variant, warehouse_id=shop, on_hand=5)
    lock = f"SELECT 1 FROM inventory_levels WHERE warehouse_id = '{shop}' FOR UPDATE"  # noqa: S608
    then = (
        f"UPDATE inventory_levels SET on_hand = {stock.ON_HAND_MAX} WHERE warehouse_id = '{shop}'"  # noqa: S608
    )
    outcome = race(world.a, world.ana, [lock, does(asked(variant, origin, shop, 1))], then=then)
    assert outcome[1] == "StockLimit" and on_hand(world.a, variant, origin) == [2]


def test_a_transfer_waits_for_whoever_deletes_the_variant(world: Any) -> None:
    """Borrarla en el catálogo bloquea la variante (`FOR NO KEY UPDATE`): la transferencia espera
    y, cuando el borrado confirma, ya no la encuentra."""
    variant, origin, (shop,) = stocked(world.a, 2, "TDA")
    retire = f"UPDATE product_variants SET deleted_at = now() WHERE id = '{variant}'"  # noqa: S608
    outcome = race(world.a, world.ana, [retire, does(asked(variant, origin, shop, 1))])
    assert outcome[1] == "BadReference" and written(world.a) == (0, 0)
