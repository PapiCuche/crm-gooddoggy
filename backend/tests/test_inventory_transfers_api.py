"""F3-31: la ruta de transferencias (`…/inventory/transfers/`) y el ledger por grupo. Middleware,
sesión, motor de autorización y PostgreSQL con el rol `crm_app`, también a la vez por HTTP."""

import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from django.db import connection
from django.test import Client
from django.utils import timezone

from apps.inventory import stock
from apps.inventory.models import InventoryLevel, Warehouse
from core.tenancy.scope import tenant_scope
from tests import test_authorization, test_self_context
from tests.factories import make_user
from tests.test_authorization import give
from tests.test_inventory_stock import cell
from tests.test_inventory_transfers import asked, on_hand, send, site, stocked, written
from tests.test_memberships import ctx, join
from tests.test_products import send as call
from tests.test_self_context import reply, signed

world, real_stack = test_authorization.world, test_self_context.real_stack
pytestmark = pytest.mark.usefixtures("tenant_db")
URL, LEDGER = "/api/v1/o/org-a/inventory/transfers/", "/api/v1/o/org-a/inventory/movements/"
TRANSFER: dict[str, str | None] = {"inventory.view": None, "inventory.transfer": None}
OTHERS: dict[str, str | None] = {  # todo el inventario menos transferir
    **{"inventory.view": None, "inventory.adjust": None, "inventory.manage": None},
    "warehouses.manage": None,
}
COUNTS = (  # lo que una petición rechazada no debe tocar
    "SELECT (SELECT count(*) FROM inventory_movements), (SELECT count(*) FROM audit_logs),"
    " (SELECT sum(on_hand) FROM inventory_levels), (SELECT count(*) FROM inventory_levels)"
)


def body(variant: Any, source: Any, target: Any, **extra: Any) -> dict[str, Any]:
    ids = {"variant_id": str(variant), "from_warehouse_id": str(source)}
    base = {"to_warehouse_id": str(target), "quantity": 1, "reason": "Reponer tienda"}
    return {**ids, **base, "idempotency_key": uuid4().hex, **extra}


def post(client: Client, *where: Any, **extra: Any) -> Any:
    return call(client, "post", URL, body(*where, **extra))


def only_transfer(world: Any) -> Client:
    other = make_user(email="mover@example.com")
    give(world.a, join(world.a, other).pk, {"inventory.transfer": None})
    return signed(other)


@pytest.fixture
def ana(world: Any) -> Client:
    give(world.a, world.membership, TRANSFER)
    return signed(world.ana)


@pytest.fixture
def luis(world: Any) -> Client:
    other = make_user(email="luis@example.com")
    give(world.a, join(world.a, other).pk, TRANSFER)
    return signed(other)


def test_a_transfer_over_http_and_its_two_legs_in_the_ledger(ana: Client, world: Any) -> None:
    variant, origin, (shop,) = stocked(world.a, 10, "TDA")
    asked_ = body(variant, origin, shop, quantity=4, idempotency_key="traslado-01")
    made = call(ana, "post", URL, asked_)
    assert made.status_code == 201, made.content
    data = made.json()
    assert set(data) == {"transfer_group_id", "legs"}
    out, entry = data["legs"]
    assert set(out) == {  # sin clave de idempotencia, organización ni costos
        *("id", "variant_id", "variant", "warehouse_id", "movement_type", "quantity"),
        *("on_hand_after", "reserved_after", "reason", "created_by_user_id", "created_at"),
        *("reference_type", "reference_id", "transfer_group_id"),
    }
    assert [(m["movement_type"], m["warehouse_id"], m["quantity"]) for m in (out, entry)] == [
        ("TRANSFER_OUT", str(origin), -4),
        ("TRANSFER_IN", str(shop), 4),
    ]
    group = data["transfer_group_id"]
    links = {(m["reference_type"], m["reference_id"], m["transfer_group_id"]) for m in data["legs"]}
    assert links == {("TRANSFER", group, group)}
    assert out["created_by_user_id"] == str(world.ana.pk) and out["variant"]["sku"] == "IP16"
    again = call(ana, "post", URL, asked_)
    assert (again.status_code, again.json()) == (200, data)  # no se repite
    send(world.a, None, asked(variant, origin, shop, 1))  # otra transferencia: otro grupo
    rows = ana.get(LEDGER, {"transfer_group_id": group}).json()["results"]
    assert sorted(r["id"] for r in rows) == sorted(m["id"] for m in data["legs"])
    assert on_hand(world.a, variant, origin, shop) == [5, 5] and written(world.a) == (4, 2)


