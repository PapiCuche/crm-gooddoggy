"""F3-06 y F3-06b: productos con su variante por defecto (`…/products/`, `…/variants/`).
Middleware, sesión, motor de autorización y PostgreSQL con el rol `crm_app`."""

from typing import Any

import psycopg
import pytest
from django.test import Client
from django.utils import timezone

from apps.catalog.brands import BrandInUse, delete_brand
from apps.catalog.categories import CategoryInUse, delete_category
from apps.catalog.models import Brand, Category, Product, ProductVariant
from apps.catalog.products import BadReference, create_product
from core.tenancy.scope import tenant_scope
from tests import test_authorization, test_self_context
from tests.test_anti_escalation import race
from tests.test_authorization import give
from tests.test_memberships import ctx
from tests.test_self_context import signed

world, real_stack = test_authorization.world, test_self_context.real_stack
pytestmark = pytest.mark.usefixtures("tenant_db")
PRODUCTS = "/api/v1/o/{}/products/"
VIEW: dict[str, str | None] = {"products.view": None}
MANAGE: dict[str, str | None] = {
    "products.view": None,
    "products.manage": None,
    "brands.manage": None,
    "categories.manage": None,
}


def send(client: Client, method: str, path: str, body: Any = None) -> Any:
    client.cookies["csrftoken"] = token = "t" * 32
    return getattr(client, method)(path, body, "application/json", headers={"X-CSRFToken": token})


def url(org: str = "org-a") -> str:
    return PRODUCTS.format(org)


def create(client: Client, **body: Any) -> Any:
    return send(client, "post", url(), {"name": "iPhone 16 Pro", "sku": "ip16p", **body})


@pytest.fixture
def ana(world: Any) -> Client:
    give(world.a, world.membership, MANAGE)
    return signed(world.ana)


def test_a_product_is_born_with_its_default_variant(
    ana: Client, world: Any, migrator: psycopg.Connection[Any]
) -> None:
    made = create(ana, short_description="  Titanio  ")
    assert made.status_code == 201, made.content
    product = made.json()
    assert (product["slug"], product["status"], product["product_type"]) == (
        "iphone-16-pro",
        "DRAFT",
        "PHYSICAL",
    )
    assert product["tracks_inventory"] is True and product["short_description"] == "Titanio"
    with tenant_scope(ctx(world.a)):
        variants = ProductVariant.objects.filter(product_id=product["id"])
        assert [(v.sku, v.name, v.is_default, v.status) for v in variants] == [
            ("IP16P", "iPhone 16 Pro", True, "ACTIVE")  # el SKU en mayúsculas
        ]
    row = migrator.execute(
        "SELECT action, changes::text FROM audit_logs WHERE action = 'product.created'"
    ).fetchone()
    assert row is not None and '"sku": [null, "IP16P"]' in row[1]


def test_a_service_carries_no_stock(ana: Client) -> None:
    service = create(ana, name="Cambio de batería", sku="SRV-BAT", product_type="SERVICE")
    assert service.json()["tracks_inventory"] is False


def test_slug_and_sku_are_unique_among_live_ones(ana: Client, world: Any) -> None:
    first = create(ana).json()
    slug = create(ana, sku="OTRO")
    assert slug.status_code == 409 and slug.json()["code"] == "PRODUCT_SLUG_TAKEN"
    sku = create(ana, slug="otro-iphone", sku="IP16P")
    assert sku.status_code == 409 and sku.json()["code"] == "SKU_TAKEN"
    bad = create(ana, slug="x", sku="ñ 1")
    assert bad.status_code == 400 and "sku" in bad.json()["fields"]
    with tenant_scope(ctx(world.a)):  # un borrado lógico libera el slug y el SKU
        now = timezone.now()
        Product.objects.filter(pk=first["id"]).update(deleted_at=now)
        ProductVariant.objects.filter(product_id=first["id"]).update(deleted_at=now)
    assert create(ana).status_code == 201


def test_brand_and_category_from_this_organization(ana: Client, world: Any) -> None:
    brand = send(ana, "post", "/api/v1/o/org-a/brands/", {"name": "Apple"}).json()
    category = send(ana, "post", "/api/v1/o/org-a/categories/", {"name": "Celulares"}).json()
    product = create(ana, brand_id=brand["id"], category_id=category["id"]).json()
    assert product["brand"] == {"id": brand["id"], "name": "Apple"}
    assert product["category"] == {"id": category["id"], "name": "Celulares"}
    with tenant_scope(ctx(world.b)):
        foreign_brand = Brand.objects.create(name="Ajena", slug="ajena")
        foreign_category = Category.objects.create(
            name="Ajena", slug="ajena", path="/ajena/", depth=1
        )
    for field, value in (("brand_id", foreign_brand.pk), ("category_id", foreign_category.pk)):
        refused = create(ana, name="Otro", sku="OTRO", **{field: str(value)})
        assert refused.status_code == 400 and field in refused.json()["fields"]
    assert [row["slug"] for row in ana.get(url()).json()["results"]] == ["iphone-16-pro"]


