"""F3-37 (#361): quien borra algo del catálogo es miembro de su organización (FK compuesta a las
membresías, ADR-001 §3) y la migración `catalog/0005`: se niega ante filas huérfanas sin tocarlas,
corre solo con `BYPASSRLS`, no espera sin fin un bloqueo, se puede repetir y vuelve atrás.
PostgreSQL real; el runtime es `crm_app`."""

import time
from collections.abc import Callable
from importlib import import_module
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from django.core.management import call_command
from django.db import IntegrityError, OperationalError, connection

from apps.catalog.brands import create_brand, delete_brand
from apps.catalog.categories import create_category, delete_category
from apps.catalog.models import Brand, Category, Product, ProductVariant
from apps.catalog.products import create_product, create_variant, delete_product, delete_variant
from core.tenancy.scope import tenant_scope
from tests import test_authorization
from tests.conftest import migrator_conninfo
from tests.factories import make_user
from tests.test_memberships import ctx, join
from tests.test_pipelines_tables import as_migrator

world = test_authorization.world
pytestmark = pytest.mark.usefixtures("tenant_db")
DB = psycopg.Connection[Any]
MIGRATION = import_module("apps.catalog.migrations.0005_deleted_by_member_fk")
MODELS = {
    "brands": Brand, "categories": Category, "products": Product, "product_variants": ProductVariant
}  # fmt: skip
CLEAR = "UPDATE {} SET deleted_by_user_id = NULL WHERE deleted_at IS NULL"
DEFINITION = (
    "FOREIGN KEY (organization_id, deleted_by_user_id)"
    " REFERENCES organization_memberships(organization_id, user_id)"
)


def constraint(raised: pytest.ExceptionInfo[IntegrityError]) -> str:
    return str(raised.value.__cause__.diag.constraint_name)  # type: ignore[union-attr]


@pytest.fixture
def shop(world: Any) -> SimpleNamespace:
    """En A: una marca, una categoría y un producto con dos variantes, de `ana` (miembro de A).
    `luis` es miembro solo de B."""
    luis = make_user(email="luis@example.com")
    join(world.b, luis)
    with tenant_scope(mine := ctx(world.a, world.ana)):
        product = create_product(mine, name="Laptop", sku_code="lap-1")
        variant = create_variant(mine, product_id=product.pk, sku_code="lap-2", tag="Azul")
        brand = create_brand(mine, name="Apple")
        category = create_category(mine, name="Laptops")
    return SimpleNamespace(
        **vars(world), luis=luis, brand=brand, category=category, product=product, variant=variant
    )


def fks(migrator: DB) -> dict[str, tuple[str, bool]]:
    found = migrator.execute(
        "SELECT conname, pg_get_constraintdef(oid), convalidated FROM pg_constraint"
        " WHERE conname LIKE '%%_deleted_by_fk' AND conrelid = ANY(%s::regclass[])",
        [list(MODELS)],
    ).fetchall()
    return {name: (definition, valid) for name, definition, valid in found}


def live(migrator: DB) -> list[Any]:
    """Por tabla: cuántas filas siguen vivas y quién figura como quien las borró."""
    query = "SELECT count(*) FILTER (WHERE deleted_at IS NULL), array_agg(deleted_by_user_id) FROM "
    return [migrator.execute(query + table).fetchone() for table in MODELS]


def test_every_catalog_user_column_has_its_member_fk(migrator: DB) -> None:
    columns = migrator.execute(
        "SELECT table_name, column_name FROM information_schema.columns WHERE table_schema ="
        " 'public' AND table_name = ANY(%s) AND column_name LIKE '%%\\_user\\_id'",
        [list(MODELS)],
    ).fetchall()
    assert sorted(columns) == sorted((table, "deleted_by_user_id") for table in MODELS)
    assert fks(migrator) == {f"{table}_deleted_by_fk": (DEFINITION, True) for table in MODELS}


