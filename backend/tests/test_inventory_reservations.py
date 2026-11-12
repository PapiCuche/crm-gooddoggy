"""F3-33: reservas de stock (`apps.inventory.reservations`) contra PostgreSQL real con el rol
`crm_app`, también con varias transacciones a la vez (un hilo y una conexión cada una, E04-08).
Tras cada secuencia, `reserved` es la suma de las reservas ACTIVE de la celda."""

import threading
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from django.db import DatabaseError, connection
from django.db.models import Sum
from django.utils import timezone

from apps.catalog.models import ProductVariant
from apps.inventory import reservations as res
from apps.inventory import stock
from apps.inventory.models import InventoryMovement, InventoryReservation, Warehouse
from core.tenancy.scope import tenant_scope
from tests import test_authorization
from tests.test_inventory_delete_guards import drop_variant, product_of, sibling
from tests.test_inventory_stock import cell, level, move, race, wanted
from tests.test_memberships import ctx

world = test_authorization.world
pytestmark = pytest.mark.usefixtures("tenant_db")
Cell = tuple[UUID, UUID]


def asked(where: Cell, quantity: int = 1, key: str = "", hours: float = 2) -> res.Wanted:
    expires = timezone.now() + timedelta(hours=hours)
    return res.Wanted(*where, quantity, expires, " Apartado  para Ana ", key or uuid4().hex)


def reserve(org: UUID, user: Any, want: res.Wanted) -> tuple[InventoryReservation, bool]:
    with tenant_scope(ctx(org, user)) as tenant:
        return res.reserve(tenant, want)


def release(org: UUID, user: Any, pk: UUID, why: str = "Ya no lo quiere") -> Any:
    with tenant_scope(ctx(org, user)) as tenant:
        return res.release(tenant, pk, why)


def stocked(org: UUID, units: int, user: Any = None, sku: str = "IP16") -> Cell:
    here = cell(org, sku)
    move(org, user, wanted(here, "RECEIPT", units))
    return here


def ledger(org: UUID, kind: str) -> list[int]:
    with tenant_scope(ctx(org)):
        rows = InventoryMovement.objects.filter(movement_type=kind).order_by("pk")
        return list(rows.values_list("quantity", flat=True))


def consistent(org: UUID, where: Cell) -> tuple[int, int, int]:
    """El saldo de la celda, comprobando la invariante: `reserved` = Σ reservas ACTIVE."""
    with tenant_scope(ctx(org)):
        active = InventoryReservation.objects.filter(
            variant_id=where[0], warehouse_id=where[1], status="ACTIVE"
        ).aggregate(total=Sum("quantity"))["total"]
    saldo = level(org, where)
    assert saldo[1] == (active or 0) and saldo[1] <= saldo[0]
    return saldo


def test_reserve_and_release_move_reserved_with_their_ledger_and_audit(
    world: Any, migrator: psycopg.Connection[Any]
) -> None:
    here = stocked(world.a, 5, world.ana)
    made, new = reserve(world.a, world.ana, asked(here, 2))
    assert new and (made.status, made.quantity, made.reason) == ("ACTIVE", 2, "Apartado para Ana")
    assert (made.created_by_type, made.created_by_user_id) == ("USER", world.ana.pk)
    assert consistent(world.a, here) == (5, 2, 3)
    ended, now = release(world.a, world.ana, made.pk, " Se  arrepintió ")
    assert now and (ended.status, ended.end_reason) == ("RELEASED", "Se arrepintió")
    assert ended.ended_by_user_id == world.ana.pk and ended.ended_at is not None
    assert consistent(world.a, here) == (5, 0, 5)
    with tenant_scope(ctx(world.a)):
        lines = InventoryMovement.objects.filter(reference_type="RESERVATION").order_by("pk")
        seen = [(m.movement_type, m.quantity, m.on_hand_after, m.reserved_after) for m in lines]
        assert [m.idempotency_key for m in lines] == [
            f"R/{made.pk}/{s}" for s in ("reserve", "release")
        ]
        assert {m.reference_id for m in lines} == {made.pk}
    assert seen == [("RESERVE", 2, 5, 2), ("RELEASE", -2, 5, 0)]
    audit = "SELECT action, changes FROM audit_logs WHERE entity_type = 'inventory_reservation'"
    assert sorted(migrator.execute(audit).fetchall()) == [
        ("inventory.reservation_released", {"reserved": [2, 0], "status": ["ACTIVE", "RELEASED"]}),
        ("inventory.reserved", {"reserved": [0, 2]}),
    ]


