"""F5-09a (E13-01): las tablas `quotes` y `quote_items` con PostgreSQL real y el rol `crm_app`:
aislamiento (también por SQL), FKs compuestas, `CHECK` de importes y estados, unicidad, permisos
de la aplicación y la migración. Los comandos llegan con F5-09a2 y la API, después."""

from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from django.core.management import call_command
from django.db import DatabaseError, IntegrityError, ProgrammingError, connection, transaction
from django.db.migrations.loader import MigrationLoader
from django.utils import timezone

from apps.quotes.models import Quote, QuoteItem
from core.tenancy.scope import tenant_scope
from tests import test_opportunities_tables as tables
from tests.test_memberships import ctx
from tests.test_pipelines_tables import as_migrator
from tests.test_prices import price_list

world = tables.world
pytestmark = pytest.mark.usefixtures("tenant_db")
type DB = psycopg.Connection[Any]
NOW = timezone.now()
MONEY = Decimal("100.00")
_codes = iter(range(10_000))


def quote(world: SimpleNamespace, org: str = "a", **over: Any) -> Quote:
    """Una cotización en borrador, con lo mínimo: la lista y el contacto de la organización."""
    mine = getattr(world, org)
    fields: dict[str, Any] = {
        "number": "COT-000001", "contact_id": tables.contact(mine).pk,
        "price_list_id": price_list(mine, f"L{next(_codes)}"), "currency": "PEN",
        "tax_rate": Decimal("0.18"), "created_by_user_id": world.ana.pk if org == "a" else None,
    }  # fmt: skip
    with tenant_scope(ctx(mine)):
        made: Quote = Quote.objects.create(**{**fields, **over})
    return made


def new_variant(world: SimpleNamespace) -> Any:
    return tables.variant(world.a, f"V{next(_codes)}").pk


def line(world: SimpleNamespace, made: Quote, **over: Any) -> QuoteItem:
    """Una línea sin descuento de 100,00 por unidad."""
    fields: dict[str, Any] = {
        "quote": made, "sort_order": 1, "variant_id": over.get("variant_id") or new_variant(world),
        "product_name_snapshot": "iPhone", "variant_name_snapshot": "iPhone 16",
        "sku_snapshot": "IP16",
        "quantity": 2, "list_unit_price": MONEY, "unit_price": MONEY, "final_unit_price": MONEY,
        "line_total": 2 * MONEY, "currency": "PEN",
    }  # fmt: skip
    with tenant_scope(ctx(world.a)):
        created: QuoteItem = QuoteItem.objects.create(**{**fields, **over})
    return created


def sql(org: Any, statement: str, params: list[Any] | None = None) -> list[Any]:
    with tenant_scope(ctx(org)), connection.cursor() as cursor:
        cursor.execute(statement, params)
        return cursor.fetchall() if cursor.description else [(cursor.rowcount,)]


def test_what_a_creates_never_exists_for_b_not_even_by_sql(world: SimpleNamespace) -> None:
    line(world, quote(world))
    with tenant_scope(ctx(world.b)):
        assert not Quote.objects.exists() and not QuoteItem.objects.exists()
    for table in ("quotes", "quote_items"):  # RLS, no solo el manager
        assert sql(world.a, f"SELECT count(*) FROM {table}") == [(1,)]  # noqa: S608
        assert sql(world.b, f"SELECT count(*) FROM {table}") == [(0,)]  # noqa: S608
    assert sql(world.b, "UPDATE quotes SET internal_notes = 'otra'") == [(0,)]  # ni las cambia
    assert sql(world.b, "UPDATE quote_items SET quantity = 9") == [(0,)]
    with pytest.raises(DatabaseError, match="row-level security"):  # ni las mueve de organización
        sql(world.a, "UPDATE quotes SET organization_id = %s", [world.b])
    with pytest.raises(DatabaseError, match="row-level security"):
        sql(world.a, "UPDATE quote_items SET organization_id = %s", [world.b])


