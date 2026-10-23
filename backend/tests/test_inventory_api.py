"""F3-11: las rutas de existencias y movimientos (`…/inventory/`) y borrar un almacén. Middleware,
sesión, motor de autorización y PostgreSQL con el rol `crm_app`."""

from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.catalog.models import ProductVariant
from apps.inventory.models import InventoryLevel, Warehouse
from apps.inventory.warehouses import delete_warehouse
from core.tenancy.scope import tenant_scope
from tests import test_authorization, test_self_context
from tests.test_authorization import give
from tests.test_inventory_stock import cell, level, move, race, wanted
from tests.test_memberships import ctx
from tests.test_products import send
from tests.test_self_context import signed

world, real_stack = test_authorization.world, test_self_context.real_stack
pytestmark = pytest.mark.usefixtures("tenant_db")
MOVEMENTS, LEVELS = "/api/v1/o/org-a/inventory/movements/", "/api/v1/o/org-a/inventory/levels/"
VIEW: dict[str, str | None] = {"inventory.view": None}
ADJUST: dict[str, str | None] = {**VIEW, "inventory.adjust": None, "warehouses.manage": None}


def post(client: Client, where: tuple[UUID, UUID], kind: str, **extra: Any) -> Any:
    body: dict[str, Any] = {"variant_id": str(where[0]), "warehouse_id": str(where[1])}
    body["movement_type"] = kind
    body |= {"quantity": 1, "reason": "Compra", "idempotency_key": uuid4().hex, **extra}
    return send(client, "post", MOVEMENTS, body)


@pytest.fixture
def ana(world: Any) -> Client:
    give(world.a, world.membership, ADJUST)
    return signed(world.ana)


def test_movements_levels_and_the_ledger(ana: Client, world: Any) -> None:
    here = cell(world.a)
    first = post(ana, here, "RECEIPT", quantity=10, idempotency_key="compra-0001")
    assert first.status_code == 201, first.content
    assert first.json()["created_by_user_id"] == str(world.ana.pk)
    again = post(ana, here, "RECEIPT", quantity=10, idempotency_key="compra-0001")
    assert again.status_code == 200 and again.json() == first.json()  # no se repite
    assert post(ana, here, "ISSUE", quantity=3).status_code == 201
    counted = post(ana, here, "ADJUSTMENT", quantity=5).json()
    assert (counted["quantity"], counted["on_hand_after"]) == (-2, 5)
    post(ana, cell(world.a, "OTRA"), "RECEIPT")  # otra variante, en el mismo almacén
    ledger = ana.get(MOVEMENTS, {"variant_id": str(here[0])}).json()["results"]
    assert [row["quantity"] for row in ledger] == [-2, -3, 10]  # el más reciente primero
    rows = ana.get(LEVELS, {"warehouse_id": str(here[1])}).json()["results"]
    assert sorted((r["on_hand"], r["reserved"], r["available"]) for r in rows) == [
        (1, 0, 1),
        (5, 0, 5),
    ]
    assert ana.get(LEVELS, {"variant_id": str(uuid4())}).json()["results"] == []
    assert ana.get(LEVELS, {"variant_id": "x"}).status_code == 400


def test_each_refusal_has_its_code_or_its_field(ana: Client, world: Any) -> None:
    here, service, foreign = cell(world.a), cell(world.a, "SRV", "SERVICE"), cell(world.b, "B")
    assert post(ana, here, "ISSUE").json()["code"] == "INSUFFICIENT_STOCK"
    post(ana, here, "RECEIPT", idempotency_key="compra-0001")
    reused = post(ana, here, "RECEIPT", quantity=2, idempotency_key="compra-0001")
    assert reused.status_code == 409 and reused.json()["code"] == "IDEMPOTENCY_KEY_REUSED"
    for field, where in (
        ("variant_id", service),
        ("variant_id", (foreign[0], here[1])),
        ("warehouse_id", (here[0], foreign[1])),
    ):
        assert field in post(ana, where, "RECEIPT").json()["fields"]
    for bad in (
        {"quantity": 0},
        {"movement_type": "SALE"},
        {"reason": " "},
        {"idempotency_key": "x"},
    ):
        assert post(ana, here, "RECEIPT", **bad).json()["fields"].keys() == bad.keys()
    with tenant_scope(ctx(world.a)):
        Warehouse.objects.filter(pk=here[1]).update(is_active=False)
    assert post(ana, here, "RECEIPT").json()["code"] == "WAREHOUSE_INACTIVE"
    assert level(world.a, here) == (1, 0, 1)


def test_a_warehouse_with_stock_is_not_deleted(
    ana: Client, world: Any, migrator: psycopg.Connection[Any]
) -> None:
    here = cell(world.a)
    item = f"/api/v1/o/org-a/warehouses/{here[1]}/"
    post(ana, here, "RECEIPT")
    refused = send(ana, "delete", item)
    assert refused.status_code == 409 and refused.json()["code"] == "WAREHOUSE_HAS_STOCK"
    post(ana, here, "ISSUE")
    assert send(ana, "delete", item).status_code == 204
    audit = "SELECT count(*) FROM audit_logs WHERE action = 'warehouse.deleted'"
    assert migrator.execute(audit).fetchone() == (1,)
    assert send(ana, "delete", item).status_code == 404
    assert "warehouse_id" in post(ana, here, "RECEIPT").json()["fields"]  # ya no existe