def test_permissions_and_isolation(world: Any) -> None:
    viewer = signed(world.ana)
    assert viewer.get(url()).status_code == 403
    give(world.a, world.membership, VIEW)
    with tenant_scope(ctx(world.a)):
        Product.objects.create(name="Mío", slug="mio")
    with tenant_scope(ctx(world.b)):
        Product.objects.create(name="Ajeno", slug="ajeno")
    assert [row["slug"] for row in viewer.get(url()).json()["results"]] == ["mio"]
    assert create(viewer).status_code == 403


def test_update_renames_the_default_variant_and_changes_status(ana: Client) -> None:
    product = create(ana).json()
    changed = send(
        ana, "patch", f"{url()}{product['id']}/", {"name": "iPhone 16 Pro Max", "status": "ACTIVE"}
    )
    assert changed.status_code == 200
    assert (changed.json()["name"], changed.json()["slug"], changed.json()["status"]) == (
        "iPhone 16 Pro Max",
        "iphone-16-pro",  # el slug no cambia
        "ACTIVE",
    )
    (variant,) = ana.get(f"{url()}{product['id']}/variants/").json()["results"]
    assert (variant["name"], variant["sku"], variant["is_default"]) == (
        "iPhone 16 Pro Max",
        "IP16P",
        True,
    )


def test_deleting_frees_its_slug_and_skus(ana: Client) -> None:
    first = create(ana).json()
    assert send(ana, "delete", f"{url()}{first['id']}/").status_code == 204
    assert create(ana).status_code == 201  # el slug y el SKU quedaron libres
    assert ana.get(f"{url()}{first['id']}/variants/").status_code == 404
    assert send(ana, "delete", f"{url()}{first['id']}/").status_code == 404


def test_brand_and_category_in_use(ana: Client, world: Any) -> None:
    brand = send(ana, "post", "/api/v1/o/org-a/brands/", {"name": "Apple"}).json()
    category = send(ana, "post", "/api/v1/o/org-a/categories/", {"name": "Celulares"}).json()
    product = create(ana, brand_id=brand["id"], category_id=category["id"]).json()
    in_use = send(ana, "delete", f"/api/v1/o/org-a/brands/{brand['id']}/")
    assert in_use.status_code == 409 and in_use.json()["code"] == "BRAND_IN_USE"
    used = send(ana, "delete", f"/api/v1/o/org-a/categories/{category['id']}/")
    assert used.status_code == 409 and used.json()["code"] == "CATEGORY_IN_USE"
    with tenant_scope(ctx(world.b)):
        foreign_brand = Brand.objects.create(name="Ajena", slug="ajena")
        foreign_category = Category.objects.create(
            name="Ajena", slug="ajena", path="/ajena/", depth=1
        )
    for field, value in (("brand_id", foreign_brand.pk), ("category_id", foreign_category.pk)):
        refused = send(ana, "patch", f"{url()}{product['id']}/", {field: str(value)})
        assert refused.status_code == 400 and field in refused.json()["fields"]
    cleared = send(ana, "patch", f"{url()}{product['id']}/", {"brand_id": None})
    assert cleared.json()["brand"] is None
    assert send(ana, "delete", f"/api/v1/o/org-a/brands/{brand['id']}/").status_code == 204
    assert send(ana, "delete", f"{url()}{product['id']}/").status_code == 204
    assert send(ana, "delete", f"/api/v1/o/org-a/categories/{category['id']}/").status_code == 204


