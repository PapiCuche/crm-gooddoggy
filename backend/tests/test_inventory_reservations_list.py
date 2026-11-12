"""F3-35b: el listado de reservas (`GET …/inventory/reservations/`) con el middleware, la sesión, el
motor de autorización y PostgreSQL con el rol `crm_app`: solo `inventory.view` lista, nunca se ve
una reserva ajena, los filtros y el cursor que no valen son 400 y las páginas no repiten filas."""

import base64
import threading
from typing import Any
from urllib.parse import urlencode
from uuid import uuid4

import psycopg
import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.catalog.models import ProductVariant
from apps.inventory.models import Warehouse
from core.tenancy.scope import tenant_scope
from tests.test_authorization import give
from tests.test_inventory_reservations import stocked
from tests.test_inventory_reservations_api import URL, VIEW, ana, bruno, real_stack, release, world
from tests.test_inventory_reservations_api import reserve as post
from tests.test_inventory_stock import move, wanted
from tests.test_memberships import ctx
from tests.test_self_context import reply, signed

__all__ = ["ana", "bruno", "real_stack", "world"]  # fixtures
pytestmark = pytest.mark.usefixtures("tenant_db")
FIELDS = {  # las del POST: sin organización, clave de idempotencia, tipo de actor, costo ni precio
    *("id", "variant_id", "variant", "warehouse_id", "quantity", "status", "expires_at"),
    *("reason", "created_by_user_id", "created_at", "ended_at", "ended_by_user_id", "end_reason"),
}


def ids(client: Client, **query: Any) -> list[str]:
    listed = client.get(URL, query)
    assert listed.status_code == 200, listed.content
    return [row["id"] for row in listed.json()["results"]]


def foreign(migrator: psycopg.Connection[Any], org: Any, where: Any) -> str:
    """Una reserva de la otra organización, como la dejaría su API."""
    row = migrator.execute(
        "INSERT INTO inventory_reservations (id, organization_id, variant_id, warehouse_id,"
        " quantity, status, expires_at, reason, idempotency_key, created_by_type, created_at)"
        " VALUES (uuidv7(), %s, %s, %s, 1, 'ACTIVE', now() + interval '1 hour', 'x', %s,"
        " 'SYSTEM', now()) RETURNING id",
        [org, *where, uuid4().hex],
    ).fetchone()
    assert row is not None
    return str(row[0])


def test_only_inventory_view_lists(world: Any, bruno: Client) -> None:
    post(bruno, stocked(world.a, 2, world.ana))
    assert reply(Client(enforce_csrf_checks=True).get(URL))[0] == 401
    member = signed(world.ana)
    assert member.get(URL).status_code == 403
    others: dict[str, str | None] = {
        "inventory.manage": None,
        "inventory.adjust": None,
        "inventory.transfer": None,
    }
    give(world.a, world.membership, others)
    refused = member.get(URL)  # reservar o ajustar no es ver
    assert refused.status_code == 403 and "results" not in refused.json()
    give(world.a, world.membership, VIEW)
    assert len(ids(member)) == 1


def test_the_rows_are_the_posts_answers_newest_first_and_filtered(ana: Client, world: Any) -> None:
    here, other = stocked(world.a, 5, world.ana), stocked(world.a, 5, world.ana, "OTRA")
    with tenant_scope(ctx(world.a)):  # otro almacén para la misma variante
        branch = Warehouse.objects.get(pk=here[1]).branch_id
        alt = Warehouse.objects.create(branch_id=branch, code="ALT", name="Otro").pk
    move(world.a, world.ana, wanted((here[0], alt), "RECEIPT", 3))
    first = post(ana, here).json()
    second = post(ana, other, quantity=2).json()
    third = post(ana, (here[0], alt)).json()
    ended = release(ana, first["id"]).json()
    listed = ana.get(URL).json()
    assert listed == {"results": [third, second, ended], "next": None}  # lo mismo que el POST
    assert all(set(row) == FIELDS for row in listed["results"])
    assert ids(ana, status="ACTIVE") == [third["id"], second["id"]]
    assert ids(ana, status="RELEASED") == [first["id"]]
    assert ids(ana, status="EXPIRED") == ids(ana, status="CONSUMED") == []
    assert ids(ana, variant_id=str(here[0])) == [third["id"], first["id"]]
    assert ids(ana, warehouse_id=str(alt)) == [third["id"]]
    assert ids(ana, warehouse_id=str(here[1])) == [second["id"], first["id"]]
    cell_ = {"variant_id": str(here[0]), "warehouse_id": str(here[1])}
    assert ids(ana, **cell_, status="RELEASED") == [first["id"]]
    assert ids(ana, **cell_, status="ACTIVE") == []
    assert ids(ana, variant_id=str(here[0]).upper()) == [third["id"], first["id"]]
    ignored = {"ordering": "expires_at", "sort": "status", "page": "2", "status__in": "X"}
    assert ids(ana, **ignored) == [third["id"], second["id"], first["id"]]  # como sus hermanas