ROW = (  # una cotización completa, de la organización que se diga
    "INSERT INTO quotes (id, organization_id, number, revision, contact_id, price_list_id,"
    " status, currency, tax_mode, tax_rate, subtotal, discount_total, tax_total, total, version,"
    " created_at, updated_at, notes_to_customer, internal_notes)"
    " VALUES (%s, %s, 'COT-000001', 1, %s, %s, 'DRAFT', 'PEN', 'INCLUDED', 0.18, 0, 0, 0, 0, 1,"
    " now(), now(), '', '')"
)


def test_the_organization_is_a_real_reference_and_rls_refuses_a_foreign_insert(
    world: SimpleNamespace, migrator: DB
) -> None:
    mine = quote(world)
    with pytest.raises(DatabaseError, match="row-level security"):  # ni las escribe en A
        sql(world.b, ROW, [uuid4(), world.a, mine.contact_id, mine.price_list_id])
    with pytest.raises(psycopg.errors.ForeignKeyViolation, match="quotes_organization_fk"):
        migrator.execute(ROW, [uuid4(), uuid4(), uuid4(), uuid4()])  # una organización inventada


def test_both_organization_references_exist_in_the_catalog(migrator: DB) -> None:
    """Por el catálogo y no por el orden en que PostgreSQL dispara las FKs de una fila inventada."""
    for table in ("quotes", "quote_items"):
        assert migrator.execute(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = %s",
            [f"{table}_organization_fk"],
        ).fetchone() == ("FOREIGN KEY (organization_id) REFERENCES organizations(id)",)


def test_a_complete_quote_with_a_promotion_and_a_cancelled_one_are_valid(
    world: SimpleNamespace,
) -> None:
    made = quote(
        world, subtotal=Decimal("84.75"), tax_total=Decimal("15.25"), total=MONEY,
        discount_total=Decimal("20.00"), opportunity_id=tables.make(world.a).pk,
    )  # fmt: skip
    discounted = line(world, made, promotion_id_snapshot=uuid4(), promotion_name_snapshot="Promo",
                      list_unit_price=Decimal("120.00"), final_unit_price=MONEY, quantity=1,
                      line_total=MONEY, discount_amount=Decimal("20.00"))  # fmt: skip
    assert discounted.final_unit_price == MONEY and made.version == 1
    manual = line(world, made, sort_order=2, discount_type="PERCENT", discount_value=10,
                  discount_amount=10, final_unit_price=Decimal("90"), quantity=1,
                  line_total=Decimal("90"))  # fmt: skip
    assert (manual.unit_price, manual.final_unit_price) == (MONEY, Decimal("90"))
    gone = quote(world, number="COT-000002", status="CANCELLED", cancelled_at=NOW,
                 cancel_reason="El cliente ya no quiere")  # fmt: skip
    assert (gone.status, gone.revision) == ("CANCELLED", 1)


def test_composite_foreign_keys_keep_every_reference_in_the_organization(
    world: SimpleNamespace,
) -> None:
    mine = quote(world)
    foreign = {
        "contact_id": tables.contact(world.b).pk,
        "opportunity_id": tables.make(world.b).pk,
        "price_list_id": price_list(world.b, "OTRA"),
        "created_by_user_id": world.luis.pk,  # no es miembro de A
        "updated_by_user_id": world.luis.pk,
    }
    for field, other in foreign.items():
        constraint = {"created_by_user_id": "quotes_created_by_fk"}.get(
            field, f"quotes_{field.removesuffix('_id').removesuffix('_user')}_fk"
        )
        constraint = constraint.replace("updated_by_user", "updated_by")
        with tenant_scope(ctx(world.a)), pytest.raises(IntegrityError, match=constraint):
            with transaction.atomic():
                Quote.objects.filter(pk=mine.pk).update(**{field: other})
    with tenant_scope(ctx(world.a)), pytest.raises(IntegrityError, match="quotes_price_list_fk"):
        with transaction.atomic():  # la moneda de la cotización es la de su lista
            Quote.objects.filter(pk=mine.pk).update(currency="USD")
    row = line(world, mine)
    other_variant = tables.variant(world.b, "AJENA").pk
    with tenant_scope(ctx(world.a)), pytest.raises(IntegrityError, match="quote_items_variant_fk"):
        with transaction.atomic():
            QuoteItem.objects.filter(pk=row.pk).update(variant_id=other_variant)
    with tenant_scope(ctx(world.a)), pytest.raises(IntegrityError, match="quote_items_quote_fk"):
        with transaction.atomic():  # una línea no cambia de moneda respecto a su cotización
            QuoteItem.objects.filter(pk=row.pk).update(currency="USD")
    foreign_quote = quote(world, "b").pk
    with tenant_scope(ctx(world.a)), pytest.raises(IntegrityError, match="quote_items_quote_fk"):
        with transaction.atomic():
            QuoteItem.objects.filter(pk=row.pk).update(quote_id=foreign_quote)


