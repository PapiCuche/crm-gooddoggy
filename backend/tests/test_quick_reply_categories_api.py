"""F6-08a4: gestionar las categorías de respuestas rápidas por HTTP (`…/quick-reply-categories/`).
Middleware, sesión, motor de autorización y PostgreSQL con el rol `crm_app`; los comandos se prueban
en `test_quick_replies`."""

import json
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from apps.conversations import quick_replies as qr
from apps.conversations.models import QuickReplyCategory
from apps.conversations.seed import CATEGORIES, ensure_quick_reply_categories
from core.tenancy.scope import tenant_scope
from tests import test_contacts_api, test_self_context
from tests.test_authorization import give
from tests.test_member_branch import DENIED
from tests.test_memberships import ctx
from tests.test_pipelines_write_api import CSRF_FAILED, administrator
from tests.test_products import send
from tests.test_self_context import NOT_FOUND, reply, signed

crm, real_stack = test_contacts_api.crm, test_self_context.real_stack
pytestmark = pytest.mark.usefixtures("tenant_db")
DB = psycopg.Connection[Any]
LIST = "/api/v1/o/org-a/quick-reply-categories/"
FIELDS = {"id", "name", "sort_order", "is_active", "version", "created_at", "updated_at"}


@pytest.fixture
def world(crm: SimpleNamespace) -> SimpleNamespace:
    for org in (crm.a, crm.b):
        with tenant_scope(ctx(org)) as tenant:
            ensure_quick_reply_categories(tenant)
    return crm


def at(category_id: Any, org: str = "org-a") -> str:
    return f"/api/v1/o/{org}/quick-reply-categories/{category_id}/"


def pk(world: SimpleNamespace, name: str, org: str = "a") -> UUID:
    with tenant_scope(ctx(getattr(world, org))):
        found: UUID = QuickReplyCategory.objects.get(name=name, deleted_at__isnull=True).pk
    return found


def change(client: Any, category_id: Any, version: Any = 1, **fields: Any) -> Any:
    return send(client, "patch", at(category_id), {"version": version, **fields})


def audit(migrator: DB) -> list[tuple[str, Any, dict[str, Any]]]:
    rows = migrator.execute(
        "SELECT action, actor_id, changes::text FROM audit_logs "
        "WHERE entity_type = 'quick_reply_category' ORDER BY occurred_at, id"
    ).fetchall()
    return [(action, actor, json.loads(changes)) for action, actor, changes in rows]


def test_the_administrator_creates_promociones_and_orders_it(
    world: SimpleNamespace, migrator: DB
) -> None:
    admin = administrator(world)
    client = signed(admin)
    made = send(client, "post", LIST, {"name": "  Promociones "})
    assert made.status_code == 201, made.content
    new = made.json()
    assert set(new) == FIELDS
    assert (new["name"], new["sort_order"], new["is_active"], new["version"]) == (
        "Promociones", len(CATEGORIES), True, 1,
    )  # fmt: skip
    ordered = change(client, new["id"], sort_order=-1)
    assert (ordered.status_code, ordered.json()["sort_order"], ordered.json()["version"]) == (
        200, -1, 2,
    )  # fmt: skip
    renamed = change(client, new["id"], version=2, name="Ofertas")
    assert renamed.json()["name"] == "Ofertas"
    assert change(client, new["id"], version=3, name="Ofertas").json()["version"] == 3  # igual
    listed = client.get(LIST).json()["results"]
    assert len(listed) == len(CATEGORIES) + 1 and listed[-1] == renamed.json()
    first = min(listed, key=lambda row: row["sort_order"])
    assert first["name"] == "Ofertas"  # la pantalla ordena por `sort_order`
    assert [(a, actor) for a, actor, _ in audit(migrator)] == [
        ("quick_reply_category.created", admin.pk),
        ("quick_reply_category.updated", admin.pk),
        ("quick_reply_category.updated", admin.pk),
    ]
    assert audit(migrator)[1][2] == {"sort_order": [len(CATEGORIES), -1]}
    assert audit(migrator)[2][2] == {"name": ["Promociones", "Ofertas"]}


def test_names_are_unique_and_versions_are_checked(world: SimpleNamespace, migrator: DB) -> None:
    ana, sales = signed(world.ana), pk(world, "Ventas")
    taken = send(ana, "post", LIST, {"name": "VENTAS"})
    assert (taken.status_code, taken.json()["code"]) == (409, "CATEGORY_NAME_TAKEN")
    renamed = change(ana, sales, name="delivery")
    assert (renamed.status_code, renamed.json()["code"]) == (409, "CATEGORY_NAME_TAKEN")
    stale = change(ana, sales, version=7, name="Ventas web")
    assert (stale.status_code, stale.json()["code"]) == (409, "STALE_VERSION")
    assert audit(migrator) == []


