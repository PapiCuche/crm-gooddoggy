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
from tests.test_memberships import ctx
from tests.test_self_context import signed

world, real_stack = test_authorization.world, test_self_context.real_stack
pytestmark = pytest.mark.usefixtures("tenant_db")
URL = "/api/v1/o/{}/categories/"
VIEW: dict[str, str | None] = {"products.view": None}
MANAGE: dict[str, str | None] = {"products.view": None, "categories.manage": None}


def send(client: Client, method: str, path: str, body: Any = None) -> Any:
    client.cookies["csrftoken"] = token = "t" * 32
    return getattr(client, method)(path, body, "application/json", headers={"X-CSRFToken": token})


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


def test_a_tree_with_paths_and_depths(ana: Client) -> None:
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


def test_at_most_five_levels(ana: Client) -> None:
    parent = None
    for level in range(5):
        parent = made(ana, f"Nivel {level + 1}", parent["id"] if parent else None)
    deep = send(ana, "post", one(), {"name": "Seis", "parent_id": parent["id"]})  # type: ignore[index]
    assert deep.status_code == 400 and "parent_id" in deep.json()["fields"]


def test_move_rewrites_the_whole_branch_and_refuses_cycles(
    ana: Client, migrator: psycopg.Connection[Any]
) -> None:
    phones, audio = made(ana, "Celulares"), made(ana, "Audio")
    iphone = made(ana, "iPhone", phones["id"])
    cases = made(ana, "Fundas", iphone["id"])
    moved = send(ana, "patch", f"{one()}{iphone['id']}/", {"parent_id": audio["id"]})
    assert moved.status_code == 200 and moved.json()["path"] == "/audio/iphone/"
    assert tree(ana)["/audio/iphone/fundas/"] == ("Fundas", 3)
    assert "/celulares/iphone/" not in tree(ana)
    # A sí misma o a una descendiente, no.
    for target in (iphone["id"], cases["id"]):
        cycle = send(ana, "patch", f"{one()}{iphone['id']}/", {"parent_id": target})
        assert cycle.status_code == 400 and "parent_id" in cycle.json()["fields"]
    # A la raíz, y renombrar en la misma petición.
    root = send(ana, "patch", f"{one()}{iphone['id']}/", {"parent_id": None, "name": "Apple"})
    assert (root.json()["path"], root.json()["depth"], root.json()["name"]) == (
        "/iphone/",
        1,
        "Apple",
    )
    assert tree(ana)["/iphone/fundas/"] == ("Fundas", 2)
    # Si el destino ya tiene ese slug, nada cambia (tampoco el nombre).
    made(ana, "iPhone", phones["id"])
    clash = send(ana, "patch", f"{one()}{iphone['id']}/", {"parent_id": phones["id"], "name": "X"})
    assert clash.status_code == 409 and clash.json()["code"] == "CATEGORY_SLUG_TAKEN"
    assert tree(ana)["/iphone/"] == ("Apple", 1)
    actions = migrator.execute(
        "SELECT action, changes::text FROM audit_logs WHERE action LIKE 'category.%' "
        "ORDER BY occurred_at, id"
    ).fetchall()
    assert [row[0] for row in actions].count("category.moved") == 2
    assert ('"path": ["/celulares/iphone/", "/audio/iphone/"]') in actions[4][1]


def test_a_move_cannot_exceed_five_levels(ana: Client) -> None:
    deep = None
    for level in range(4):
        deep = made(ana, f"Nivel {level + 1}", deep["id"] if deep else None)
    branch = made(ana, "Rama")
    made(ana, "Hoja", branch["id"])
    too_deep = send(ana, "patch", f"{one()}{branch['id']}/", {"parent_id": deep["id"]})  # type: ignore[index]
    assert too_deep.status_code == 400 and "parent_id" in too_deep.json()["fields"]


def test_delete_only_without_children_and_it_frees_the_slug(ana: Client) -> None:
    phones = made(ana, "Celulares")
    iphone = made(ana, "iPhone", phones["id"])
    refused = send(ana, "delete", f"{one()}{phones['id']}/")
    assert refused.status_code == 409 and refused.json()["code"] == "CATEGORY_HAS_CHILDREN"
    assert send(ana, "delete", f"{one()}{iphone['id']}/").status_code == 204
    assert send(ana, "delete", f"{one()}{iphone['id']}/").status_code == 404
    assert "/celulares/iphone/" not in tree(ana)
    made(ana, "iPhone", phones["id"])  # el slug quedó libre
    assert send(ana, "patch", f"{one()}{iphone['id']}/", {"name": "X"}).status_code == 404
    empty = send(ana, "patch", f"{one()}{phones['id']}/", {})
    assert empty.status_code == 400


def test_permissions_and_isolation(world: Any) -> None:
    viewer = signed(world.ana)
    assert viewer.get(one()).status_code == 403
    give(world.a, world.membership, VIEW)
    assert viewer.get(one()).status_code == 200
    assert send(viewer, "post", one(), {"name": "Celulares"}).status_code == 403
    with tenant_scope(ctx(world.a)):
        mine = Category.objects.create(name="Mía", slug="mia", path="/mia/", depth=1)
    with tenant_scope(ctx(world.b)):
        foreign = Category.objects.create(name="Ajena", slug="ajena", path="/ajena/", depth=1)
    # Solo `products.view`: lee, pero no renombra, no mueve ni borra.
    assert send(viewer, "patch", f"{one()}{mine.pk}/", {"name": "X"}).status_code == 403
    assert send(viewer, "delete", f"{one()}{mine.pk}/").status_code == 403
    give(world.a, world.membership, MANAGE)
    assert [row["slug"] for row in viewer.get(one()).json()["results"]] == ["mia"]
    child = send(viewer, "post", one(), {"name": "Hija", "parent_id": str(foreign.pk)})
    assert child.status_code == 400 and "parent_id" in child.json()["fields"]  # no existe aquí
    into = send(viewer, "patch", f"{one()}{mine.pk}/", {"parent_id": str(foreign.pk)})
    assert into.status_code == 400
    assert send(viewer, "patch", f"{one()}{foreign.pk}/", {"name": "X"}).status_code == 404
    assert send(viewer, "delete", f"{one()}{foreign.pk}/").status_code == 404