def test_what_a_reservation_cannot_ask(world: Any) -> None:
    here = stocked(world.a, 2, world.ana)
    late = timezone.now() + res.EXPIRES_MAX + timedelta(minutes=1)
    for bad in (
        asked(here, 0),
        asked(here, -1),
        asked(here, stock.QUANTITY_MAX + 1),
        res.Wanted(*here, True, late - timedelta(hours=1), "Motivo", "clave-0001"),
        asked(here, hours=-1),  # ya vencida
        res.Wanted(*here, 1, late, "Motivo", "clave-0001"),  # más allá del máximo
        res.Wanted(*here, 1, datetime(2030, 1, 1), "Motivo", "clave-0001"),  # noqa: DTZ001
        asked(here, key="corta"),
        asked(here, key="R/otra/reserve"),  # el alfabeto de las claves derivadas
        res.Wanted(*here, 1, late - timedelta(hours=1), "   ", "clave-0001"),
    ):
        with pytest.raises(ValueError):
            reserve(world.a, world.ana, bad)
    with pytest.raises(stock.InsufficientStock):
        reserve(world.a, world.ana, asked(here, 3))
    service, foreign = cell(world.a, "SRV", "SERVICE"), cell(world.b, "B")
    for field, where in (
        ("variant_id", service),
        ("variant_id", (foreign[0], here[1])),  # de otra organización
        ("warehouse_id", (here[0], foreign[1])),
    ):
        with pytest.raises(stock.BadReference) as refused:
            reserve(world.a, world.ana, asked(where))
        assert refused.value.field == field
    with tenant_scope(ctx(world.a)):
        ProductVariant.objects.filter(pk=here[0]).update(status="INACTIVE")
    with pytest.raises(res.VariantInactive):
        reserve(world.a, world.ana, asked(here))
    with tenant_scope(ctx(world.a)):
        ProductVariant.objects.filter(pk=here[0]).update(status="ACTIVE")
        Warehouse.objects.filter(pk=here[1]).update(is_active=False)
    with pytest.raises(stock.WarehouseInactive):
        reserve(world.a, world.ana, asked(here))
    assert consistent(world.a, here) == (2, 0, 2) and ledger(world.a, "RESERVE") == []


def test_release_twice_after_expiry_and_in_an_inactive_warehouse(world: Any) -> None:
    here = stocked(world.a, 3, world.ana)
    first = reserve(world.a, world.ana, asked(here, 2))[0]
    with tenant_scope(ctx(world.a)):
        Warehouse.objects.filter(pk=here[1]).update(is_active=False)
    assert release(world.a, world.ana, first.pk)[1] is True  # también desactivado
    again, now = release(world.a, None, first.pk, "Otra vez")
    assert (now, again.end_reason, again.ended_by_user_id) == (
        False,
        "Ya no lo quiere",
        world.ana.pk,
    )
    assert ledger(world.a, "RELEASE") == [-2] and consistent(world.a, here) == (3, 0, 3)
    with pytest.raises(InventoryReservation.DoesNotExist):
        release(world.b, None, first.pk)  # de otra organización: no existe
    with pytest.raises(ValueError):
        release(world.a, world.ana, first.pk, "  ")
    with tenant_scope(ctx(world.a)):
        Warehouse.objects.filter(pk=here[1]).update(is_active=True)
    for status in ("EXPIRED", "CONSUMED"):
        made = reserve(world.a, world.ana, asked(here, 1))[0]
        with tenant_scope(ctx(world.a)):  # como lo cerrarán el job (F3-34) y la venta
            connection.cursor().execute(
                "UPDATE inventory_reservations SET status = %s, ended_at = now(),"
                " ended_by_type = 'SYSTEM' WHERE id = %s",
                [status, made.pk],
            )
            connection.cursor().execute(
                "UPDATE inventory_levels SET reserved = reserved - 1 WHERE variant_id = %s",
                [here[0]],
            )
        with pytest.raises(res.ReservationNotActive) as refused:
            release(world.a, world.ana, made.pk)
        assert refused.value.status == status
    assert consistent(world.a, here) == (3, 0, 3)


def test_the_same_key_reserves_once_and_another_request_with_it_is_refused(world: Any) -> None:
    here = stocked(world.a, 5, world.ana)
    same = asked(here, 2, key="pedido-0001")
    first, new = reserve(world.a, world.ana, same)
    again, repeated = reserve(world.a, world.ana, same)
    assert (new, repeated, again.pk) == (True, False, first.pk)
    release(world.a, world.ana, first.pk)
    assert reserve(world.a, world.ana, same)[0].status == "RELEASED"  # la de entonces, liberada
    other = stocked(world.a, 1, world.ana, "OTRA")
    for changed in (
        res.Wanted(*here, 3, same.expires_at, same.reason, same.idempotency_key),
        res.Wanted(*other, 2, same.expires_at, same.reason, same.idempotency_key),
        res.Wanted(
            *here, 2, same.expires_at + timedelta(seconds=1), same.reason, same.idempotency_key
        ),
        res.Wanted(*here, 2, same.expires_at, "Otro motivo", same.idempotency_key),
    ):
        with pytest.raises(stock.KeyReused):
            reserve(world.a, world.ana, changed)
    assert ledger(world.a, "RESERVE") == [2] and consistent(world.a, here) == (5, 0, 5)


