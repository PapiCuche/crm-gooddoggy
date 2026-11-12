"""F3-35: las rutas de reservas (`…/inventory/reservations/`): middleware, sesión, motor de
autorización y PostgreSQL con el rol `crm_app`, también con peticiones a la vez por HTTP."""

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from django.db import connection
from django.test import Client
from django.utils import timezone

from apps.catalog.models import ProductVariant
from apps.inventory import reservations as res
from apps.inventory.models import Warehouse
from core.tenancy.scope import tenant_scope
from tests import test_authorization, test_self_context
from tests.factories import make_user
from tests.test_authorization import give
from tests.test_inventory_reservations import consistent, ledger, stocked
from tests.test_inventory_stock import cell
from tests.test_memberships import ctx, join
from tests.test_products import send
from tests.test_self_context import reply, signed

world, real_stack = test_authorization.world, test_self_context.real_stack
pytestmark = pytest.mark.usefixtures("tenant_db")
URL, LEVELS = "/api/v1/o/org-a/inventory/reservations/", "/api/v1/o/org-a/inventory/levels/"
VIEW: dict[str, str | None] = {"inventory.view": None}
MANAGE: dict[str, str | None] = {**VIEW, "inventory.manage": None}
KEY_REUSED = "IDEMPOTENCY_KEY_REUSED"
COUNTS = (  # lo que una petición rechazada no debe tocar
    "SELECT (SELECT count(*) FROM inventory_reservations), (SELECT count(*) FROM"
    " inventory_movements), (SELECT count(*) FROM audit_logs), (SELECT sum(reserved) FROM"
    " inventory_levels)"
)


def body(where: tuple[Any, Any], **extra: Any) -> dict[str, Any]:
    soon = (timezone.now() + timedelta(hours=2)).isoformat()
    base = {"variant_id": str(where[0]), "warehouse_id": str(where[1]), "quantity": 1}
    return {
        **base,
        **{"expires_at": soon, "reason": "Para Ana", "idempotency_key": uuid4().hex},
        **extra,
    }


def reserve(client: Client, where: tuple[Any, Any], **extra: Any) -> Any:
    return send(client, "post", URL, {**body(where), **extra})


def release(client: Client, pk: Any, **extra: Any) -> Any:
    return send(client, "post", f"{URL}{pk}/release/", {"reason": "Ya no", **extra})


def states(migrator: psycopg.Connection[Any]) -> dict[str, Any]:
    rows = migrator.execute("SELECT id, status, ended_by_user_id FROM inventory_reservations")
    return {str(pk): (status, ender) for pk, status, ender in rows.fetchall()}


@pytest.fixture
def ana(world: Any) -> Client:
    give(world.a, world.membership, MANAGE)
    return signed(world.ana)


@pytest.fixture
def bruno(world: Any) -> Client:
    other = make_user(email="bruno@example.com")
    give(world.a, join(world.a, other).pk, MANAGE)
    return signed(other)


def test_reserve_and_release_move_the_cell(ana: Client, world: Any) -> None:
    here = stocked(world.a, 5, world.ana)
    asked = body(here, quantity=2, idempotency_key="pedido-0001")
    made = send(ana, "post", URL, asked)
    assert made.status_code == 201, made.content
    data = made.json()
    assert set(data) == {  # sin clave de idempotencia, organización, costos ni precios
        *("id", "variant_id", "variant", "warehouse_id", "quantity", "status", "expires_at"),
        *("reason", "created_by_user_id", "created_at", "ended_at", "ended_by_user_id"),
        "end_reason",
    }
    assert data["status"] == "ACTIVE" and data["created_by_user_id"] == str(world.ana.pk)
    assert set(data["variant"]) == {"id", "sku", "name", "product_id", "is_deleted"}
    row = ana.get(LEVELS, {"variant_id": str(here[0])}).json()["results"][0]
    assert (row["on_hand"], row["reserved"], row["available"]) == (5, 2, 3)
    ended = release(ana, data["id"])
    assert (ended.status_code, ended.json()["ended_by_user_id"]) == (200, str(world.ana.pk))
    assert release(ana, data["id"], reason="Otra vez").json() == ended.json()  # no escribe
    assert ledger(world.a, "RELEASE") == [-2] and consistent(world.a, here) == (5, 0, 5)
    after = send(ana, "post", URL, asked)  # la clave, ya liberada
    assert (after.status_code, after.json()["status"]) == (200, "RELEASED")


