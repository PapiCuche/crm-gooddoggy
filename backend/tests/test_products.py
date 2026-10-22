"""F3-06: productos con su variante por defecto (`…/products/`). Middleware, sesión, motor de
autorización y PostgreSQL con el rol `crm_app`."""

from typing import Any

import psycopg
import pytest
from django.test import Client
from django.utils import timezone

from apps.catalog.models import Brand, Category, Product, ProductVariant
from core.tenancy.scope import tenant_scope
from tests import test_authorization, test_self_context
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