def test_an_expired_reservation_frees_its_unit_without_the_job(
    world: Any, monkeypatch: pytest.MonkeyPatch, migrator: psycopg.Connection[Any]
) -> None:
    here = stocked(world.a, 1, world.ana)
    old = reserve(world.a, world.ana, asked(here, 1, hours=1))[0]
    with pytest.raises(stock.InsufficientStock):
        reserve(world.a, world.ana, asked(here, 1))
    later = timezone.now() + timedelta(hours=1, seconds=1)
    monkeypatch.setattr(res, "_now", lambda: later)
    made = reserve(world.a, world.ana, asked(here, 1, hours=3))[0]
    with tenant_scope(ctx(world.a)):
        old.refresh_from_db()
        expiry = InventoryMovement.objects.get(movement_type="RELEASE")
    assert (old.status, old.ended_by_type, old.ended_by_user_id, old.end_reason) == (
        "EXPIRED",
        "SYSTEM",
        None,
        res.EXPIRED,
    )
    assert (expiry.idempotency_key, expiry.created_by_type) == (f"R/{old.pk}/release", "SYSTEM")
    assert made.status == "ACTIVE" and consistent(world.a, here) == (1, 1, 0)
    expired = "SELECT count(*) FROM audit_logs WHERE action = 'inventory.reservation_expired'"
    assert migrator.execute(expired).fetchone() == (1,)
    with pytest.raises(res.ReservationNotActive):
        release(world.a, world.ana, old.pk)


def test_a_closed_reservation_never_changes_and_the_runtime_cannot_delete(world: Any) -> None:
    here = stocked(world.a, 2, world.ana)
    made = reserve(world.a, world.ana, asked(here, 1))[0]
    for change in (
        "DELETE FROM inventory_reservations",
        "UPDATE inventory_reservations SET quantity = 2",
    ):
        with pytest.raises(DatabaseError, match="permission denied"), tenant_scope(ctx(world.a)):
            connection.cursor().execute(change)
    release(world.a, world.ana, made.pk)
    for change in (
        "UPDATE inventory_reservations SET status = 'ACTIVE', ended_at = NULL,"
        " ended_by_type = NULL, ended_by_user_id = NULL, end_reason = NULL",
        "UPDATE inventory_reservations SET status = 'EXPIRED'",
    ):
        with pytest.raises(DatabaseError, match="una reserva cerrada"), tenant_scope(ctx(world.a)):
            connection.cursor().execute(change)
    still = reserve(world.a, world.ana, asked(here, 1))[0]
    with pytest.raises(DatabaseError, match="inventory_res_ended_ck"), tenant_scope(ctx(world.a)):
        connection.cursor().execute(  # cerrada sin fecha ni actor del cierre
            "UPDATE inventory_reservations SET status = 'RELEASED' WHERE id = %s", [still.pk]
        )
    with (
        pytest.raises(DatabaseError, match="inventory_movements_sign_ck"),
        tenant_scope(ctx(world.a)),
    ):  # el ledger: RESERVE suma y RELEASE resta, siempre con su reserva
        InventoryMovement.objects.create(
            variant_id=here[0], warehouse_id=here[1], movement_type="RESERVE", quantity=-1,
            on_hand_after=2, reserved_after=0, reference_type="RESERVATION", reference_id=made.pk,
            reason="x", idempotency_key="clave-9999", created_by_type="SYSTEM",
        )  # fmt: skip
    with (
        pytest.raises(DatabaseError, match="inventory_movements_reservation_ck"),
        tenant_scope(ctx(world.a)),
    ):
        InventoryMovement.objects.create(
            variant_id=here[0], warehouse_id=here[1], movement_type="RELEASE", quantity=-1,
            on_hand_after=2, reserved_after=0, reason="x", idempotency_key="clave-9998",
            created_by_type="SYSTEM",
        )  # fmt: skip


def test_reservations_of_another_organization_are_invisible(world: Any) -> None:
    here = stocked(world.a, 2, world.ana)
    made = reserve(world.a, world.ana, asked(here, 1))[0]
    with tenant_scope(ctx(world.b)):
        assert not InventoryReservation.objects.exists()
        cursor = connection.cursor()
        cursor.execute("SELECT count(*) FROM inventory_reservations")
        assert cursor.fetchone() == (0,)
        cursor.execute(
            "UPDATE inventory_reservations SET status = 'RELEASED', ended_at = now(),"
            " ended_by_type = 'SYSTEM' WHERE id = %s",
            [made.pk],
        )
        assert cursor.rowcount == 0
    with pytest.raises(DatabaseError, match="row-level security"), tenant_scope(ctx(world.b)):
        connection.cursor().execute(
            "INSERT INTO inventory_reservations (id, organization_id, variant_id, warehouse_id,"
            " quantity, status, expires_at, reason, idempotency_key, created_by_type, created_at)"
            " VALUES (uuidv7(), %s, %s, %s, 1, 'ACTIVE', %s, 'x', 'clave-0002', 'SYSTEM', now())",
            [world.a, *here, made.expires_at],
        )
    assert consistent(world.a, here) == (2, 1, 1)


