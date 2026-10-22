"""F3-04: las categorías del catálogo en árbol (`…/categories/`). Middleware, sesión, motor de
autorización y PostgreSQL con el rol `crm_app`."""

from typing import Any

import psycopg
import pytest
from django.test import Client

from apps.catalog.models import Category
from core.tenancy.scope import tenant_scope
from tests import test_authorization, test_self_context
from tests.test_authorization import give
from tests.test_brands import VIEW, send
from tests.test_memberships import ctx
from tests.test_self_context import signed

world, real_stack = test_authorization.world, test_self_context.real_stack
pytestmark = pytest.mark.usefixtures("tenant_db")
URL = "/api/v1/o/{}/categories/"
MANAGE: dict[str, str | None] = {"products.view": None, "categories.manage": None}


def one(org: str = "org-a") -> str:
    return URL.format(org)


def made(client: Client, name: str, parent: str | None = None, **extra: Any) -> dict[str, Any]:
    body = {"name": name, **({"parent_id": parent} if parent else {}), **extra}
    response = send(client, "post", one(), body)
    assert response.status_code == 201, response.content
    created: dict[str, Any] = response.json()
    return created


def tree(client: Client) -> dict[str, tuple[str, int]]:
    return {row["path"]: (row["name"], row["depth"]) for row in client.get(one()).json()["results"]}


@pytest.fixture
def ana(world: Any) -> Client:
    give(world.a, world.membership, MANAGE)
    return signed(world.ana)


def test_a_tree_with_paths_and_depths(ana: Client, migrator: psycopg.Connection[Any]) -> None:
    phones = made(ana, "Celulares")
    iphone = made(ana, "iPhone", phones["id"])
    made(ana, "Accesorios", iphone["id"])
    assert (phones["path"], phones["depth"], phones["parent_id"]) == ("/celulares/", 1, None)
    assert (iphone["path"], iphone["depth"]) == ("/celulares/iphone/", 2)
    assert tree(ana) == {
        "/celulares/": ("Celulares", 1),
        "/celulares/iphone/": ("iPhone", 2),
        "/celulares/iphone/accesorios/": ("Accesorios", 3),
    }
    # El mismo slug vale en otra rama, no entre hermanas.
    made(ana, "Accesorios")
    taken = send(
        ana, "post", one(), {"name": "Otra", "slug": "accesorios", "parent_id": iphone["id"]}
    )
    assert taken.status_code == 409 and taken.json()["code"] == "CATEGORY_SLUG_TAKEN"
    audited = migrator.execute(
        "SELECT changes::text FROM audit_logs WHERE action = 'category.created' ORDER BY id"
    ).fetchall()
    assert len(audited) == 4 and '"path": [null, "/celulares/iphone/"]' in audited[1][0]


def test_at_most_five_levels(ana: Client) -> None:
    parent = None
    for level in range(5):
        parent = made(ana, f"Nivel {level + 1}", parent["id"] if parent else None)
    deep = send(ana, "post", one(), {"name": "Seis", "parent_id": parent["id"]})  # type: ignore[index]
    assert deep.status_code == 400 and "parent_id" in deep.json()["fields"]


def test_permissions_and_isolation(world: Any) -> None:
    viewer = signed(world.ana)
    assert viewer.get(one()).status_code == 403
    give(world.a, world.membership, VIEW)
    assert viewer.get(one()).status_code == 200
    assert send(viewer, "post", one(), {"name": "Celulares"}).status_code == 403
    with tenant_scope(ctx(world.a)):
        Category.objects.create(name="Mía", slug="mia", path="/mia/", depth=1)
    with tenant_scope(ctx(world.b)):
        foreign = Category.objects.create(name="Ajena", slug="ajena", path="/ajena/", depth=1)
    give(world.a, world.membership, MANAGE)
    assert [row["slug"] for row in viewer.get(one()).json()["results"]] == ["mia"]
    child = send(viewer, "post", one(), {"name": "Hija", "parent_id": str(foreign.pk)})
    assert child.status_code == 400 and "parent_id" in child.json()["fields"]  # no existe aquí


def test_the_table_keeps_each_branch_in_one_organization(
    world: Any, migrator: psycopg.Connection[Any]
) -> None:
    with tenant_scope(ctx(world.b)):
        foreign = Category.objects.create(name="Ajena", slug="ajena", path="/ajena/", depth=1)
    insert = (
        "INSERT INTO categories (organization_id, parent_id, name, slug, path, depth, sort_order,"
        " created_at, updated_at) VALUES (%s, %s, 'Hija', 'hija', %s, %s, 0, now(), now())"
    )
    with pytest.raises(psycopg.errors.ForeignKeyViolation):  # padre de otra organización
        migrator.execute(insert, [world.a, foreign.pk, "/ajena/hija/", 2])
    with pytest.raises(psycopg.errors.CheckViolation):  # raíz con nivel 2
        migrator.execute(insert, [world.b, None, "/hija/", 2])
    with pytest.raises(psycopg.errors.CheckViolation):  # más de 5 niveles
        migrator.execute(insert, [world.b, foreign.pk, "/ajena/hija/", 6])
    migrator.execute(insert, [world.b, foreign.pk, "/ajena/hija/", 2])
    keys = migrator.execute(
        "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint"
        " WHERE conrelid = 'categories'::regclass AND contype IN ('f', 'u')"
    ).fetchall()
    assert sorted(keys) == [
        ("categories_org_id_uq", "UNIQUE (organization_id, id)"),
        (
            "categories_organization_fk",
            "FOREIGN KEY (organization_id) REFERENCES organizations(id)",
        ),
        (
            "categories_parent_fk",
            "FOREIGN KEY (organization_id, parent_id) REFERENCES categories(organization_id, id)",
        ),
    ]
