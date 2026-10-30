"""F3-48i: `?q=` y `?ids=` en los listados de marcas, categorías y productos, y la búsqueda de
variantes entre productos (`GET …/variants/`). Middleware, sesión, motor de autorización y
PostgreSQL con el rol `crm_app`."""

from types import SimpleNamespace
from typing import Any
from urllib.parse import urlencode
from uuid import UUID, uuid4

import psycopg
import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from apps.catalog.brands import create_brand
from apps.catalog.categories import create_category
from apps.catalog.products import create_product, create_variant, delete_product, delete_variant
from core.tenancy.scope import tenant_scope
from tests import test_authorization, test_self_context
from tests.test_authorization import give
from tests.test_memberships import ctx
from tests.test_products import VIEW
from tests.test_self_context import signed

world, real_stack = test_authorization.world, test_self_context.real_stack
pytestmark = pytest.mark.usefixtures("tenant_db")
BASE = "/api/v1/o/org-a/"
ROUTES = ("brands", "categories", "products")


def get(client: Client, route: str, **params: str) -> Any:
    query = f"?{urlencode(params)}" if params else ""
    return client.get(f"{BASE}{route}/{query}")


def rows(client: Client, route: str, **params: str) -> list[dict[str, Any]]:
    response = get(client, route, **params)
    assert response.status_code == 200, response.content
    found: list[dict[str, Any]] = response.json()["results"]
    return found


def names(client: Client, route: str, **params: str) -> set[str]:
    return {row["name"] for row in rows(client, route, **params)}


@pytest.fixture
def shop(world: Any) -> SimpleNamespace:
    """Catálogo de A (con borrados) y un par de filas de B que casan con todo."""
    give(world.a, world.membership, VIEW)
    tenant, other = ctx(world.a), ctx(world.b)
    made: dict[str, Any] = {}
    with tenant_scope(tenant):
        for label in ("Apple", "Samsung", "100% Pure", "Pet_Shop", "Back\\slash", "Sony"):
            made[label] = create_brand(tenant, name=label).pk
        for label in ("Laptops", "Fundas", "Audio"):
            made[label] = create_category(tenant, name=label).pk
        pro = create_product(tenant, name="Laptop Pro", sku_code="lap-1", brand_id=made["Apple"])
        air = create_product(tenant, name="Laptop Air", sku_code="lap-2")
        extra = create_variant(
            tenant, product_id=air.pk, sku_code="lap-2-13", tag="13 pulgadas", barcode="775000111"
        )
        gone_variant = create_variant(tenant, product_id=air.pk, sku_code="lap-2-gone", tag="Vieja")
        mouse = create_product(tenant, name="Mouse Gamer", sku_code="mou-1")
        old = create_product(tenant, name="Laptop Vieja", sku_code="lap-old")
        delete_product(tenant, product_id=old.pk)
        delete_variant(tenant, product_id=air.pk, variant_id=gone_variant.pk)
    with tenant_scope(other):
        foreign = create_product(other, name="Laptop Ajena", sku_code="lap-b")
        create_brand(other, name="Apple Ajena")
        create_category(other, name="Laptops Ajenos")
    return SimpleNamespace(
        ids=made, pro=pro.pk, air=air.pk, extra=extra.pk, mouse=mouse.pk, old=old.pk,
        foreign=foreign.pk, client=signed(world.ana), world=world,
    )  # fmt: skip


def test_q_filters_each_list_by_name_without_case(shop: SimpleNamespace) -> None:
    c = shop.client
    assert names(c, "brands", q="appl") == {"Apple"}
    assert names(c, "brands", q="SAMSUNG") == {"Samsung"}
    assert names(c, "categories", q="laptop") == {"Laptops"}
    assert names(c, "products", q="LAPTOP") == {"Laptop Pro", "Laptop Air"}  # no el borrado
    assert names(c, "products", q="  laptop   pro ") == {"Laptop Pro"}  # recortado
    assert names(c, "products", q="gamer") == {"Mouse Gamer"}
    assert names(c, "brands", q="Apple Ajena") == set()  # otra organización
    assert names(c, "products", q="ajena") == set()
    assert names(c, "brands", q="apple") == {"Apple"}  # no «Apple Ajena», de B


def test_like_wildcards_and_backslash_are_literal(shop: SimpleNamespace) -> None:
    c = shop.client
    assert names(c, "brands", q="%") == {"100% Pure"}
    assert names(c, "brands", q="_") == {"Pet_Shop"}
    assert names(c, "brands", q="\\") == {"Back\\slash"}
    assert names(c, "brands", q="a%e") == names(c, "brands", q="a_e") == set()