def test_two_reservations_of_the_last_unit_at_once_only_one_goes(world: Any) -> None:
    here = stocked(world.a, 1, world.ana)
    first, second = race(world.a, world.ana, [lambda t: res.reserve(t, asked(here)) for _ in "ab"])
    assert first[1] is True and second == "InsufficientStock"
    assert consistent(world.a, here) == (1, 1, 0) and ledger(world.a, "RESERVE") == [1]


def test_eight_at_once_over_the_last_three_units_exactly_three_go(world: Any) -> None:
    here = stocked(world.a, 3, world.ana)
    start, outcome = threading.Barrier(8), list[Any]([None] * 8)

    def run(index: int) -> None:
        try:
            with tenant_scope(ctx(world.a, world.ana)) as tenant:
                start.wait(timeout=10)
                outcome[index] = res.reserve(tenant, asked(here))[1]
        except Exception as error:  # noqa: BLE001 — el resultado lo comprueba el test
            outcome[index] = type(error).__name__
        finally:
            connection.close()

    threads = [threading.Thread(target=run, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert sorted(map(str, outcome)) == ["InsufficientStock"] * 5 + ["True"] * 3
    assert consistent(world.a, here) == (3, 3, 0) and ledger(world.a, "RESERVE") == [1, 1, 1]


@pytest.mark.parametrize("reserve_first", [True, False])
def test_a_reservation_against_the_issue_of_the_last_unit(world: Any, reserve_first: bool) -> None:
    here = stocked(world.a, 1, world.ana)
    hold, issue = (lambda t: res.reserve(t, asked(here))), wanted(here, "ISSUE", 1)
    outcome = race(world.a, world.ana, [hold, issue] if reserve_first else [issue, hold])
    assert outcome[1] == "InsufficientStock"
    assert consistent(world.a, here) == ((1, 1, 0) if reserve_first else (0, 0, 0))


def test_two_releases_at_once_write_one_release(world: Any) -> None:
    here = stocked(world.a, 2, world.ana)
    made = reserve(world.a, world.ana, asked(here, 2))[0]
    first, second = race(world.a, world.ana, [lambda t: res.release(t, made.pk, "Fin")] * 2)
    assert (first[1], second[1], second[0].status) == (True, False, "RELEASED")
    assert ledger(world.a, "RELEASE") == [-2] and consistent(world.a, here) == (2, 0, 2)


def test_the_same_key_at_once_makes_one_reservation(world: Any) -> None:
    here, there = stocked(world.a, 3, world.ana), stocked(world.a, 3, world.ana, "OTRA")
    same = asked(here, 1, key="clave-0001")
    first, second = race(world.a, world.ana, [lambda t: res.reserve(t, same)] * 2)
    assert first[0].pk == second[0].pk and (first[1], second[1]) == (True, False)
    # la misma clave, a la vez, para otra celda: la decide el índice único
    keyed = asked(here, 1, key="clave-0002"), asked(there, 1, key="clave-0002")
    outcome = race(world.a, world.ana, [lambda t, w=w: res.reserve(t, w) for w in keyed])
    assert outcome[1] == "KeyReused"
    assert consistent(world.a, here) == (3, 2, 1) and consistent(world.a, there) == (3, 0, 3)


def test_deleting_the_variant_waits_for_a_reservation_and_the_reverse(world: Any) -> None:
    """Borrar la variante con una reserva en vuelo: el borrado espera y ve lo reservado. Reservar
    con un borrado en vuelo: la reserva espera y ya no encuentra la variante."""
    here = stocked(world.a, 1, world.ana)
    product, other = product_of(world.a, here[0]), sibling(world.a, here[0])
    outcome = race(
        world.a, world.ana, [lambda t: res.reserve(t, asked(here)), drop_variant(product, here[0])]
    )
    assert outcome[1] == "VariantsHeld" and consistent(world.a, here) == (1, 1, 0)
    hold = asked((other, here[1]))
    outcome = race(
        world.a, world.ana, [drop_variant(product, other), lambda t: res.reserve(t, hold)]
    )
    assert outcome[1] == "BadReference"
    with tenant_scope(ctx(world.a)):
        assert InventoryReservation.objects.count() == 1
