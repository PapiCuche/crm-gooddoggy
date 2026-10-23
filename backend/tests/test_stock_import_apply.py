"""F3-21: aplicar una importación de stock (`apps.inventory.imports.apply` y `…/apply/`), contra
PostgreSQL real con el rol `crm_app`, también con dos transacciones a la vez."""

from dataclasses import replace
from typing import Any
from uuid import UUID

import psycopg
import pytest
from django.test import Client
from django.utils import timezone

from apps.catalog.models import ProductVariant
from apps.imports.models import DataImport, DataImportRow
from apps.inventory import imports, stock
from apps.inventory.models import InventoryLevel, InventoryMovement, Warehouse
from core.tenancy.scope import tenant_scope
from tests import test_authorization, test_self_context
from tests.factories import make_user
from tests.test_authorization import give
from tests.test_inventory_stock import cell, level, move, race, wanted
from tests.test_memberships import ctx, join
from tests.test_products import send
from tests.test_self_context import signed
from tests.test_stock_import import ADJUST, IMPORTS
from tests.test_stock_import_preview import preview, rows

world, real_stack = test_authorization.world, test_self_context.real_stack
pytestmark = pytest.mark.usefixtures("tenant_db")
HEADER = "sku,almacen,cantidad\n"


@pytest.fixture
def ana(world: Any) -> Client:
    give(world.a, world.membership, ADJUST)
    return signed(world.ana)


def apply(client: Client, job: DataImport) -> Any:
    return send(client, "post", f"{IMPORTS}{job.pk}/apply/")


def ledger(org: UUID) -> list[tuple[Any, ...]]:
    with tenant_scope(ctx(org)):
        found = InventoryMovement.objects.order_by("id")
        return list(
            found.values_list("quantity", "reference_type", "reference_id", "idempotency_key")
        )


def test_apply_sets_each_cell_and_the_ledger_points_at_the_import(
    ana: Client, world: Any, migrator: psycopg.Connection[Any]
) -> None:
    counted, fresh, same = cell(world.a, "A1"), cell(world.a, "B2"), cell(world.a, "C3")
    move(world.a, world.ana, wanted(counted, "RECEIPT", 2))
    job = preview(world.a, world.ana, HEADER + "C3,ALM,0\nB2,ALM,3\nA1,ALM,5\nNOPE,ALM,1\n")
    response = apply(ana, job)
    assert response.status_code == 200, response.content
    body = response.json()
    assert (body["status"], body["applied_at"] is not None) == ("APPLIED", True)
    assert body["result"] == {"applied": 2, "unchanged": 1, "stale": 0, "failed": 0}
    assert (level(world.a, counted), level(world.a, fresh)) == ((5, 0, 5), (3, 0, 3))
    with tenant_scope(ctx(world.a)):
        assert not InventoryLevel.objects.filter(variant_id=same[0]).exists()
    assert ledger(world.a)[1:] == [  # por variante y almacén (A1 es anterior), no por línea
        (3, "IMPORT", job.pk, f"import:{job.pk}:4"),
        (3, "IMPORT", job.pk, f"import:{job.pk}:3"),
    ]
    found = rows(world.a, job)
    assert {n: (r.action, r.target_entity_type) for n, r in found.items()} == {
        2: ("UNCHANGED", ""),
        3: ("CREATE", "inventory_movement"),
        4: ("UPDATE", "inventory_movement"),
        5: ("ERROR", ""),
    }
    audit = "SELECT entity_id, metadata FROM audit_logs WHERE action = 'inventory.import_applied'"
    assert migrator.execute(audit).fetchall() == [(job.pk, body["result"])]
    with tenant_scope(ctx(world.a)):
        assert DataImport.objects.get().applied_by_user_id == world.ana.pk


def test_applying_again_creates_no_movement(
    ana: Client, world: Any, migrator: psycopg.Connection[Any]
) -> None:
    here = cell(world.a)
    job = preview(world.a, world.ana, HEADER + "IP16,ALM,4\n")
    first = apply(ana, job).json()
    assert apply(ana, job).json() == first and len(ledger(world.a)) == 1
    move(world.a, world.ana, wanted(here, "ISSUE", 1))
    # aunque se perdiera el estado, la clave de cada fila no deja repetir el ajuste
    migrator.execute("UPDATE data_imports SET status = 'VALIDATED'")
    assert apply(ana, job).json()["result"]["applied"] == 1
    assert len(ledger(world.a)) == 2 and level(world.a, here) == (3, 0, 3)