def test_empty_q_is_no_filter_and_the_lists_keep_their_order(shop: SimpleNamespace) -> None:
    c = shop.client
    for route in ROUTES:
        plain = rows(c, route)
        assert rows(c, route, q="") == rows(c, route, q="   ") == plain
        assert [r["id"] for r in plain] == sorted(r["id"] for r in plain)
    assert len(rows(c, "brands")) == 6 and len(rows(c, "products")) == 3


def test_q_is_bounded_and_strict(shop: SimpleNamespace) -> None:
    c = shop.client
    for route in ROUTES:
        assert get(c, route, q=" " * 9 + "x" * 254).status_code == 200  # se mide sin espacios
        for q in ("x" * 255, "a\nb", "a\tb", "a\x00b"):
            bad = get(c, route, q=q)
            assert bad.status_code == 400, (route, q)
            body = bad.json()
            assert (body["code"], set(body["fields"])) == ("VALIDATION_ERROR", {"q"})
            assert q not in bad.content.decode()
        twice = c.get(f"{BASE}{route}/?q=a&q=b")
        assert twice.status_code == 400 and set(twice.json()["fields"]) == {"q"}


def test_ids_resolve_only_what_is_live_and_theirs(shop: SimpleNamespace) -> None:
    c, ids = shop.client, shop.ids
    asked = f"{ids['Apple']},{ids['Samsung']},{uuid4()}"  # uno no existe: sin error
    assert names(c, "brands", ids=asked) == {"Apple", "Samsung"}
    assert names(c, "brands", ids=f" {ids['Apple']} , {ids['Apple']}") == {"Apple"}  # repetido
    assert names(c, "categories", ids=str(ids["Audio"])) == {"Audio"}
    assert names(c, "products", ids=f"{shop.pro},{shop.old},{shop.foreign}") == {"Laptop Pro"}
    with_q = names(c, "products", ids=f"{shop.pro},{shop.mouse}", q="mouse")
    assert with_q == {"Mouse Gamer"}  # `q` e `ids` se combinan
    assert names(c, "products", ids=f"{shop.pro},{shop.mouse}", q="zzz") == set()


def test_ids_are_strict(shop: SimpleNamespace) -> None:
    c, good = shop.client, str(shop.pro)
    fifty = ",".join(str(uuid4()) for _ in range(50))
    for route in ROUTES:
        assert get(c, route, ids=fifty).status_code == 200  # 50 caben
        for bad in (fifty + f",{good}", "no-es-uuid", f"{good},", ",", "", f"{good},x", " "):
            response = get(c, route, ids=bad)
            assert response.status_code == 400, (route, bad)
            assert set(response.json()["fields"]) == {"ids"}, (route, bad)
        twice = c.get(f"{BASE}{route}/?ids={good}&ids={good}")
        assert twice.status_code == 400 and set(twice.json()["fields"]) == {"ids"}


def test_the_search_pages_with_the_cursor(shop: SimpleNamespace) -> None:
    c, seen = shop.client, []
    url: str | None = f"{BASE}products/?q=laptop&limit=1"
    while url:
        page = c.get(url).json()
        seen += [r["name"] for r in page["results"]]
        url = page["next"] and f"{BASE}products/?q=laptop&limit=1&cursor={page['next']}"
    assert seen == ["Laptop Pro", "Laptop Air"]
    assert get(c, "products", q="laptop", limit="0").status_code == 400


def test_anonymous_and_foreign_organization(shop: SimpleNamespace) -> None:
    for route in (*ROUTES, "variants"):
        assert Client().get(f"{BASE}{route}/?q=a").status_code == 401, route
    assert shop.client.get("/api/v1/o/org-b/brands/?q=Apple").status_code == 404  # no es miembro


def test_without_products_view_it_is_403(world: Any) -> None:
    client = signed(world.ana)
    for route in (*ROUTES, "variants"):
        assert get(client, route, q="a").status_code == 403, route


def variants(client: Client, **params: str) -> list[dict[str, Any]]:
    return rows(client, "variants", **params)


def skus(client: Client, **params: str) -> set[str]:
    return {r["sku"] for r in variants(client, **params)}


def test_variants_search_by_sku_barcode_name_and_product_name(shop: SimpleNamespace) -> None:
    c = shop.client
    assert skus(c, q="LAP-2-13") == {"LAP-2-13"}
    assert skus(c, q="lap-2-1") == {"LAP-2-13"}  # el SKU se guarda en mayúsculas
    assert skus(c, q="775000") == {"LAP-2-13"}  # código de barras
    assert skus(c, q="pulgadas") == {"LAP-2-13"}  # la etiqueta está en el nombre
    assert skus(c, q="mouse") == {"MOU-1"}
    # El nombre del producto casa con todas sus variantes vivas, cada una una vez.
    found = variants(c, q="laptop")
    assert sorted(r["sku"] for r in found) == ["LAP-1", "LAP-2", "LAP-2-13"]
    assert len({r["id"] for r in found}) == 3
    assert skus(c, q="vieja") == set()  # variante borrada y producto borrado
    assert skus(c, q="ajena") == set() and skus(c, q="lap-b") == set()  # otra organización
    assert skus(c, q="%") == skus(c, q="_") == set()


