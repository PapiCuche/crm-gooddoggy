"""F3-07: las variantes de un producto (`…/products/{id}/variants/`). Middleware, sesión, motor
de autorización y PostgreSQL con el rol `crm_app`."""

import threading
from typing import Any

import psycopg
import pytest
from django.db import connection
from django.test import Client

from apps.catalog.models import Product, ProductVariant
from apps.catalog.products import (
    LABEL_MAX,
    NAME_MAX,
    delete_variant,
    update_product,
    update_variant,
)
from core.tenancy.scope import tenant_scope
from tests import test_authorization, test_self_context
from tests.test_authorization import give
from tests.test_memberships import ctx
from tests.test_products import MANAGE, VIEW, create, send, url
from tests.test_self_context import signed

world, real_stack = test_authorization.world, test_self_context.real_stack
pytestmark = pytest.mark.usefixtures("tenant_db")


@pytest.fixture
def ana(world: Any) -> Client:
    give(world.a, world.membership, MANAGE)
    return signed(world.ana)


def variants_of(client: Client, product: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = client.get(f"{url()}{product['id']}/variants/").json()["results"]
    return rows


def test_add_edit_and_retire_variants(ana: Client, migrator: psycopg.Connection[Any]) -> None:
    product = create(ana).json()
    path = f"{url()}{product['id']}/variants/"
    added = send(ana, "post", path, {"sku": "ip16p-256-blk", "label": " 256 GB  Negro "})
    assert added.status_code == 201, added.content
    variant = added.json()
    assert (variant["sku"], variant["label"], variant["name"], variant["is_default"]) == (
        "IP16P-256-BLK",
        "256 GB Negro",
        "iPhone 16 Pro 256 GB Negro",
        False,
    )
    edited = send(
        ana, "patch", f"{path}{variant['id']}/", {"label": "512 GB Negro", "barcode": "0194"}
    )
    assert edited.json()["name"] == "iPhone 16 Pro 512 GB Negro"
    # Renombrar el producto mantiene cada variante como producto + etiqueta.
    send(ana, "patch", f"{url()}{product['id']}/", {"name": "iPhone 16"})
    names = sorted(row["name"] for row in variants_of(ana, product))
    assert names == ["iPhone 16", "iPhone 16 512 GB Negro"]
    taken = send(ana, "post", path, {"sku": "IP16P", "label": "otra"})  # el de la por defecto
    assert taken.status_code == 409 and taken.json()["code"] == "SKU_TAKEN"
    assert send(ana, "delete", f"{path}{variant['id']}/").status_code == 204
    assert send(ana, "post", path, {"sku": "IP16P-256-BLK"}).status_code == 201  # SKU libre
    actions = [
        row[0]
        for row in migrator.execute(
            "SELECT action FROM audit_logs WHERE action LIKE 'variant.%' ORDER BY occurred_at, id"
        ).fetchall()
    ]
    assert actions == ["variant.created", "variant.updated", "variant.deleted", "variant.created"]


def test_a_product_never_loses_its_last_variant(ana: Client) -> None:
    product = create(ana).json()
    (default,) = variants_of(ana, product)
    path = f"{url()}{product['id']}/variants/{default['id']}/"
    last = send(ana, "delete", path)
    assert last.status_code == 409 and last.json()["code"] == "LAST_VARIANT"
    other = send(ana, "post", f"{url()}{product['id']}/variants/", {"sku": "OTRA"}).json()
    assert send(ana, "delete", path).status_code == 204  # ya no es la única
    again = send(ana, "delete", f"{url()}{product['id']}/variants/{other['id']}/")
    assert again.status_code == 409  # ahora la única es esta


def test_permissions_and_isolation(world: Any) -> None:
    viewer = signed(world.ana)
    give(world.a, world.membership, VIEW)
    with tenant_scope(ctx(world.a)):
        mine = Product.objects.create(name="Mío", slug="mio")
        variant = ProductVariant.objects.create(
            product=mine, sku="MIO", name="Mío", is_default=True
        )
    with tenant_scope(ctx(world.b)):
        foreign = Product.objects.create(name="Ajeno", slug="ajeno")
        theirs = ProductVariant.objects.create(
            product=foreign, sku="AJENO", name="Ajeno", is_default=True
        )
    path = f"{url()}{mine.pk}/variants/"
    assert send(viewer, "post", path, {"sku": "X"}).status_code == 403
    assert send(viewer, "patch", f"{path}{variant.pk}/", {"sku": "X"}).status_code == 403
    assert send(viewer, "delete", f"{path}{variant.pk}/").status_code == 403
    give(world.a, world.membership, MANAGE)
    assert send(viewer, "post", f"{url()}{foreign.pk}/variants/", {"sku": "X"}).status_code == 404
    # La variante de otro producto u otra organización no se toca por esta ruta.
    assert send(viewer, "patch", f"{path}{theirs.pk}/", {"sku": "X"}).status_code == 404
    assert send(viewer, "delete", f"{path}{theirs.pk}/").status_code == 404


def test_the_longest_name_and_label_fit(ana: Client) -> None:
    product = create(ana, name="N" * NAME_MAX, sku="LARGO").json()
    path = f"{url()}{product['id']}/variants/"
    added = send(ana, "post", path, {"sku": "LARGO-2", "label": "L" * LABEL_MAX})
    assert added.status_code == 201, added.content
    assert len(added.json()["name"]) == NAME_MAX + 1 + LABEL_MAX
    short = create(ana, name="Corto", sku="CORTO").json()
    send(ana, "post", f"{url()}{short['id']}/variants/", {"sku": "C-2", "label": "L" * LABEL_MAX})
    renamed = send(ana, "patch", f"{url()}{short['id']}/", {"name": "M" * NAME_MAX})
    assert renamed.status_code == 200, renamed.content  # renombrar no desborda sus variantes


def test_the_sku_of_an_edit_is_cleaned_and_unique(ana: Client) -> None:
    product = create(ana).json()
    path = f"{url()}{product['id']}/variants/"
    other = send(ana, "post", path, {"sku": "OTRA"}).json()
    edited = send(ana, "patch", f"{path}{other['id']}/", {"sku": " otra-2 "})
    assert edited.status_code == 200 and edited.json()["sku"] == "OTRA-2"
    taken = send(ana, "patch", f"{path}{other['id']}/", {"sku": "ip16p"})
    assert taken.status_code == 409 and taken.json()["code"] == "SKU_TAKEN"


def test_a_variant_of_another_product_is_not_reached(ana: Client) -> None:
    one, two = create(ana).json(), create(ana, name="Otro", sku="OTRO").json()
    (theirs,) = variants_of(ana, two)
    send(ana, "post", f"{url()}{two['id']}/variants/", {"sku": "OTRO-2"})  # no sería la última
    path = f"{url()}{one['id']}/variants/{theirs['id']}/"
    assert send(ana, "patch", path, {"label": "x"}).status_code == 404
    assert send(ana, "delete", path).status_code == 404
    assert len(variants_of(ana, two)) == 2


def hold_then(world: Any, first: Any, second: Any) -> dict[str, str]:
    """`first` escribe y retiene su transacción; `second` debe esperar el bloqueo del producto."""
    wrote, release, outcome = threading.Event(), threading.Event(), dict[str, str]()

    def run(name: str, change: Any) -> None:
        try:
            with tenant_scope(ctx(world.a, world.ana)) as tenant:
                change(tenant)
                if name == "first":
                    wrote.set()
                    release.wait(timeout=10)
            outcome[name] = "ok"
        except Exception as error:  # noqa: BLE001 — el hilo informa qué excepción vio
            outcome[name] = type(error).__name__
        finally:
            wrote.set()
            connection.close()

    holder = threading.Thread(target=run, args=("first", first))
    waiter = threading.Thread(target=run, args=("second", second))
    holder.start()
    assert wrote.wait(timeout=10)
    waiter.start()
    waiter.join(timeout=1)
    assert waiter.is_alive() and "second" not in outcome  # espera el bloqueo del producto
    release.set()
    holder.join(timeout=10)
    waiter.join(timeout=10)
    return outcome


def test_two_deletes_at_once_keep_one_variant(ana: Client, world: Any) -> None:
    product = create(ana).json()
    send(ana, "post", f"{url()}{product['id']}/variants/", {"sku": "OTRA"})
    first, second = (row["id"] for row in variants_of(ana, product))

    def retire(variant_id: Any) -> Any:
        return lambda tenant: delete_variant(
            tenant, product_id=product["id"], variant_id=variant_id
        )

    outcome = hold_then(world, retire(first), retire(second))
    assert outcome == {"first": "ok", "second": "LastVariant"}
    assert [row["id"] for row in variants_of(ana, product)] == [second]


def test_a_rename_and_a_label_edit_at_once_keep_the_name(ana: Client, world: Any) -> None:
    product = create(ana).json()
    path = f"{url()}{product['id']}/variants/"
    variant = send(ana, "post", path, {"sku": "OTRA", "label": "A"}).json()
    outcome = hold_then(
        world,
        lambda tenant: update_product(tenant, product_id=product["id"], name="Nuevo"),
        lambda tenant: update_variant(
            tenant, product_id=product["id"], variant_id=variant["id"], label="B"
        ),
    )
    assert outcome == {"first": "ok", "second": "ok"}
    assert sorted(row["name"] for row in variants_of(ana, product)) == ["Nuevo", "Nuevo B"]
