"""F3-20: previsualizar una importación de stock (`apps.inventory.imports.preview`) y sus tablas,
contra PostgreSQL real con el rol `crm_app`."""

from typing import Any
from uuid import UUID

import psycopg
import pytest
from django.db import IntegrityError, connection

from apps.catalog.models import ProductVariant
from apps.imports.csvfile import Unreadable
from apps.imports.models import DataImport, DataImportRow
from apps.inventory import imports
from apps.inventory.models import InventoryLevel, InventoryMovement, Warehouse
from core.tenancy.scope import tenant_scope
from tests import test_authorization
from tests.factories import make_user
from tests.test_import_xlsx import book
from tests.test_inventory_stock import cell, level, move, wanted
from tests.test_memberships import ctx, join

world = test_authorization.world
pytestmark = pytest.mark.usefixtures("tenant_db")


def preview(org: UUID, user: Any, text: str, name: str = "conteo.csv") -> DataImport:
    with tenant_scope(ctx(org, user)) as tenant:
        return imports.preview(tenant, file_name=name, data=text.encode())


def rows(org: UUID, job: DataImport) -> dict[int, DataImportRow]:
    with tenant_scope(ctx(org)):
        return {
            r.row_number: r for r in DataImportRow.objects.filter(data_import=job).order_by("id")
        }


def test_the_preview_checks_every_row_and_leaves_the_stock_alone(
    world: Any, migrator: psycopg.Connection[Any]
) -> None:
    counted, fresh = cell(world.a, "IP16"), cell(world.a, "IP15")
    cell(world.a, "SRV", "SERVICE")
    move(world.a, world.ana, wanted(counted, "RECEIPT", 5))
    with tenant_scope(ctx(world.a)):
        InventoryLevel.objects.filter(variant_id=counted[0]).update(reserved=2)
        branch = Warehouse.objects.get(pk=counted[1]).branch_id
        Warehouse.objects.create(branch_id=branch, code="OFF", name="Cerrado", is_active=False)
        product = ProductVariant.objects.get(pk=fresh[0]).product_id
        ProductVariant.objects.create(product_id=product, sku="IP15-B", name="B")
    text = (
        "SKU;Almacén;Cantidad;Notas\n"
        "ip16; alm ;8;recuento\n"  # 2: la misma celda que la 12
        "IP15;ALM;3\n"  # 3 CREATE: sin saldo
        "IP15-B;ALM;0\n"  # 4 UNCHANGED: sin saldo y 0
        "NOPE;ALM;1\n"  # 5
        "SRV;ALM;1\n"  # 6
        "IP16;XX;1\n"  # 7
        "IP16;OFF;1\n"  # 8
        "IP15-B;ALM;-1\n"  # 9
        "IP15-B;ALM;1.5\n"  # 10
        "IP15-B;ALM;1000001\n"  # 11
        "IP16;ALM;1\n"  # 12: menos que lo reservado
        f";ALM;{'9' * 201}\n"  # 13
    )
    job = preview(world.a, world.ana, text)
    assert (job.status, job.total_rows, job.valid_rows, job.error_rows) == ("VALIDATED", 12, 2, 10)
    assert (job.new_count, job.update_count, job.summary["unchanged"]) == (1, 0, 1)
    assert (job.summary["encoding"], job.summary["separator"]) == ("utf-8", ";")
    assert (job.summary["ignored_columns"], job.summary["duplicate_of"]) == (["Notas"], None)
    found = rows(world.a, job)
    assert list(found) == list(range(2, 14))  # en el orden del archivo
    assert (found[2].action, found[2].raw) == (
        "ERROR",
        {"sku": "ip16", "almacen": " alm ", "cantidad": "8"},
    )
    assert found[2].errors == ["La misma celda (SKU y almacén) que la línea 12."]
    assert (found[3].action, found[3].normalized) == (
        "CREATE",
        {
            "variant_id": str(fresh[0]),
            "warehouse_id": str(fresh[1]),
            "quantity": 3,
            "on_hand_seen": 0,
        },
    )
    assert (found[4].action, found[4].normalized["quantity"]) == ("UNCHANGED", 0)
    assert found[5].errors == ["SKU: no hay una variante con ese SKU."]
    assert found[6].errors == ["SKU: es un servicio, no lleva stock."]
    assert found[7].errors == ["Almacén: no hay un almacén con ese código."]
    assert found[8].errors == ["Almacén: está desactivado."]
    for line in (9, 10, 11):
        assert found[line].errors == ["Cantidad: un entero entre 0 y 1000000, sin decimales."]
    assert found[12].errors == [
        "Cantidad: hay 2 reservadas y la importación no libera reservas.",
        "La misma celda (SKU y almacén) que la línea 2.",
    ]
    assert found[13].errors == ["Alguna celda pasa de 200 caracteres."]
    assert len(found[13].raw["cantidad"]) == 200 and found[13].normalized == {}
    assert level(world.a, counted) == (5, 2, 3)  # nada cambió
    with tenant_scope(ctx(world.a)):
        assert InventoryMovement.objects.count() == 1
    audit = "SELECT entity_id, metadata FROM audit_logs WHERE action = 'inventory.import_uploaded'"
    assert migrator.execute(audit).fetchall() == [
        (job.pk, {"rows": 12, "errors": 10, "repeated": False})
    ]