def test_the_ledger_filter_by_group(ana: Client, world: Any) -> None:
    variant, origin, (shop,) = stocked(world.a, 3, "TDA")
    mine = send(world.a, None, asked(variant, origin, shop, 1))[0]
    theirs_variant, theirs_origin, (theirs_shop,) = stocked(world.b, 3, "TDB")
    theirs = send(world.b, None, asked(theirs_variant, theirs_origin, theirs_shop, 1))[0]
    found = ana.get(LEDGER, {"transfer_group_id": str(mine[0].transfer_group_id)}).json()
    assert len(found["results"]) == 2 and "next" in found  # la paginación de ADR-016
    for group in (theirs[0].transfer_group_id, uuid4()):  # de otra organización = inventado
        assert ana.get(LEDGER, {"transfer_group_id": str(group)}).json()["results"] == []
    bad = [ana.get(LEDGER, {"transfer_group_id": v}) for v in ("x", "12345", "<b>hola</b>", "")]
    assert {(r.status_code, r.content) for r in bad} == {(bad[0].status_code, bad[0].content)}
    assert bad[0].status_code == 400 and list(bad[0].json()["fields"]) == ["transfer_group_id"]
    assert b"hola" not in bad[2].content
    assert only_transfer(world).get(LEDGER).status_code == 403  # transferir no lee el ledger


def test_permissions_and_methods(world: Any, migrator: psycopg.Connection[Any]) -> None:
    variant, origin, (shop,) = stocked(world.a, 5, "TDA")
    anonymous = Client(enforce_csrf_checks=True)
    assert reply(anonymous.get(URL))[0] == 401
    assert call(anonymous, "post", URL, body(variant, origin, shop)).status_code == 401
    member = signed(world.ana)
    before = migrator.execute(COUNTS).fetchone()
    assert post(member, variant, origin, shop).status_code == 403  # sin permisos
    give(world.a, world.membership, OTHERS)  # ajustar, gestionar y ver no transfieren
    refused = post(member, variant, origin, shop)
    assert (refused.status_code, refused.json()["code"]) == (403, "PERMISSION_DENIED")
    assert call(member, "post", URL, {"quantity": "nada"}).status_code == 403  # antes del cuerpo
    assert migrator.execute(COUNTS).fetchone() == before
    mover = only_transfer(world)  # basta, sin ver ni ajustar
    assert post(mover, variant, origin, shop).status_code == 201
    for method in ("get", "patch", "put", "delete"):
        assert call(mover, method, URL, {}).status_code in (403, 405), method
    assert call(member, "post", URL.replace("org-a", "org-b"), {}).status_code == 404


@pytest.mark.parametrize(
    ("field", "value"),
    [
        *(("quantity", v) for v in ("5", 5.0, True, 0, -1, stock.QUANTITY_MAX + 1, None, [1])),
        *(("variant_id", v) for v in ("no-es-uuid", 5, None)),
        *(("from_warehouse_id", v) for v in ("", 5, None)),
        *(("to_warehouse_id", v) for v in ("x", 5, None)),
        *(("reason", v) for v in (" ", "", "x" * 256, "a\x00b", None, 7, ["Reponer"])),
        *(
            ("idempotency_key", v)
            for v in ("corta", "import:abcdefgh", "con/barra-01", "a b c d e f", 12345678, None)
        ),
        ("organization_id", "00000000-0000-0000-0000-000000000000"),
        ("movement_type", "RECEIPT"),
    ],
)
def test_a_bad_body_is_a_400_on_its_field_and_writes_nothing(
    ana: Client, world: Any, migrator: psycopg.Connection[Any], field: str, value: Any
) -> None:
    variant, origin, (shop,) = stocked(world.a, 2, "TDA")
    before = migrator.execute(COUNTS).fetchone()
    refused = post(ana, variant, origin, shop, **{field: value})
    assert refused.status_code == 400 and list(refused.json()["fields"]) == [field], refused.content
    assert migrator.execute(COUNTS).fetchone() == before


def test_the_same_warehouse_and_a_missing_field(ana: Client, world: Any) -> None:
    variant, origin, (shop,) = stocked(world.a, 2, "TDA")
    same = post(ana, variant, origin, origin)
    assert (same.status_code, list(same.json()["fields"])) == (400, ["to_warehouse_id"])
    for field in body(variant, origin, shop):
        wanted = body(variant, origin, shop)
        del wanted[field]
        assert list(call(ana, "post", URL, wanted).json()["fields"]) == [field]
    assert call(ana, "post", URL, [1]).status_code == 400
    number, unknown = (post(ana, variant, origin, shop, variant_id=v) for v in (5, str(uuid4())))
    assert number.json()["fields"] != unknown.json()["fields"]  # un número no se busca como id
    assert written(world.a) == (0, 0)