def test_a_delete_by_someone_who_is_not_a_member_is_refused(shop: Any, migrator: DB) -> None:
    before = live(migrator)
    stranger = ctx(shop.a, shop.luis)  # miembro de B, no de A: la base rechaza su baja
    deletes: dict[str, Callable[[], None]] = {
        "brands": lambda: delete_brand(stranger, brand_id=shop.brand.pk),
        "categories": lambda: delete_category(stranger, category_id=shop.category.pk),
        "product_variants": lambda: delete_variant(
            stranger, product_id=shop.product.pk, variant_id=shop.variant.pk
        ),
    }
    for table, delete in deletes.items():
        with pytest.raises(IntegrityError) as raised, tenant_scope(stranger):
            delete()
        assert constraint(raised) == f"{table}_deleted_by_fk"
    for table, model in MODELS.items():  # cada tabla por sí sola, como `crm_app`
        for who in (shop.luis.pk, uuid4()):
            with pytest.raises(IntegrityError) as raised, tenant_scope(ctx(shop.a)):
                model.objects.update(deleted_by_user_id=who)
            assert constraint(raised) == f"{table}_deleted_by_fk"
        with pytest.raises(psycopg.errors.ForeignKeyViolation):  # también para el migrador
            migrator.execute(f"UPDATE {table} SET deleted_by_user_id = %s", [uuid4()])  # noqa: S608
    assert live(migrator) == before  # nada se borró
    mine = ctx(shop.a, shop.ana)  # quien es miembro sí borra, y queda quién fue
    with tenant_scope(mine):
        delete_brand(mine, brand_id=shop.brand.pk)
        delete_category(mine, category_id=shop.category.pk)
        delete_variant(mine, product_id=shop.product.pk, variant_id=shop.variant.pk)
        delete_product(mine, product_id=shop.product.pk)
    assert live(migrator) == [(0, [shop.ana.pk] * n) for n in (1, 1, 1, 2)]


def test_the_migration_refuses_orphans_waits_for_no_lock_repeats_and_goes_back(
    shop: Any, migrator: DB
) -> None:
    orphan = "UPDATE {} SET deleted_by_user_id = %s WHERE id = %s"
    try:
        with as_migrator():
            call_command("migrate", "catalog", "0004", verbosity=0)
        assert fks(migrator) == {}  # hacia atrás quita las cuatro
        migrator.execute(orphan.format("brands"), [shop.luis.pk, shop.brand.pk])
        migrator.execute(orphan.format("product_variants"), [uuid4(), shop.variant.pk])
        before = live(migrator)
        message = r"2 deleted catalog rows .* \(brands: 1, product_variants: 1\)"
        with as_migrator(), pytest.raises(RuntimeError, match=message):
            call_command("migrate", "catalog", verbosity=0)
        assert (fks(migrator), live(migrator)) == ({}, before)  # ni FKs ni datos tocados
        with connection.schema_editor() as editor, pytest.raises(RuntimeError, match="BYPASSRLS"):
            MIGRATION.add(None, editor)  # `crm_app`: bajo RLS no vería las huérfanas
        migrator.execute(orphan.format("brands"), [None, shop.brand.pk])
        migrator.execute(orphan.format("product_variants"), [None, shop.variant.pk])
        migrator.execute(  # un intento anterior que falló a medias dejó una sin validar
            f"ALTER TABLE categories ADD CONSTRAINT categories_deleted_by_fk FOREIGN KEY"
            f" (organization_id, deleted_by_user_id) REFERENCES {MIGRATION.MEMBERS} NOT VALID"
        )
        with psycopg.connect(migrator_conninfo()) as holder:  # una transacción larga en brands
            holder.execute("LOCK TABLE brands IN ACCESS EXCLUSIVE MODE")
            started = time.monotonic()
            with as_migrator(), pytest.raises(OperationalError, match="lock timeout"):
                call_command("migrate", "catalog", verbosity=0)
            assert time.monotonic() - started < 30  # `lock_timeout`: falla en vez de esperar
        with as_migrator():
            call_command("migrate", "catalog", verbosity=0)
        assert fks(migrator) == {f"{table}_deleted_by_fk": (DEFINITION, True) for table in MODELS}
        with as_migrator(), connection.schema_editor() as editor:
            MIGRATION.add(None, editor)  # repetirla no cambia nada
            with connection.cursor() as cursor:
                cursor.execute("SHOW lock_timeout")
                assert cursor.fetchone() == ("0",)  # la sesión vuelve a su valor
        assert len(fks(migrator)) == 4
    finally:
        for table in MODELS:  # las huérfanas del test, para dejar la base migrada
            migrator.execute(CLEAR.format(table))
        with as_migrator():
            call_command("migrate", "catalog", verbosity=0)