@pytest.mark.parametrize(
    ("fields", "constraint"),
    [
        ({"number": "COT-12"}, "quotes_number_ck"),
        ({"number": "XXX-000001"}, "quotes_number_ck"),
        ({"revision": 0}, "quotes_revision_ck"),
        ({"status": "SOON"}, "quotes_status_ck"),
        ({"tax_mode": "OTHER"}, "quotes_tax_mode_ck"),
        ({"currency": "pen"}, "quotes_currency_ck"),
        ({"tax_rate": Decimal("1")}, "quotes_tax_rate_ck"),
        ({"total": Decimal("1")}, "quotes_totals_ck"),  # no es neto más IGV
        ({"discount_total": Decimal("-1")}, "quotes_totals_ck"),
        ({"tax_total": Decimal("-1"), "total": Decimal("-1")}, "quotes_totals_ck"),
        ({"subtotal": Decimal("-1"), "total": Decimal("-1")}, "quotes_totals_ck"),
        ({"cancelled_at": NOW}, "quotes_cancelled_ck"),  # una fecha sin estar cancelada
        ({"cancel_reason": "x"}, "quotes_cancelled_ck"),
        ({"status": "CANCELLED"}, "quotes_cancelled_ck"),  # cancelada sin fecha
        ({"version": 0}, "quotes_version_ck"),
    ],
)  # fmt: skip
def test_the_database_refuses_a_bad_quote(
    world: SimpleNamespace, fields: dict[str, Any], constraint: str
) -> None:
    mine = quote(world)
    with tenant_scope(ctx(world.a)), pytest.raises(IntegrityError, match=constraint):
        with transaction.atomic():
            Quote.objects.filter(pk=mine.pk).update(**fields)


@pytest.mark.parametrize(
    ("fields", "constraint"),
    [
        ({"quantity": 0}, "quote_items_quantity_ck"),
        ({"quantity": 1_000_001}, "quote_items_quantity_ck"),
        ({"discount_type": "X"}, "quote_items_discount_ck"),
        ({"list_unit_price": Decimal("0")}, "quote_items_prices_ck"),
        ({"final_unit_price": Decimal("0"), "line_total": Decimal("0")}, "quote_items_prices_ck"),
        ({"discount_amount": Decimal("-1")}, "quote_items_prices_ck"),
        ({"unit_price": Decimal("101")}, "quote_items_prices_ck"),  # el precio solo baja
        ({"final_unit_price": Decimal("101"), "line_total": Decimal("202")}, "quote_items_prices"),
        ({"line_total": Decimal("1")}, "quote_items_total_ck"),  # cantidad por precio final
        (
            {"final_unit_price": Decimal("90"), "line_total": Decimal("200")},  # por el precio
            "quote_items_total_ck",  # unitario, no por el final tras el descuento
        ),
        ({"currency": "pen"}, "quote_items_currency_ck"),
    ],
)  # fmt: skip
def test_the_database_refuses_a_bad_line(
    world: SimpleNamespace, fields: dict[str, Any], constraint: str
) -> None:
    row = line(world, quote(world))
    with tenant_scope(ctx(world.a)), pytest.raises(IntegrityError, match=constraint):
        with transaction.atomic():
            QuoteItem.objects.filter(pk=row.pk).update(**fields)