def test_unknown_references_look_the_same_whoever_owns_them(
    ana: Client, world: Any, migrator: psycopg.Connection[Any]
) -> None:
    variant, origin, (shop,) = stocked(world.a, 2, "TDA")
    foreign = cell(world.b, "B")
    service = cell(world.a, "SRV", "SERVICE")[0]
    before = migrator.execute(COUNTS).fetchone()
    for field, theirs in (
        ("variant_id", foreign[0]),
        ("from_warehouse_id", foreign[1]),
        ("to_warehouse_id", foreign[1]),
    ):
        one, two = (post(ana, variant, origin, shop, **{field: str(x)}) for x in (theirs, uuid4()))
        assert (one.status_code, one.content) == (two.status_code, two.content)
        assert list(one.json()["fields"]) == [field]
    assert list(post(ana, service, origin, shop).json()["fields"]) == ["variant_id"]
    with tenant_scope(ctx(world.a)):  # borrado: lo mismo que si no existiera
        Warehouse.objects.filter(pk=shop).update(deleted_at=timezone.now())
    assert list(post(ana, variant, origin, shop).json()["fields"]) == ["to_warehouse_id"]
    assert migrator.execute(COUNTS).fetchone() == before
    assert on_hand(world.b, foreign[0], foreign[1]) == [0]  # lo ajeno nunca se mueve


def test_the_conflicts_carry_their_code_and_no_figures(
    ana: Client, luis: Client, world: Any, migrator: psycopg.Connection[Any]
) -> None:
    variant, origin, (shop, closed) = stocked(world.a, 3, "TDA", "CERR")
    big = post(ana, variant, origin, shop, quantity=4)
    assert (big.status_code, big.json()["code"]) == (409, "INSUFFICIENT_STOCK")
    assert not any(ch.isdigit() for ch in big.json()["message"])
    with tenant_scope(ctx(world.a)):
        Warehouse.objects.filter(pk=closed).update(is_active=False)
    sides = [post(ana, variant, origin, closed), post(ana, variant, closed, origin)]
    assert [(r.status_code, r.json()["code"]) for r in sides] == [(409, "WAREHOUSE_INACTIVE")] * 2
    assert "destino" in sides[0].json()["message"] and "origen" in sides[1].json()["message"]
    with tenant_scope(ctx(world.a)):
        InventoryLevel.objects.create(
            variant_id=variant, warehouse_id=shop, on_hand=stock.ON_HAND_MAX
        )
    full = post(ana, variant, origin, shop)
    assert (full.status_code, full.json()["code"]) == (409, "STOCK_LIMIT")
    assert written(world.a) == (0, 0)
    target = site(world.a, (variant, origin), "OTRO")
    same = body(variant, origin, target, idempotency_key="traslado-02")
    first = call(ana, "post", URL, same)
    assert first.status_code == 201
    before = migrator.execute(COUNTS).fetchone()
    for client, change in ((ana, {"quantity": 2}), (ana, {"reason": "Otro"}), (luis, {})):
        reused = call(client, "post", URL, {**same, **change})
        assert (reused.status_code, reused.json()["code"]) == (409, "IDEMPOTENCY_KEY_REUSED")
        assert first.json()["transfer_group_id"] not in reused.text  # nunca las patas ajenas
    assert migrator.execute(COUNTS).fetchone() == before


def at_once(*calls: Any) -> list[tuple[int, Any]]:
    start = threading.Barrier(len(calls))

    def run(request: Any) -> tuple[int, Any]:
        try:
            start.wait(timeout=10)
            made = request()
            data = made.json()
            return made.status_code, data.get("transfer_group_id") or data.get("code")
        finally:
            connection.close()

    with ThreadPoolExecutor(len(calls)) as pool:
        return list(pool.map(run, calls))


def test_the_same_key_at_once_over_http(ana: Client, luis: Client, world: Any) -> None:
    variant, origin, (shop,) = stocked(world.a, 6, "TDA")
    same = body(variant, origin, shop, quantity=2)
    first, second = at_once(*[lambda: call(ana, "post", URL, same)] * 2)
    assert sorted((first[0], second[0])) == [200, 201] and first[1] == second[1]
    theirs = body(variant, origin, shop, quantity=2)
    outcome = at_once(
        lambda: call(ana, "post", URL, theirs), lambda: call(luis, "post", URL, theirs)
    )
    assert sorted(code for code, _ in outcome) == [201, 409]
    assert "IDEMPOTENCY_KEY_REUSED" in [why for _, why in outcome]
    assert on_hand(world.a, variant, origin, shop) == [2, 4] and written(world.a) == (4, 2)


def test_the_last_unit_sent_twice_over_http_leaves_once(
    ana: Client, luis: Client, world: Any
) -> None:
    variant, origin, (shop, other) = stocked(world.a, 1, "TDA", "OTRO")
    outcome = at_once(
        lambda: post(ana, variant, origin, shop), lambda: post(luis, variant, origin, other)
    )
    assert sorted(code for code, _ in outcome) == [201, 409]
    assert "INSUFFICIENT_STOCK" in [why for _, why in outcome]
    assert sorted(on_hand(world.a, variant, origin, shop, other)) == [0, 0, 1]