def test_the_product_name_and_a_live_product_are_checked_in_the_database(
    shop: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    """Aunque el nombre de una variante quedara distinto del de su producto, o una variante viva
    colgara de un producto borrado, la búsqueda sigue la regla: nombre del producto, solo vivos."""
    migrator.execute("UPDATE product_variants SET name = 'otro' WHERE id = %s", [shop.extra])
    assert skus(shop.client, q="laptop air") == {"LAP-2", "LAP-2-13"}
    migrator.execute("UPDATE products SET deleted_at = now() WHERE id = %s", [shop.mouse])
    assert skus(shop.client, q="mou-1") == set()
    assert skus(shop.client, ids=str(next(iter(_ids_of(migrator, shop.mouse))))) == set()


def _ids_of(migrator: psycopg.Connection[Any], product: UUID) -> list[UUID]:
    found = migrator.execute("SELECT id FROM product_variants WHERE product_id = %s", [product])
    return [row[0] for row in found.fetchall()]


def test_variants_return_exactly_the_documented_fields(shop: SimpleNamespace) -> None:
    (row,) = variants(shop.client, q="lap-2-13")
    assert row == {
        "id": str(shop.extra),
        "name": "Laptop Air 13 pulgadas",
        "sku": "LAP-2-13",
        "barcode": "775000111",
        "product_id": str(shop.air),
        "product_name": "Laptop Air",
    }


def test_variants_need_q_or_ids(shop: SimpleNamespace) -> None:
    c = shop.client
    for params in ({}, {"q": ""}, {"q": "   "}, {"limit": "5"}):
        response = get(c, "variants", **params)
        assert response.status_code == 400, params
        assert set(response.json()["fields"]) == {"q"}, params
    assert get(c, "variants", ids=str(shop.extra)).status_code == 200  # solo `ids`
    assert get(c, "variants", q="x").status_code == 200  # solo `q`


def test_variants_ids_resolve_names_and_ignore_what_is_not_theirs(shop: SimpleNamespace) -> None:
    c = shop.client
    with_ids = variants(c, ids=f"{shop.extra},{uuid4()},{shop.foreign}")
    assert [r["sku"] for r in with_ids] == ["LAP-2-13"]
    assert skus(c, ids=f"{shop.extra}", q="mouse") == set()  # combinados
    bad = get(c, "variants", ids="x", q="a")
    assert bad.status_code == 400 and set(bad.json()["fields"]) == {"ids"}
    fifty_one = ",".join(str(uuid4()) for _ in range(51))
    assert get(c, "variants", ids=fifty_one).status_code == 400
    for q in ("x" * 255, "a\nb"):
        assert set(get(c, "variants", q=q).json()["fields"]) == {"q"}


def test_variants_search_pages_with_the_cursor(shop: SimpleNamespace) -> None:
    c, seen = shop.client, []
    url: str | None = f"{BASE}variants/?q=laptop&limit=2"
    while url:
        page = c.get(url).json()
        seen += [r["sku"] for r in page["results"]]
        url = page["next"] and f"{BASE}variants/?q=laptop&limit=2&cursor={page['next']}"
    assert seen == ["LAP-1", "LAP-2", "LAP-2-13"]


def test_the_queries_do_not_grow_with_the_result(shop: SimpleNamespace) -> None:
    tenant = ctx(shop.world.a)
    with tenant_scope(tenant):
        for n in range(48):
            create_product(tenant, name=f"Cable {n}", sku_code=f"cab-{n}")
    c = shop.client
    cases = [
        ("products", {"q": "cable"}),
        ("variants", {"q": "cable"}),
        ("brands", {"q": "a"}),
        ("categories", {"q": "a"}),
    ]
    for route, params in cases:
        counts = []
        for limit in ("2", "50"):
            with CaptureQueriesContext(connection) as queries:
                assert get(c, route, limit=limit, **params).status_code == 200
            counts.append(len(queries))
        assert counts[0] == counts[1], (route, counts)
    assert len(variants(c, q="cable", limit="50")) == 48


def test_the_nested_variants_list_and_plain_lists_are_unchanged(shop: SimpleNamespace) -> None:
    c = shop.client
    nested = c.get(f"{BASE}products/{shop.air}/variants/?q=zzz&ids=nada")  # no la afectan
    assert nested.status_code == 200 and len(nested.json()["results"]) == 2
    assert {r["name"] for r in rows(c, "products")} == {"Laptop Pro", "Laptop Air", "Mouse Gamer"}