def test_another_organizations_reservations_never_show(
    ana: Client, world: Any, migrator: psycopg.Connection[Any]
) -> None:
    mine = post(ana, stocked(world.a, 2, world.ana)).json()["id"]
    theirs = stocked(world.b, 2, None, "B")
    hidden = foreign(migrator, world.b, theirs)
    assert ids(ana) == [mine]
    assert ids(ana, organization_id=str(world.b)) == [mine]  # la organización no la elige nadie
    empty = ana.get(URL, {"variant_id": str(uuid4())}).content
    for query in ({"variant_id": theirs[0]}, {"warehouse_id": theirs[1]}):
        assert ana.get(URL, {key: str(value) for key, value in query.items()}).content == empty
    top = base64.b64encode(urlencode({"p": "ffffffff-ffff-ffff-ffff-ffffffffffff"}).encode())
    assert ids(ana, cursor=top.decode()) == [mine]  # un cursor fabricado no cruza la RLS
    assert hidden not in ana.get(URL, {"limit": 200}).content.decode()
    assert signed(world.ana).get("/api/v1/o/org-b/inventory/reservations/").status_code == 404


@pytest.mark.parametrize(
    ("field", "value"),
    [
        *(("status", v) for v in ("active", "", "ACTIVE ", "BORRADA", "ACTIVE,RELEASED")),
        *(("variant_id", v) for v in ("no-es-uuid", "", "1", "' OR 1=1 --")),
        *(("warehouse_id", v) for v in ("", "x" * 100, "a\x00b")),
        *(("cursor", v) for v in ("basura", "", "cD0x", "%%")),
        *(("limit", v) for v in ("0", "201", "-1", "1.5", "")),
    ],
)
def test_a_bad_filter_or_cursor_is_a_400_on_its_field(
    ana: Client, world: Any, field: str, value: str
) -> None:
    post(ana, stocked(world.a, 2, world.ana))
    refused = ana.get(URL, {field: value})
    assert refused.status_code == 400 and list(refused.json()["fields"]) == [field], refused.content
    assert "results" not in refused.json()


def test_the_queries_do_not_grow_with_the_rows(ana: Client, world: Any) -> None:
    here = stocked(world.a, 2, world.ana)
    post(ana, here)

    def queries() -> tuple[int, list[Any]]:
        with CaptureQueriesContext(connection) as captured:
            listed = ana.get(URL, {"limit": 200})
        assert listed.status_code == 200, listed.content
        return len(captured), listed.json()["results"]

    few, rows = queries()
    assert len(rows) == 1
    for index in range(5):
        post(ana, stocked(world.a, 1, world.ana, f"SKU{index}"))
    count, rows = queries()
    assert (count, len(rows)) == (few, 6)  # seis filas, las mismas consultas que una
    assert {row["variant"]["sku"] for row in rows} == {"IP16", *(f"SKU{i}" for i in range(5))}
    with tenant_scope(ctx(world.a)):  # borrada en el catálogo: la reserva sigue, marcada
        ProductVariant.objects.filter(pk=here[0]).update(deleted_at=timezone.now())
    listed = ana.get(URL, {"variant_id": str(here[0])}).json()["results"]
    assert [row["variant"]["is_deleted"] for row in listed] == [True]


def test_pages_neither_repeat_nor_skip_while_others_reserve(
    ana: Client, bruno: Client, world: Any
) -> None:
    here = stocked(world.a, 20, world.ana)
    made = [post(ana, here).json()["id"] for _ in range(5)]
    page = ana.get(URL, {"limit": 2}).json()
    seen, codes = [row["id"] for row in page["results"]], list[int]()

    def writer() -> None:  # otra persona reserva a la vez, por HTTP y con su conexión
        try:
            codes.extend(post(bruno, here).status_code for _ in range(5))
        finally:
            connection.close()

    thread = threading.Thread(target=writer)
    thread.start()
    while page["next"] is not None:
        page = ana.get(URL, {"limit": 1, "cursor": page["next"]}).json()
        seen += [row["id"] for row in page["results"]]
    thread.join(timeout=30)
    assert (
        codes == [201] * 5 and seen == made[::-1]
    )  # las nuevas van delante: ni se cuelan ni repiten
    assert len(ids(ana, limit=200)) == 10