def test_the_number_is_unique_per_organization_and_revision(world: SimpleNamespace) -> None:
    mine = quote(world)
    other_list = price_list(world.a, f"L{next(_codes)}")
    again = {"number": mine.number, "contact_id": mine.contact_id, "price_list_id": other_list}
    again |= {"currency": "PEN", "tax_rate": Decimal("0.18")}
    with tenant_scope(ctx(world.a)), pytest.raises(IntegrityError, match="quotes_number_uq"):
        with transaction.atomic():
            Quote.objects.create(**again)
    assert quote(world, "b").number == "COT-000001"  # cada organización cuenta lo suyo
    assert quote(world, revision=2, price_list_id=other_list).revision == 2  # otra revisión, sí


def test_the_lines_are_unique_per_variant_and_position_within_a_quote(
    world: SimpleNamespace,
) -> None:
    mine = quote(world)
    first = line(world, mine)
    clone = {f.attname: getattr(first, f.attname) for f in QuoteItem._meta.concrete_fields}
    for change, constraint in (
        ({"variant_id": new_variant(world)}, "quote_items_order_uq"),  # la misma posición
        ({"sort_order": 2}, "quote_items_variant_uq"),  # la misma variante
    ):
        with tenant_scope(ctx(world.a)), pytest.raises(IntegrityError, match=constraint):
            with transaction.atomic():
                QuoteItem.objects.create(**{**clone, "id": uuid4(), **change})
    assert line(world, mine, sort_order=2).sort_order == 2
    other = quote(world, number="COT-000002", price_list_id=mine.price_list_id)
    assert line(world, other, variant_id=first.variant_id).sort_order == 1  # en otra cotización, sí


def test_the_runtime_role_cannot_delete_or_truncate_a_quote_and_never_sees_a_cost(
    world: SimpleNamespace, migrator: DB
) -> None:
    mine = quote(world)
    row = line(world, mine)
    for statement in ("DELETE FROM quotes", "TRUNCATE quotes", "TRUNCATE quote_items"):
        with tenant_scope(ctx(world.a)), connection.cursor() as cursor:
            with pytest.raises(ProgrammingError, match="permission denied"):
                cursor.execute(statement)
    assert sql(world.a, "DELETE FROM quote_items WHERE id = %s", [row.pk]) == [
        (1,)
    ]  # reemplazables
    for model in (Quote, QuoteItem):
        assert not [f.name for f in model._meta.get_fields() if "cost" in f.name.lower()]
    columns = migrator.execute(
        "SELECT column_name FROM information_schema.columns"
        " WHERE table_name IN ('quotes', 'quote_items') AND column_name ILIKE '%cost%'"
    ).fetchall()
    assert columns == []  # el costo de una variante nunca llega a la cotización (ADR-006)


def test_the_migration_depends_on_what_it_uses() -> None:
    """Sin estas dependencias, `migrate quotes` en una base vacía fallaría al crear las FKs."""
    ancestors = MigrationLoader(None).graph.forwards_plan(("quotes", "0001_quotes"))
    for node in (
        ("core", "0001_tenancy_functions"),
        ("organizations", "0010_invitations_are_not_deleted"),
        ("contacts", "0007_consents"),
        ("catalog", "0004_variant_label"),
        ("deals", "0005_opportunity_conversation"),
        ("pricing", "0006_variant_costs"),
    ):
        assert node in ancestors


def test_the_migration_goes_back_and_forward(world: SimpleNamespace, migrator: DB) -> None:
    exists = (
        "SELECT to_regclass('public.quotes') IS NOT NULL,"
        " to_regclass('public.quote_items') IS NOT NULL"
    )
    assert migrator.execute(exists).fetchone() == (True, True)
    with as_migrator():
        call_command("migrate", "quotes", "zero", verbosity=0)
    assert migrator.execute(exists).fetchone() == (False, False)
    with as_migrator():
        call_command("migrate", verbosity=0)
    assert migrator.execute(exists).fetchone() == (True, True)
    assert migrator.execute(
        "SELECT count(*) FROM pg_constraint WHERE conrelid = 'quotes'::regclass AND contype = 'f'"
    ).fetchone() == (6,)
    assert migrator.execute(
        "SELECT count(*) FROM pg_constraint WHERE conrelid = 'quote_items'::regclass"
        " AND contype = 'f'"
    ).fetchone() == (3,)