def test_a_cell_that_changed_since_the_preview_is_stale(ana: Client, world: Any) -> None:
    moved, kept, zero = cell(world.a, "A1"), cell(world.a, "B2"), cell(world.a, "C3")
    job = preview(world.a, world.ana, HEADER + "A1,ALM,5\nB2,ALM,6\nC3,ALM,0\n")
    move(world.a, world.ana, wanted(moved, "RECEIPT", 1))
    move(world.a, world.ana, wanted(zero, "RECEIPT", 2))
    assert apply(ana, job).json()["result"] == {
        "applied": 1,
        "unchanged": 0,
        "stale": 2,
        "failed": 0,
    }
    found = rows(world.a, job)
    assert (found[2].action, found[4].action, found[3].action) == ("STALE", "STALE", "CREATE")
    assert found[2].errors == ["El saldo cambió desde la previsualización: era 0, ahora 1."]
    assert (level(world.a, moved), level(world.a, kept), level(world.a, zero)) == (
        (1, 0, 1),
        (6, 0, 6),
        (2, 0, 2),
    )


def test_a_movement_during_the_apply_leaves_the_row_stale(world: Any) -> None:
    """El movimiento bloquea la variante primero: la aplicación espera, lee el saldo que deja y
    no lo pisa."""
    here = cell(world.a)
    job = preview(world.a, world.ana, HEADER + "IP16,ALM,7\n")

    def confirm(tenant: Any) -> DataImport:
        return imports.apply(tenant, import_id=job.pk)

    first, second = race(world.a, world.ana, [wanted(here, "RECEIPT", 2), confirm])
    assert first[1] is True and second.summary["result"]["stale"] == 1
    assert rows(world.a, job)[2].action == "STALE" and level(world.a, here) == (2, 0, 2)


def test_two_applies_at_once_go_one_after_the_other(
    world: Any, migrator: psycopg.Connection[Any]
) -> None:
    here = cell(world.a)
    job = preview(world.a, world.ana, HEADER + "IP16,ALM,7\n")

    def confirm(tenant: Any) -> DataImport:
        return imports.apply(tenant, import_id=job.pk)

    first, second = race(world.a, world.ana, [confirm, confirm])
    assert (first.status, second.status, level(world.a, here)) == ("APPLIED", "APPLIED", (7, 0, 7))
    audit = "SELECT count(*) FROM audit_logs WHERE action = 'inventory.import_applied'"
    assert migrator.execute(audit).fetchone() == (1,) and len(ledger(world.a)) == 1


def test_what_changed_since_the_preview_fails_on_its_row(ana: Client, world: Any) -> None:
    closed, held, gone = cell(world.a, "A1"), cell(world.a, "B2"), cell(world.a, "C3")
    with tenant_scope(ctx(world.a)):
        branch = Warehouse.objects.get(pk=closed[1]).branch_id
        other = Warehouse.objects.create(branch_id=branch, code="OTRO", name="Otro")
    move(world.a, world.ana, wanted(held, "RECEIPT", 5))
    job = preview(world.a, world.ana, HEADER + "A1,OTRO,1\nB2,ALM,4\nC3,ALM,2\n")
    with tenant_scope(ctx(world.a)):
        Warehouse.objects.filter(pk=other.pk).update(is_active=False)
        InventoryLevel.objects.filter(variant_id=held[0]).update(reserved=5)
        ProductVariant.objects.filter(pk=gone[0]).update(deleted_at=timezone.now())
    result = apply(ana, job).json()["result"]
    assert result == {"applied": 0, "unchanged": 0, "stale": 0, "failed": 3}
    assert [r.errors for r in rows(world.a, job).values()] == [
        ["Almacén: está desactivado."],
        ["Cantidad: ahora hay más reservadas que eso."],
        ["La variante o el almacén ya no existen."],
    ]
    assert level(world.a, held) == (5, 5, 0) and len(ledger(world.a)) == 1