def test_permissions_and_methods(
    world: Any, bruno: Client, migrator: psycopg.Connection[Any]
) -> None:
    here = stocked(world.a, 2, world.ana)
    anonymous = Client(enforce_csrf_checks=True)
    assert reply(anonymous.get(URL))[0] == 401  # ni el listado (F3-35b)
    assert send(anonymous, "post", URL, body(here)).status_code == 401
    member = signed(world.ana)  # miembro sin permisos
    assert reserve(member, here).status_code == 403
    give(world.a, world.membership, {**VIEW, "inventory.adjust": None, "inventory.transfer": None})
    made = reserve(bruno, here).json()["id"]
    before = migrator.execute(COUNTS).fetchone()
    assert reserve(member, here).status_code == 403  # ver y ajustar no reservan ni liberan
    assert release(member, made).status_code == 403
    give(world.a, world.membership, MANAGE)  # y con permiso, el resto de métodos no existe
    for method in ("get", "patch", "put", "delete"):
        for path in (URL, f"{URL}{made}/release/")[method == "get" :]:  # listar: F3-35b
            assert send(member, method, path, {}).status_code in (403, 405), (method, path)
        assert send(member, method, f"{URL}{made}/", {}).status_code == 404  # sin ruta
    assert migrator.execute(COUNTS).fetchone() == before
    assert signed(world.ana).get("/api/v1/o/org-b/inventory/reservations/").status_code == 404


def test_a_manager_may_release_a_colleagues_reservation_and_the_replay_is_not_shared(
    ana: Client, bruno: Client, world: Any
) -> None:
    here = stocked(world.a, 3, world.ana)
    same = body(here, idempotency_key="pedido-0001")
    first = send(ana, "post", URL, same)
    assert send(ana, "post", URL, same).json()["id"] == first.json()["id"]
    refused = send(bruno, "post", URL, same)  # lo mismo, de otra persona: no recibe la ajena
    assert (refused.status_code, refused.json()["code"]) == (409, "IDEMPOTENCY_KEY_REUSED")
    assert first.json()["id"] not in refused.text
    later = (timezone.now() + timedelta(hours=3)).isoformat()  # un reintento con otro `expires_at`
    for change in ({"quantity": 2}, {"expires_at": later}):
        changed = send(ana, "post", URL, {**same, **change})
        assert (changed.status_code, changed.json()["code"]) == (409, "IDEMPOTENCY_KEY_REUSED")
    ended = release(bruno, first.json()["id"])
    other = ended.json()["ended_by_user_id"] != first.json()["created_by_user_id"]
    assert (ended.status_code, other) == (200, True)
    assert release(ana, first.json()["id"]).json() == ended.json()  # ya liberada: la guardada


@pytest.mark.parametrize(
    ("field", "value"),
    [
        *(("quantity", v) for v in ("5", 5.0, True, 0, -1, 1_000_001, 2**63, None, [1])),
        *(("variant_id", v) for v in ("no-es-uuid", 5, None)),
        *(("warehouse_id", v) for v in ("", 5, None)),
        *(
            ("expires_at", v)
            for v in ("2030-01-01T10:00:00", "ayer", 5, None, "2030-01-01")
            + ("2030-02-30T10:00:00Z", "2030-01-01T10:00:00+25:00")
        ),
        *(("reason", v) for v in (" ", "", "x" * 256, "a\x00b", None, 7)),
        *(("idempotency_key", v) for v in ("corta", "import:abcdefgh", "a b c d e f", 12345678)),
        ("organization_id", "00000000-0000-0000-0000-000000000000"),
        ("sku", "X"),
    ],
)
def test_a_bad_body_is_a_400_on_its_field_and_writes_nothing(
    ana: Client, world: Any, migrator: psycopg.Connection[Any], field: str, value: Any
) -> None:
    here = stocked(world.a, 2, world.ana)
    before = migrator.execute(COUNTS).fetchone()
    reply_ = reserve(ana, here, **{field: value})
    assert reply_.status_code == 400 and list(reply_.json()["fields"]) == [field], reply_.content
    assert migrator.execute(COUNTS).fetchone() == before