def test_deleting_waits_for_a_movement_in_flight(world: Any) -> None:
    """El borrado (`FOR UPDATE`) y el movimiento (`FOR KEY SHARE` del almacén) se esperan."""
    here = cell(world.a)

    def delete(tenant: Any) -> None:
        delete_warehouse(tenant, warehouse_id=here[1])

    outcome = race(world.a, world.ana, [wanted(here, "RECEIPT", 1), delete])
    assert outcome[1] == "WarehouseHasStock"
    move(world.a, world.ana, wanted(here, "ISSUE", 1))
    with tenant_scope(ctx(world.a)):
        assert InventoryLevel.objects.get().on_hand == 0
    # y al revés: el movimiento que llega mientras se borra ya no encuentra el almacén
    outcome = race(world.a, world.ana, [delete, wanted(here, "RECEIPT", 1)])
    assert outcome[1] == "BadReference" and level(world.a, here) == (0, 0, 0)


def test_permissions_and_isolation(world: Any) -> None:
    viewer, here = signed(world.ana), cell(world.a)
    assert viewer.get(LEVELS).status_code == 403
    give(world.a, world.membership, VIEW)
    assert post(viewer, here, "RECEIPT").status_code == 403
    assert send(viewer, "delete", f"/api/v1/o/org-a/warehouses/{here[1]}/").status_code == 403
    move(world.a, world.ana, wanted(here, "RECEIPT", 1))
    move(world.b, None, wanted(cell(world.b, "B"), "RECEIPT", 7))
    assert [row["on_hand"] for row in viewer.get(LEVELS).json()["results"]] == [1]
    assert [row["quantity"] for row in viewer.get(MOVEMENTS).json()["results"]] == [1]


def test_inventory_view_alone_reads_only_what_identifies_the_stock(world: Any) -> None:
    """Decisión del mantenedor (2026-10-22): `inventory.view` basta para leer el SKU y el nombre
    de las variantes con stock, también en un rol a medida sin `products.view`. Nada más: ni el
    catálogo, ni costos, ni precios; y nunca filas de otra organización."""
    viewer, mine, theirs = signed(world.ana), cell(world.a), cell(world.b, "B")
    give(world.a, world.membership, VIEW)  # rol a medida: solo `inventory.view`
    move(world.a, world.ana, wanted(mine, "RECEIPT", 2))
    move(world.b, None, wanted(theirs, "RECEIPT", 9))
    assert viewer.get("/api/v1/o/org-a/products/").status_code == 403  # el catálogo, no
    for url in (LEVELS, MOVEMENTS):
        (row,) = viewer.get(url).json()["results"]  # la de org-b no llega
        assert set(row["variant"]) == {"id", "sku", "name", "product_id", "is_deleted"}
        assert (row["variant"]["sku"], row["variant"]["name"]) == ("IP16", "IP16")
        assert not {"cost", "price", "margin", "supplier"} & {*row, *row["variant"]}
        assert viewer.get(url, {"variant_id": str(theirs[0])}).json()["results"] == []


def test_each_row_names_its_variant_without_a_query_per_row(ana: Client, world: Any) -> None:
    """F3-19: el SKU y el nombre de la variante llegan con cada fila, en la misma consulta."""
    first = cell(world.a)
    created = post(ana, first, "RECEIPT", quantity=4).json()
    assert created["variant"] == {
        "id": str(first[0]),
        "sku": "IP16",
        "name": "IP16",
        "product_id": created["variant"]["product_id"],
        "is_deleted": False,
    }

    def queries(url: str) -> tuple[int, list[dict[str, Any]]]:
        with CaptureQueriesContext(connection) as captured:
            response = ana.get(url, {"warehouse_id": str(first[1])})
        assert response.status_code == 200, response.content
        return len(captured), response.json()["results"]

    few = {url: queries(url)[0] for url in (LEVELS, MOVEMENTS)}
    for index in range(5):
        post(ana, cell(world.a, f"SKU{index}"), "RECEIPT", quantity=index + 1)
    for url in (LEVELS, MOVEMENTS):
        count, rows = queries(url)
        assert count == few[url] and len(rows) == 6  # seis filas, las mismas consultas que una
        assert {row["variant"]["sku"] for row in rows} == {"IP16", *(f"SKU{i}" for i in range(5))}
        assert all(row["variant"]["id"] == row["variant_id"] for row in rows)
    with tenant_scope(ctx(world.a)):  # borrada en el catálogo: sigue, marcada
        ProductVariant.objects.filter(pk=first[0]).update(deleted_at=timezone.now())
    for url in (LEVELS, MOVEMENTS):
        rows = ana.get(url, {"variant_id": str(first[0])}).json()["results"]
        assert [row["variant"]["is_deleted"] for row in rows] == [True]


def test_levels_with_or_without_stock(ana: Client, world: Any) -> None:
    here, other = cell(world.a), cell(world.a, "OTRA")
    post(ana, here, "RECEIPT", quantity=2)
    post(ana, other, "RECEIPT")
    post(ana, other, "ISSUE")  # se queda en cero: la fila sigue

    def skus(**filters: str) -> list[str]:
        rows = ana.get(LEVELS, {"warehouse_id": str(here[1]), **filters}).json()["results"]
        return sorted(row["variant"]["sku"] for row in rows)

    assert skus() == ["IP16", "OTRA"]
    assert skus(has_stock="true") == ["IP16"]
    assert skus(has_stock="false") == ["OTRA"]
    refused = ana.get(LEVELS, {"has_stock": "quizá"})
    assert refused.status_code == 400 and list(refused.json()["fields"]) == ["has_stock"]
    assert ana.get(MOVEMENTS, {"has_stock": "true"}).status_code == 200  # no es del ledger