def test_the_error_report_cannot_run_formulas(ana: Client, world: Any) -> None:
    cell(world.a)
    job = preview(world.a, world.ana, "almacen,sku,cantidad\nALM,=HYPERLINK(1),1\nALM,IP16,2\n")
    report = ana.get(f"{IMPORTS}{job.pk}/errors/")
    assert report.status_code == 200 and report["Content-Type"] == "text/csv; charset=utf-8"
    assert report.content.decode("utf-8-sig").split("\r\n") == [
        "linea,sku,almacen,cantidad,accion,errores",
        "2,'=HYPERLINK(1),ALM,1,ERROR,SKU: no hay una variante con ese SKU.",
        "",
    ]


def test_it_needs_inventory_adjust_and_stays_in_its_organization(world: Any) -> None:
    bo = make_user(email="bo@example.com")
    join(world.b, bo)
    cell(world.b, "B1")
    theirs = preview(world.b, bo, HEADER + "B1,ALM,1\n")
    give(world.a, world.membership, {"inventory.view": None})
    viewer = signed(world.ana)
    assert apply(viewer, theirs).status_code == 403
    assert viewer.get(f"{IMPORTS}{theirs.pk}/errors/").status_code == 403
    give(world.a, world.membership, ADJUST)
    assert apply(viewer, theirs).status_code == 404
    assert viewer.get(f"{IMPORTS}{theirs.pk}/errors/").status_code == 404
    with tenant_scope(ctx(world.b)):
        assert DataImport.objects.get().status == "VALIDATED"
        assert not DataImportRow.objects.filter(target_entity_id__isnull=False).exists()


def test_the_service_only_takes_manual_or_import_references(world: Any) -> None:
    here = cell(world.a)
    with pytest.raises(ValueError, match="reference_type"):
        move(world.a, world.ana, replace(wanted(here, "RECEIPT", 1), reference_type="ORDER"))
    with pytest.raises(stock.Stale):
        move(world.a, world.ana, replace(wanted(here, "RECEIPT", 1), expected_on_hand=3))
    for reference, key in (("MANUAL", "import:x:2"), ("IMPORT", "manual-0001")):
        with pytest.raises(ValueError, match="import:"):  # nadie ocupa antes una clave de import
            move(
                world.a,
                world.ana,
                replace(wanted(here, "RECEIPT", 1, key), reference_type=reference),
            )
    assert ledger(world.a) == []


def test_a_manual_movement_never_takes_an_import_key(ana: Client, world: Any) -> None:
    here = cell(world.a)
    job = preview(world.a, world.ana, HEADER + "IP16,ALM,4\n")
    body = {"variant_id": str(here[0]), "warehouse_id": str(here[1]), "movement_type": "ADJUSTMENT"}
    body |= {"quantity": 4, "reason": imports.REASON, "idempotency_key": f"import:{job.pk}:2"}
    refused = send(ana, "post", "/api/v1/o/org-a/inventory/movements/", body)
    assert "idempotency_key" in refused.json()["fields"]
    move(world.a, world.ana, wanted(here, "ADJUSTMENT", 4, f"manual:{job.pk}:2"))
    assert apply(ana, job).json()["result"] == {
        "applied": 0,
        "unchanged": 0,
        "stale": 1,
        "failed": 0,
    }


def test_two_imports_of_the_same_cells_in_opposite_order_do_not_deadlock(world: Any) -> None:
    first_cell, second_cell = cell(world.a, "A1"), cell(world.a, "B2")
    one = preview(world.a, world.ana, HEADER + "A1,ALM,5\nB2,ALM,5\n")
    two = preview(world.a, world.ana, HEADER + "B2,ALM,7\nA1,ALM,7\n")

    def confirm(job: DataImport) -> Any:
        return lambda tenant: imports.apply(tenant, import_id=job.pk)

    done = race(world.a, world.ana, [confirm(one), confirm(two)])
    assert [job.summary["result"]["stale"] for job in done] == [0, 2]
    assert (level(world.a, first_cell), level(world.a, second_cell)) == ((5, 0, 5), (5, 0, 5))