def test_the_expiry_bounds_and_a_missing_field(
    ana: Client, world: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    here = stocked(world.a, 2, world.ana)
    now = timezone.now()
    monkeypatch.setattr(res, "_now", lambda: now)  # los límites exactos
    naive = (now + timedelta(hours=2)).replace(tzinfo=None).isoformat()  # sin zona: no se adivina
    past, far = now - timedelta(seconds=1), now + res.EXPIRES_MAX + timedelta(seconds=1)
    for when in (naive, now.isoformat(), past.isoformat(), far.isoformat()):
        bad = reserve(ana, here, expires_at=when)
        assert (bad.status_code, list(bad.json()["fields"])) == (400, ["expires_at"]), when
    for field in ("expires_at", "quantity", "reason", "idempotency_key", "variant_id"):
        wanted = body(here)
        del wanted[field]
        assert list(send(ana, "post", URL, wanted).json()["fields"]) == [field]
    assert send(ana, "post", URL, [1]).status_code == 400
    top = (now + res.EXPIRES_MAX).isoformat()
    assert reserve(ana, here, expires_at=top).status_code == 201


def test_unknown_references_look_the_same_whoever_owns_them(ana: Client, world: Any) -> None:
    here, foreign = stocked(world.a, 2, world.ana), cell(world.b, "B")
    service = cell(world.a, "SRV", "SERVICE")
    for field, theirs in (("variant_id", foreign[0]), ("warehouse_id", foreign[1])):
        one, two = (reserve(ana, here, **{field: str(x)}) for x in (theirs, uuid4()))
        assert (one.status_code, one.content) == (two.status_code, two.content)
        assert list(one.json()["fields"]) == [field]
    assert list(reserve(ana, (service[0], here[1])).json()["fields"]) == ["variant_id"]
    with tenant_scope(ctx(world.a)):  # borrados: lo mismo que si no existieran
        Warehouse.objects.filter(pk=here[1]).update(deleted_at=timezone.now())
    assert list(reserve(ana, here).json()["fields"]) == ["warehouse_id"]
    with tenant_scope(ctx(world.a)):
        ProductVariant.objects.filter(pk=service[0]).update(deleted_at=timezone.now())
    assert list(reserve(ana, (service[0], service[1])).json()["fields"]) == ["variant_id"]


def test_the_conflicts_carry_their_code_and_no_figures(
    ana: Client, world: Any, migrator: psycopg.Connection[Any]
) -> None:
    here = stocked(world.a, 2, world.ana)
    big = reserve(ana, here, quantity=3)
    assert (big.status_code, big.json()["code"]) == (409, "INSUFFICIENT_STOCK")
    assert not any(ch.isdigit() for ch in big.json()["message"])
    with tenant_scope(ctx(world.a)):
        ProductVariant.objects.filter(pk=here[0]).update(status="INACTIVE")
    assert reserve(ana, here).json()["code"] == "VARIANT_INACTIVE"
    with tenant_scope(ctx(world.a)):
        ProductVariant.objects.filter(pk=here[0]).update(status="ACTIVE")
        Warehouse.objects.filter(pk=here[1]).update(is_active=False)
    assert reserve(ana, here).json()["code"] == "WAREHOUSE_INACTIVE"
    assert consistent(world.a, here) == (2, 0, 2)
    assert migrator.execute("SELECT count(*) FROM inventory_reservations").fetchone() == (0,)


def test_a_refusal_undoes_the_expiry_done_inside_reserve(
    ana: Client, world: Any, migrator: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    here = stocked(world.a, 2, world.ana)
    old = reserve(ana, here, quantity=2).json()
    later = timezone.now() + timedelta(hours=3)
    monkeypatch.setattr(res, "_now", lambda: later)
    before = migrator.execute(COUNTS).fetchone()
    refused = reserve(ana, here, quantity=3, expires_at=(later + timedelta(hours=1)).isoformat())
    assert (refused.status_code, refused.json()["code"]) == (409, "INSUFFICIENT_STOCK")
    assert migrator.execute(COUNTS).fetchone() == before  # la vencida sigue ACTIVE
    ok = reserve(ana, here, quantity=2, expires_at=(later + timedelta(hours=1)).isoformat())
    assert ok.status_code == 201  # la que sí cabe la vence primero, como el sistema
    assert states(migrator)[old["id"]] == ("EXPIRED", None)  # el sistema, sin persona
    assert consistent(world.a, here) == (2, 2, 0)


def test_release_of_a_closed_reservation_and_of_another_organizations(
    ana: Client, world: Any, migrator: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    here, mine = stocked(world.a, 5, world.ana), stocked(world.b, 5, None, "B")
    consumed, expiring, due = (reserve(ana, here).json() for _ in range(3))
    migrator.execute(
        "UPDATE inventory_reservations SET status = 'CONSUMED', ended_at = now(),"
        " ended_by_type = 'SYSTEM' WHERE id = %s",
        [consumed["id"]],
    )
    later = timezone.now() + timedelta(hours=3)
    monkeypatch.setattr(res, "_now", lambda: later)
    with tenant_scope(ctx(world.a)) as tenant:
        assert res.expire(tenant, expiring["id"]) is True
    for pk in (consumed["id"], expiring["id"], due["id"]):  # la vencida que el job aún no cerró
        closed = release(ana, pk)
        assert (closed.status_code, closed.json()["code"]) == (409, "RESERVATION_NOT_ACTIVE")
        assert not any(ch.isdigit() for ch in closed.json()["message"])
    assert states(migrator)[due["id"]][0] == "ACTIVE"  # ni se escribió
    foreign = migrator.execute(  # una reserva de la otra organización, como la dejaría su API
        "INSERT INTO inventory_reservations (id, organization_id, variant_id, warehouse_id,"
        " quantity, status, expires_at, reason, idempotency_key, created_by_type, created_at)"
        " VALUES (uuidv7(), %s, %s, %s, 1, 'ACTIVE', now() + interval '1 hour', 'x', 'otra-org-1',"
        " 'SYSTEM', now()) RETURNING id",
        [world.b, *mine],
    ).fetchone()
    assert foreign is not None
    gone, theirs = release(ana, uuid4()), release(ana, foreign[0])
    assert (gone.status_code, gone.content) == (theirs.status_code, theirs.content)
    assert gone.status_code == 404
    assert migrator.execute(
        "SELECT status FROM inventory_reservations WHERE id = %s", [foreign[0]]
    ).fetchone() == ("ACTIVE",)


def at_once(*calls: Any) -> list[tuple[int, Any, Any]]:
    start = threading.Barrier(len(calls))

    def run(call: Any) -> tuple[int, Any, Any]:
        try:
            start.wait(timeout=10)
            made = call()
            return made.status_code, made.json().get("id"), made.json().get("code")
        finally:
            connection.close()

    with ThreadPoolExecutor(len(calls)) as pool:
        return list(pool.map(run, calls))


def test_the_last_unit_asked_twice_over_http_goes_once(
    ana: Client, bruno: Client, world: Any, migrator: psycopg.Connection[Any]
) -> None:
    here = stocked(world.a, 1, world.ana)
    outcome = at_once(lambda: reserve(ana, here), lambda: reserve(bruno, here))
    assert sorted((code, why) for code, _, why in outcome) == [
        (201, None),
        (409, "INSUFFICIENT_STOCK"),
    ]
    assert consistent(world.a, here) == (1, 1, 0) and ledger(world.a, "RESERVE") == [1]
    assert migrator.execute("SELECT count(*) FROM inventory_reservations").fetchone() == (1,)


def test_the_same_key_and_two_releases_at_once_over_http(
    ana: Client, world: Any, migrator: psycopg.Connection[Any]
) -> None:
    here = stocked(world.a, 3, world.ana)
    same = body(here, quantity=2)
    first, second = at_once(*[lambda: send(ana, "post", URL, same)] * 2)
    assert sorted((first[0], second[0])) == [200, 201] and first[1] == second[1]
    assert migrator.execute("SELECT count(*) FROM inventory_reservations").fetchone() == (1,)
    both = at_once(*[lambda: release(ana, first[1])] * 2)
    assert [code for code, _, _ in both] == [200, 200] and ledger(world.a, "RELEASE") == [-2]
    assert consistent(world.a, here) == (3, 0, 3)


def test_the_same_key_from_two_cells_and_the_cap_at_once_over_http(
    ana: Client, world: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    here, there = stocked(world.a, 3, world.ana), stocked(world.a, 3, world.ana, "OTRA")
    same = {"idempotency_key": "clave-0001"}  # la misma clave, a la vez, en otra celda
    outcome = at_once(lambda: reserve(ana, here, **same), lambda: reserve(ana, there, **same))
    assert sorted((code, why) for code, _, why in outcome) == [(201, None), (409, KEY_REUSED)]
    monkeypatch.setattr(res, "MAX_ACTIVE_PER_CELL", 2)
    wide = stocked(world.a, 5, world.ana, "TRES")
    outcome = at_once(*[lambda: reserve(ana, wide)] * 3)
    assert sorted((code, why) for code, _, why in outcome) == [
        (201, None),
        (201, None),
        (409, "TOO_MANY_RESERVATIONS"),
    ]
    assert consistent(world.a, wide) == (5, 2, 3)
