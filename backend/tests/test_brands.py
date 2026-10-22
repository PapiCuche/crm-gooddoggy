"""F3-01: las marcas del catálogo (`…/brands/`) y los permisos del catálogo. Middleware, sesión,
motor de autorización y PostgreSQL con el rol `crm_app`."""

from typing import Any

import psycopg
import pytest
from django.test import Client

from apps.access.catalog import BY_CODE, ROLE_TEMPLATES
from apps.catalog.brands import slug_from
from apps.catalog.models import Brand
from core.tenancy.scope import tenant_scope
from tests import test_authorization, test_self_context
from tests.test_authorization import give
from tests.test_memberships import ctx
from tests.test_self_context import signed

world, real_stack = test_authorization.world, test_self_context.real_stack
pytestmark = pytest.mark.usefixtures("tenant_db")
DENIED = {"code": "PERMISSION_DENIED"}
URL = "/api/v1/o/{}/brands/"
VIEW: dict[str, str | None] = {"products.view": None}
MANAGE: dict[str, str | None] = {"products.view": None, "brands.manage": None}
CATALOG = ["products.view", "products.manage", "products.import", "categories.manage"]


def send(client: Client, method: str, path: str, body: Any = None) -> Any:
    client.cookies["csrftoken"] = token = "t" * 32
    return getattr(client, method)(path, body, "application/json", headers={"X-CSRFToken": token})


def one(org: str = "org-a") -> str:
    return URL.format(org)


@pytest.fixture
def ana(world: Any) -> Client:
    give(world.a, world.membership, MANAGE)
    return signed(world.ana)


def audit_rows(migrator: psycopg.Connection[Any]) -> list[tuple[Any, ...]]:
    return migrator.execute(
        "SELECT action, entity_type, changes::text FROM audit_logs "
        "WHERE action LIKE 'brand.%' ORDER BY occurred_at, id"
    ).fetchall()


def test_the_catalog_permissions_and_who_gets_them() -> None:
    for code in [*CATALOG, "brands.manage"]:
        permission = BY_CODE[code]
        assert (permission.module, permission.is_sensitive, permission.supports_scope) == (
            "products",
            False,
            False,
        )
    holders = {
        code: {template.code for template in ROLE_TEMPLATES if code in template.grants}
        for code in [*CATALOG, "brands.manage"]
    }
    assert holders["products.view"] == {"owner", "admin", "supervisor", "seller"}
    for code in ["products.manage", "products.import", "categories.manage", "brands.manage"]:
        assert holders[code] == {"owner", "admin"}, code


def test_create_rename_delete_with_audit(
    world: Any, ana: Client, migrator: psycopg.Connection[Any]
) -> None:
    body = {
        "name": "  Apple   Perú ",
        "organization_id": str(world.b),
    }  # la organización sale del contexto
    made = send(ana, "post", one(), body)
    assert made.status_code == 201
    brand = made.json()
    assert (brand["name"], brand["slug"]) == ("Apple Perú", "apple-peru")
    with tenant_scope(ctx(world.a)):
        assert Brand.objects.get().organization_id == world.a
    listed = ana.get(one()).json()["results"]
    assert [item["slug"] for item in listed] == ["apple-peru"]

    renamed = send(ana, "patch", f"{one()}{brand['id']}/", {"name": "Apple"})
    assert renamed.status_code == 200 and renamed.json()["slug"] == "apple-peru"  # no cambia
    same = send(ana, "patch", f"{one()}{brand['id']}/", {"name": "Apple"})
    assert same.status_code == 200  # el mismo nombre no escribe ni audita

    assert send(ana, "delete", f"{one()}{brand['id']}/").status_code == 204
    assert ana.get(one()).json()["results"] == []
    assert send(ana, "delete", f"{one()}{brand['id']}/").status_code == 404  # ya borrada
    assert send(ana, "patch", f"{one()}{brand['id']}/", {"name": "X"}).status_code == 404
    again = send(ana, "post", one(), {"name": "Apple", "slug": "apple-peru"})
    assert again.status_code == 201  # el borrado libera su slug

    actions = [row[0] for row in audit_rows(migrator)]
    assert actions == ["brand.created", "brand.updated", "brand.deleted", "brand.created"]


def test_slug_rules_and_conflicts(ana: Client) -> None:
    assert send(ana, "post", one(), {"name": "Samsung"}).status_code == 201
    taken = send(ana, "post", one(), {"name": "Otra", "slug": "samsung"})
    assert taken.status_code == 409 and taken.json()["code"] == "BRAND_SLUG_TAKEN"
    bad = send(ana, "post", one(), {"name": "Xiaomi", "slug": "Xiaomi Inc"})
    assert bad.status_code == 400 and "slug" in bad.json()["fields"]
    unformable = send(ana, "post", one(), {"name": "華為"})  # sin letras ASCII: hay que darlo
    assert unformable.status_code == 400 and "slug" in unformable.json()["fields"]
    empty = send(ana, "post", one(), {"name": " - "})
    assert empty.status_code == 400 and "name" in empty.json()["fields"]
    assert slug_from("Ñandú & Cía. ") == "nandu-cia"


def test_permissions_and_isolation(world: Any, migrator: psycopg.Connection[Any]) -> None:
    viewer = signed(world.ana)
    assert viewer.get(one()).status_code == 403  # sin `products.view`
    give(world.a, world.membership, VIEW)
    assert viewer.get(one()).status_code == 200
    denied = send(viewer, "post", one(), {"name": "Apple"})
    assert (denied.status_code, denied.json()) == (403, DENIED)
    assert viewer.get(one("org-b")).status_code == 404  # otra organización no existe para ella

    # Una marca de otra organización no se ve ni se toca.
    with tenant_scope(ctx(world.b)):
        foreign = Brand.objects.create(name="Ajena", slug="ajena")
    give(world.a, world.membership, MANAGE)
    assert viewer.get(one()).json()["results"] == []
    assert send(viewer, "patch", f"{one()}{foreign.pk}/", {"name": "Mía"}).status_code == 404
    assert send(viewer, "delete", f"{one()}{foreign.pk}/").status_code == 404
    row = migrator.execute("SELECT name, deleted_at FROM brands WHERE id = %s", [foreign.pk])
    assert row.fetchone() == ("Ajena", None)