def test_edit_permissions_and_isolation(world: Any) -> None:
    viewer = signed(world.ana)
    with tenant_scope(ctx(world.a)):
        mine = Product.objects.create(name="Mío", slug="mio")
        variant = ProductVariant.objects.create(
            product=mine, sku="MIO", name="Mío", is_default=True
        )
    with tenant_scope(ctx(world.b)):
        foreign = Product.objects.create(name="Ajeno", slug="ajeno")
    assert viewer.get(f"{url()}{mine.pk}/variants/").status_code == 403  # sin `products.view`
    give(world.a, world.membership, VIEW)
    assert send(viewer, "patch", f"{url()}{mine.pk}/", {"name": "X"}).status_code == 403
    assert send(viewer, "delete", f"{url()}{mine.pk}/").status_code == 403
    variants = viewer.get(f"{url()}{mine.pk}/variants/").json()["results"]
    assert [row["id"] for row in variants] == [str(variant.pk)]
    give(world.a, world.membership, MANAGE)
    assert send(viewer, "patch", f"{url()}{foreign.pk}/", {"name": "X"}).status_code == 404
    assert send(viewer, "delete", f"{url()}{foreign.pk}/").status_code == 404
    assert viewer.get(f"{url()}{foreign.pk}/variants/").status_code == 404


def test_each_write_is_audited(ana: Client, migrator: psycopg.Connection[Any]) -> None:
    product = create(ana).json()
    path = f"{url()}{product['id']}/"
    assert send(ana, "patch", path, {"status": "ACTIVE"}).status_code == 200
    assert send(ana, "patch", path, {"status": "ACTIVE"}).status_code == 200  # nada cambia
    assert send(ana, "delete", path).status_code == 204
    rows = migrator.execute(
        "SELECT action, changes, metadata FROM audit_logs WHERE entity_type = 'product' "
        "ORDER BY occurred_at, id"
    ).fetchall()
    assert [row[0] for row in rows] == ["product.created", "product.updated", "product.deleted"]
    assert rows[1][1] == {"status": ["DRAFT", "ACTIVE"]} and rows[2][2] == {"variants": 1}


def test_a_brand_is_not_deleted_while_a_product_starts_using_it(world: Any) -> None:
    """El producto que se crea retiene el bloqueo de la marca; el borrado la espera y, al
    releerla, ve el producto ya confirmado: `BrandInUse`, nunca una marca borrada en uso."""
    with tenant_scope(ctx(world.a)):
        apple = Brand.objects.create(name="Apple", slug="apple")
    refused: list[str] = []

    def remove(tenant: Any) -> None:
        try:
            delete_brand(tenant, brand_id=apple.pk)
        except BrandInUse:
            refused.append("BRAND_IN_USE")

    changes = {
        "create": (
            world.ana,
            lambda t: create_product(t, name="P", sku_code="P1", brand_id=apple.pk),
        ),
        "delete": (world.ana, remove),
    }
    assert race(world.a, changes, hold="create") == {"create": "ok", "delete": "ok"}
    assert refused == ["BRAND_IN_USE"]
    with tenant_scope(ctx(world.a)):
        assert Brand.objects.get(pk=apple.pk).deleted_at is None


def test_a_category_is_not_deleted_while_a_product_starts_using_it(world: Any) -> None:
    """Lo mismo con la categoría: su borrado también bloquea su fila antes de mirar."""
    with tenant_scope(ctx(world.a)):
        phones = Category.objects.create(name="Cel", slug="cel", path="/cel/", depth=1)
    refused: list[str] = []

    def remove(tenant: Any) -> None:
        try:
            delete_category(tenant, category_id=phones.pk)
        except CategoryInUse:
            refused.append("CATEGORY_IN_USE")

    def make(tenant: Any) -> None:
        create_product(tenant, name="P", sku_code="P1", category_id=phones.pk)

    changes = {"create": (world.ana, make), "delete": (world.ana, remove)}
    assert race(world.a, changes, hold="create") == {"create": "ok", "delete": "ok"}
    assert refused == ["CATEGORY_IN_USE"]


def test_a_product_does_not_take_a_brand_being_deleted(world: Any) -> None:
    """Al revés: el borrado retiene la marca; quien crea el producto espera y, al releerla, la
    ve borrada (400 en `brand_id`), nunca un producto vivo con una marca borrada."""
    with tenant_scope(ctx(world.a)):
        apple = Brand.objects.create(name="Apple", slug="apple")
    refused: list[str] = []

    def make(tenant: Any) -> None:
        try:
            create_product(tenant, name="P", sku_code="P1", brand_id=apple.pk)
        except BadReference as error:
            refused.append(error.field)

    def remove(tenant: Any) -> None:
        delete_brand(tenant, brand_id=apple.pk)

    changes = {"delete": (world.ana, remove), "create": (world.ana, make)}
    assert race(world.a, changes, hold="delete") == {"delete": "ok", "create": "ok"}
    assert refused == ["brand_id"]
    with tenant_scope(ctx(world.a)):
        assert not Product.objects.exists()
