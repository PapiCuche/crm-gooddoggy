"""F3-08: almacenes por sucursal (`…/warehouses/`). Middleware, sesión, motor de autorización y
PostgreSQL con el rol `crm_app`."""

from typing import Any
from uuid import uuid4

import psycopg
import pytest
from django.db import IntegrityError
from django.test import Client
from django.utils import timezone

from apps.access.catalog import ROLE_TEMPLATES
from apps.inventory.models import Warehouse
from apps.organizations.models import Branch
from core.tenancy.scope import tenant_scope
from tests import test_authorization, test_self_context
from tests.test_authorization import give
from tests.test_memberships import ctx
from tests.test_products import send
from tests.test_self_context import signed

world, real_stack = test_authorization.world, test_self_context.real_stack
pytestmark = pytest.mark.usefixtures("tenant_db")
WAREHOUSES = "/api/v1/o/org-a/warehouses/"
INVENTORY = ("inventory.view", "inventory.manage", "inventory.adjust", "inventory.transfer")
VIEW: dict[str, str | None] = {"inventory.view": None}
MANAGE: dict[str, str | None] = {**VIEW, "warehouses.manage": None}


def branch(org: Any, code: str = "LIM") -> Branch:
    with tenant_scope(ctx(org)):
        made: Branch = Branch.objects.create(code=code, name=f"Sucursal {code}")
    return made


def create(client: Client, branch_id: Any, **body: Any) -> Any:
    wanted = {"branch_id": str(branch_id), "code": "alm-1", "name": "Almacén central", **body}
    return send(client, "post", WAREHOUSES, wanted)


@pytest.fixture
def ana(world: Any) -> Client:
    give(world.a, world.membership, MANAGE)
    return signed(world.ana)


def audit(migrator: psycopg.Connection[Any]) -> list[tuple[str, Any]]:
    query = "SELECT action, changes FROM audit_logs WHERE entity_type = 'warehouse' ORDER BY id"
    return list(migrator.execute(query).fetchall())


def test_the_templates_carry_the_inventory_permissions_of_03_h() -> None:
    grants = {template.code: set(template.grants) for template in ROLE_TEMPLATES}
    assert grants["owner"] >= {*INVENTORY, "warehouses.manage"}
    assert grants["admin"] >= {*INVENTORY, "warehouses.manage"}
    for code in ("supervisor", "seller"):  # «view» en la matriz
        assert grants[code] & {*INVENTORY, "warehouses.manage"} == {"inventory.view"}


def test_create_and_change_a_warehouse_audited(
    ana: Client, world: Any, migrator: psycopg.Connection[Any]
) -> None:
    lima = branch(world.a)
    made = create(ana, lima.pk, type="STORE", is_sellable=False)
    assert made.status_code == 201, made.content
    body = made.json()  # el código, en mayúsculas
    assert [body[k] for k in ("code", "type", "is_sellable", "is_active")] == [
        "ALM-1",
        "STORE",
        False,
        True,
    ]
    item = f"{WAREHOUSES}{body['id']}/"
    changed = send(ana, "patch", item, {"name": " Tienda  Lima ", "is_active": False})
    assert changed.status_code == 200 and changed.json()["name"] == "Tienda Lima"
    assert send(ana, "patch", item, {"is_active": False}).status_code == 200  # sin cambios
    fixed = send(ana, "patch", item, {"code": "OTRO", "branch_id": str(uuid4()), "name": "x"})
    assert fixed.status_code == 400 and set(fixed.json()["fields"]) == {"code", "branch_id"}
    assert ana.get(WAREHOUSES).json()["results"][0]["code"] == "ALM-1"  # nada cambió
    assert [row["id"] for row in ana.get(WAREHOUSES).json()["results"]] == [body["id"]]
    assert send(ana, "patch", f"{WAREHOUSES}{uuid4()}/", {"name": "x"}).status_code == 404
    assert [action for action, _ in audit(migrator)] == ["warehouse.created", "warehouse.updated"]
    assert audit(migrator)[0][1]["code"] == [None, "ALM-1"]
    changes = {"name": ["Almacén central", "Tienda Lima"], "is_active": [True, False]}
    assert audit(migrator)[1][1] == changes


def test_the_code_is_unique_among_live_ones_and_the_branch_is_of_the_organization(
    ana: Client, world: Any
) -> None:
    lima, foreign = branch(world.a), branch(world.b, "AJENA")
    first = create(ana, lima.pk).json()
    taken = create(ana, lima.pk, code="ALM-1")
    assert taken.status_code == 409 and taken.json()["code"] == "WAREHOUSE_CODE_TAKEN"
    for bad in ({"code": "ſ1"}, {"name": "  "}, {"type": "SHOP"}, {"is_sellable": "x"}):
        refused = create(ana, lima.pk, **bad)
        assert refused.status_code == 400 and set(refused.json()["fields"]) == set(bad)
    for other in (foreign.pk, uuid4()):
        refused = create(ana, other, code="ALM-2")
        assert refused.status_code == 400 and "branch_id" in refused.json()["fields"]
    with tenant_scope(ctx(world.a)):  # un borrado lógico libera el código
        Warehouse.objects.filter(pk=first["id"]).update(deleted_at=timezone.now())
    assert send(ana, "patch", f"{WAREHOUSES}{first['id']}/", {"name": "x"}).status_code == 404
    assert ana.get(WAREHOUSES).json()["results"] == []  # ni se lista
    assert create(ana, lima.pk).status_code == 201


def test_the_database_keeps_the_branch_in_the_organization(world: Any) -> None:
    foreign = branch(world.b, "AJENA")
    with pytest.raises(IntegrityError), tenant_scope(ctx(world.a)):
        Warehouse.objects.create(branch_id=foreign.pk, code="X", name="X")


def test_permissions_and_isolation(world: Any) -> None:
    viewer, lima = signed(world.ana), branch(world.a)
    assert viewer.get(WAREHOUSES).status_code == 403
    give(world.a, world.membership, VIEW)
    with tenant_scope(ctx(world.a)):
        mine = Warehouse.objects.create(branch=lima, code="MIO", name="Mío")
    with tenant_scope(ctx(world.b)):
        Warehouse.objects.create(branch=branch(world.b, "B"), code="AJENO", name="Ajeno")
    assert [row["code"] for row in viewer.get(WAREHOUSES).json()["results"]] == ["MIO"]
    assert create(viewer, lima.pk).status_code == 403
    assert send(viewer, "patch", f"{WAREHOUSES}{mine.pk}/", {"name": "x"}).status_code == 403