def test_an_update_and_the_same_file_again_only_warns(world: Any) -> None:
    here = cell(world.a)
    move(world.a, world.ana, wanted(here, "RECEIPT", 2))
    first = preview(world.a, world.ana, "sku,almacen,cantidad\nIP16,ALM,4\n")
    assert (first.update_count, rows(world.a, first)[2].normalized["on_hand_seen"]) == (1, 2)
    again = preview(world.a, world.ana, "sku,almacen,cantidad\nIP16,ALM,4\n", "\x00otro\n.csv")
    assert (first.summary["duplicate_of"], again.summary["duplicate_of"]) == (None, str(first.pk))
    assert again.valid_rows == 1 and again.file_name == "otro.csv"
    dotless = preview(world.a, world.ana, "sku,almacen,cantidad\n\u0131p16,ALM,4\n")  # ıp16
    assert rows(world.a, dotless)[2].errors == ["SKU: no hay una variante con ese SKU."]
    assert (
        preview(world.a, world.ana, "sku,almacen,cantidad\nIP16,ALM,5\n").summary["duplicate_of"]
        is None
    )


def test_an_xlsx_previews_like_its_csv_whatever_its_name(world: Any) -> None:
    """Lo decide la firma del archivo, no su nombre: un XLSX llamado `.csv` y al revés."""
    move(world.a, world.ana, wanted(cell(world.a), "RECEIPT", 2))
    table: list[list[Any]] = [["sku", "Almacén", "cantidad", "nota"], ["IP16", "alm", 4, "x"]]
    table += [["IP16", "ALM", "1.5"], [], ["NOPE", "ALM", 1]]
    csv = "\n".join(",".join(map(str, row)) for row in table).encode()
    jobs = []
    for name, data in (("conteo.xlsx", csv), ("conteo.csv", book(*table))):
        with tenant_scope(ctx(world.a, world.ana)) as tenant:
            jobs.append(imports.preview(tenant, file_name=name, data=data))
    seen = [
        [(r.row_number, r.action, r.raw, r.normalized, r.errors) for r in rows(world.a, j).values()]
        for j in jobs
    ]
    assert seen[0] == seen[1] and [line for line, *_ in seen[0]] == [2, 3, 5]
    counts = [
        (j.total_rows, j.error_rows, j.update_count, j.summary["ignored_columns"]) for j in jobs
    ]
    assert counts == [(3, 2, 1, ["nota"])] * 2
    assert [j.summary["encoding"] for j in jobs] == ["utf-8", "xlsx"]


def test_a_file_that_cannot_be_read_saves_nothing(
    world: Any, migrator: psycopg.Connection[Any]
) -> None:
    for text in ("", "sku,cantidad\nA,1\n", "sku,almacen,cantidad\n\n"):
        with pytest.raises(Unreadable):
            preview(world.a, world.ana, text)
    assert migrator.execute("SELECT count(*) FROM data_imports").fetchone() == (0,)
    assert imports.template() == "\ufeffsku,almacen,cantidad\r\n".encode()


def test_each_organization_sees_only_its_imports(world: Any) -> None:
    bo = make_user(email="bo@example.com")
    join(world.b, bo)
    cell(world.b, "B1")
    theirs = preview(world.b, bo, "sku,almacen,cantidad\nB1,ALM,1\n")
    mine = preview(world.a, world.ana, "sku,almacen,cantidad\nB1,ALM,1\n")
    assert rows(world.a, mine)[2].errors == [  # ni su SKU ni su almacén son de A
        "SKU: no hay una variante con ese SKU.",
        "Almacén: no hay un almacén con ese código.",
    ]
    with tenant_scope(ctx(world.a)), connection.cursor() as cursor:  # RLS, sin el manager
        cursor.execute(
            "SELECT id FROM data_imports UNION ALL SELECT data_import_id FROM data_import_rows"
        )
        assert cursor.fetchall() == [(mine.pk,), (mine.pk,)]
    with pytest.raises(IntegrityError), tenant_scope(ctx(world.a)):  # FK compuesta
        DataImportRow.objects.create(data_import_id=theirs.pk, row_number=9, raw={}, action="SKIP")
    with pytest.raises(IntegrityError):  # quien la sube es miembro de la organización
        preview(world.a, bo, "sku,almacen,cantidad\nB1,ALM,1\n")