def test_a_category_with_active_replies_is_not_deactivated(
    world: SimpleNamespace, migrator: DB
) -> None:
    ana, delivery, sales = signed(world.ana), pk(world, "Delivery"), pk(world, "Ventas")
    with tenant_scope(ctx(world.a, world.ana)) as tenant:
        used = qr.create_reply(
            tenant, shortcut="delivery", title="Envío", category_id=delivery, body="Hola"
        )
    busy = change(ana, delivery, is_active=False)
    assert (busy.status_code, busy.json()["code"]) == (409, "CATEGORY_IN_USE")
    with tenant_scope(ctx(world.a, world.ana)) as tenant:  # se mueve antes (F6-08a3)
        qr.update_reply(tenant, quick_reply_id=used.pk, version=1, category_id=sales)
    off = change(ana, delivery, is_active=False)
    assert (off.status_code, off.json()["is_active"]) == (200, False)
    with tenant_scope(ctx(world.a, world.ana)) as tenant:  # una inactiva no la ocupa
        qr.update_reply(tenant, quick_reply_id=used.pk, version=2, is_active=False)
    assert change(ana, sales, is_active=False).status_code == 200
    with pytest.raises(qr.CategoryInUse), tenant_scope(ctx(world.a, world.ana)) as tenant:
        qr.delete_category(tenant, category_id=sales)  # inactiva, con una inactiva: no se borra
    assert change(ana, delivery, version=2, is_active=True).json()["is_active"] is True
    assert [a for a, *_ in audit(migrator)] == ["quick_reply_category.updated"] * 3


def test_only_quick_replies_manage_reads_and_writes(world: SimpleNamespace, migrator: DB) -> None:
    sales = pk(world, "Ventas")
    for user in (world.vera, world.sam, world.bea):  # Vendedora, Supervisor y sin rol
        client = signed(user)
        for denied in (client.get(LIST), send(client, "post", LIST, {"name": "X"}),
                       change(client, sales, name="X")):  # fmt: skip
            assert reply(denied) == DENIED, user.email
    with CaptureQueriesContext(connection) as captured:
        reply(change(signed(world.vera), sales, name="X"))
    assert not [q for q in captured.captured_queries if "quick_reply_categories" in q["sql"]]
    give(world.a, world.m["bea"], {"quick_replies.manage": None})  # basta, sin nada más
    bea = signed(world.bea)
    assert bea.get(LIST).status_code == 200
    assert send(bea, "post", LIST, {"name": "Bea"}).status_code == 201
    assert [a for a, *_ in audit(migrator)] == ["quick_reply_category.created"]


def test_another_organization_anonymous_csrf_and_what_does_not_exist(
    world: SimpleNamespace,
) -> None:
    mine, theirs, gone = pk(world, "Ventas"), pk(world, "Ventas", "b"), pk(world, "Pagos")
    with tenant_scope(ctx(world.a, world.ana)) as tenant:
        qr.delete_category(tenant, category_id=gone)
    ana = signed(world.ana)
    for missing in (theirs, gone, uuid4()):  # de B, borrada o inexistente: el mismo 404
        assert reply(change(ana, missing, name="Mía")) == NOT_FOUND
    assert reply(send(ana, "patch", LIST + "no-es-uuid/", {"version": 1})) == NOT_FOUND
    assert str(gone) not in {row["id"] for row in ana.get(LIST).json()["results"]}
    give(world.b, world.m_luis, {"quick_replies.manage": None})
    luis = signed(world.luis)
    assert reply(send(luis, "patch", at(mine, "org-b"), {"version": 1, "name": "X"})) == NOT_FOUND
    listed = luis.get("/api/v1/o/org-b/quick-reply-categories/").json()["results"]
    assert str(mine) not in {row["id"] for row in listed} and len(listed) == len(CATEGORIES)
    anonymous = signed(world.ana)
    anonymous.cookies.clear()
    assert reply(anonymous.get(LIST)) == (401, b'{"code":"NOT_AUTHENTICATED"}')
    assert reply(ana.post(LIST, {"name": "X"}, "application/json")) == CSRF_FAILED


BAD = [  # (cuerpo, el campo del 400)
    ({"name": 5}, "name"), ({"name": ""}, "name"), ({"name": "x" * 101}, "name"),
    ({"name": "a\nb"}, "name"), ({"name": "!!!"}, "name"), ({"name": "X", "sort_order": "1"},
    "sort_order"), ({"name": "X", "sort_order": True}, "sort_order"),
    ({"name": "X", "sort_order": 32768}, "sort_order"), ({"name": "X", "is_active": True},
    "is_active"), ({"name": "X", "organization_id": str(uuid4())}, "organization_id"), ({}, "name"),
]  # fmt: skip
BAD_UPDATES = [
    ({"version": 1, "name": None}, "name"), ({"version": 1, "is_active": "false"}, "is_active"),
    ({"version": 1, "sort_order": -32769}, "sort_order"), ({"version": True}, "version"),
    ({"name": "Sin versión"}, "version"), ({"version": 1, "id": str(uuid4())}, "id"),
]  # fmt: skip


@pytest.mark.parametrize(("body", "field"), BAD)
def test_a_bad_creation_is_a_400_on_its_field(
    world: SimpleNamespace, migrator: DB, body: dict[str, Any], field: str
) -> None:
    refused = send(signed(world.ana), "post", LIST, body)
    assert refused.status_code == 400 and field in refused.json()["fields"], refused.content
    assert audit(migrator) == []


@pytest.mark.parametrize(("body", "field"), BAD_UPDATES)
def test_a_bad_change_is_a_400_on_its_field(
    world: SimpleNamespace, migrator: DB, body: dict[str, Any], field: str
) -> None:
    refused = send(signed(world.ana), "patch", at(pk(world, "Ventas")), body)
    assert refused.status_code == 400 and field in refused.json()["fields"], refused.content
    assert audit(migrator) == []
