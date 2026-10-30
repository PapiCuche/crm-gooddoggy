"""F3-46: la tabla `variant_costs` y `CostService`, contra PostgreSQL real con el rol `crm_app`."""

import ast
import pathlib
import tomllib
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from django.db import DatabaseError, IntegrityError, connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.access.catalog import PERMISSIONS
from apps.catalog.models import ProductVariant
from apps.pricing import costs
from apps.pricing.models import VariantCost
from core.tenancy.scope import tenant_scope
from tests import test_authorization
from tests.test_inventory_stock import race
from tests.test_memberships import ctx
from tests.test_prices import variant

world = test_authorization.world
pytestmark = pytest.mark.usefixtures("tenant_db")
DAY = timedelta(days=1)
BACKEND = pathlib.Path(__file__).resolve().parents[1]


def put(world: Any, sku: UUID, amount: Any = "10", **fields: Any) -> VariantCost:
    wanted = {"reason": "Compra nueva", **fields}
    with tenant_scope(ctx(world.a, world.ana)) as tenant:
        return costs.set_cost(tenant, variant_id=sku, amount=amount, **wanted)


def rows(org: UUID) -> list[tuple[Decimal, datetime, datetime | None]]:
    with tenant_scope(ctx(org)):
        found = VariantCost.objects.order_by("starts_at")
        return list(found.values_list("amount", "starts_at", "ends_at"))


def audit(migrator: psycopg.Connection[Any]) -> list[tuple[Any, ...]]:
    query = (
        "SELECT action, changes, metadata FROM audit_logs WHERE action LIKE 'cost.%' ORDER BY id"
    )
    return list(migrator.execute(query).fetchall())


def test_set_cost_closes_the_ruling_one_and_audits_previous_new_and_reason(
    world: Any, migrator: psycopg.Connection[Any]
) -> None:
    sku = variant(world.a)
    first = put(world, sku, "30", reason="Costo de lanzamiento")
    second = put(world, sku, Decimal("32.50"), reason=" Subió  el proveedor ")
    assert (first.currency, first.tax_included, first.source) == ("PEN", True, "MANUAL")
    assert first.created_by_user_id == world.ana.pk
    assert rows(world.a) == [
        (Decimal("30.00"), first.starts_at, second.starts_at),
        (Decimal("32.50"), second.starts_at, None),
    ]
    seen = audit(migrator)
    assert [(a, c) for a, c, _ in seen] == [
        ("cost.changed", {"amount": [None, "30.00"]}),
        ("cost.changed", {"amount": ["30.00", "32.50"]}),
    ]
    assert seen[1][2]["reason"] == "Subió el proveedor" and seen[1][2]["variant_id"] == str(sku)
    assert migrator.execute("SELECT count(*) FROM outbox_events").fetchone() == (0,)  # sin eventos


def test_no_retroactive_cost_no_row_without_a_change_and_the_input(
    world: Any, migrator: psycopg.Connection[Any]
) -> None:
    sku = variant(world.a)
    put(world, sku, "30")
    before = (rows(world.a), audit(migrator))
    for amount, fields in (("30", {}), ("30.0", {"currency": "PEN"})):
        with pytest.raises(costs.CostUnchanged):
            put(world, sku, amount, **fields)
    for amount in ("0", "0.00", "10.999", "-1", "1e3", "", "١٢"):
        with pytest.raises(ValueError):
            put(world, sku, amount)
    with pytest.raises(TypeError):
        put(world, sku, 30.0)
    for reason in ("ab", "  ", "<b>x</b>", "x<!-- y -->", "dos\nlíneas", "x" * 501):
        with pytest.raises(ValueError):
            put(world, sku, "11", reason=reason)
    for currency in ("pen", "PE", "SOLES"):
        with pytest.raises(ValueError):
            put(world, sku, "11", currency=currency)
    assert put(world, sku, "11", reason="Costo < precio").change_reason == "Costo < precio"
    assert put(world, sku, "11", currency="USD").currency == "USD"  # otra moneda sí es un cambio
    assert rows(world.a)[0][:2] == before[0][0][:2]  # lo rechazado no tocó el costo que regía
    assert len(rows(world.a)) == 3 and audit(migrator)[:1] == before[1]


def test_a_deleted_variant_has_no_cost_and_nothing_is_written(world: Any) -> None:
    sku = variant(world.a)
    with tenant_scope(ctx(world.a)):
        ProductVariant.objects.filter(pk=sku).update(deleted_at=timezone.now())
    with pytest.raises(ProductVariant.DoesNotExist):
        put(world, sku)
    with pytest.raises(ProductVariant.DoesNotExist):  # la de otra organización
        put(world, variant(world.b, "AJENA"))
    assert rows(world.a) == []


def test_a_rollback_leaves_neither_the_cost_nor_the_audit_row(
    world: Any, migrator: psycopg.Connection[Any]
) -> None:
    sku = variant(world.a)
    put(world, sku, "30")
    with pytest.raises(RuntimeError), tenant_scope(ctx(world.a, world.ana)) as tenant:
        costs.set_cost(tenant, variant_id=sku, amount="40", reason="Se deshace")
        raise RuntimeError
    assert [amount for amount, *_ in rows(world.a)] == [Decimal("30.00")]
    assert len(audit(migrator)) == 1


def test_current_costs_uses_a_constant_number_of_queries(world: Any) -> None:
    skus = [variant(world.a, f"SKU{i}") for i in range(50)]
    for sku in skus[:30]:
        put(world, sku, "5")
    put(world, skus[0], "6")
    now = timezone.now()
    base = {"amount": 7, "currency": "PEN", "source": "MANUAL", "change_reason": "por fuera"}
    with tenant_scope(ctx(world.a)):  # uno futuro y uno terminado: ninguno rige
        VariantCost.objects.create(variant_id=skus[40], starts_at=now + DAY, **base)
        VariantCost.objects.create(
            variant_id=skus[41], starts_at=now - 3 * DAY, ends_at=now - DAY, **base
        )

    def count(ids: list[UUID]) -> tuple[int, dict[UUID, VariantCost]]:
        with tenant_scope(ctx(world.a)) as tenant, CaptureQueriesContext(connection) as captured:
            found = costs.current_costs(tenant, ids)
        return len(captured), found

    one, found_one = count(skus[:1])
    many, found_many = count(skus)
    assert one == many == 1
    assert found_one[skus[0]].amount == Decimal("6.00")
    assert len(found_many) == 30 and skus[40] not in found_many  # sin costo: no aparece
    with tenant_scope(ctx(world.b)) as tenant:
        assert costs.current_costs(tenant, skus) == {}  # RLS: otra organización no ve nada
        assert list(costs.cost_history(tenant, skus[0])) == []
    with tenant_scope(ctx(world.a)) as tenant:
        history = list(costs.cost_history(tenant, skus[0]))
    assert [cost.amount for cost in history] == [Decimal("6.00"), Decimal("5.00")]


def test_the_runtime_only_closes_a_validity(world: Any) -> None:
    """[INM], con el rol real: sin `DELETE`, `TRUNCATE` ni `UPDATE` salvo de `ends_at`."""
    cost = put(world, variant(world.a))
    with tenant_scope(ctx(world.a)), connection.cursor() as cursor:
        cursor.execute("SELECT current_user")
        assert cursor.fetchone() == ("crm_app",)
    for change in (
        "UPDATE variant_costs SET amount = 1",
        "UPDATE variant_costs SET change_reason = 'otro'",
        "UPDATE variant_costs SET starts_at = starts_at - interval '1 day'",
        "UPDATE variant_costs SET source = 'IMPORT'",
        "DELETE FROM variant_costs",
        "TRUNCATE variant_costs",
    ):
        with pytest.raises(DatabaseError, match="permission denied"), tenant_scope(ctx(world.a)):
            connection.cursor().execute(change)
    closed = cost.starts_at + DAY
    with tenant_scope(ctx(world.a)):
        assert VariantCost.objects.filter(pk=cost.pk).update(ends_at=closed) == 1


def test_a_validity_only_closes_forwards(world: Any, migrator: psycopg.Connection[Any]) -> None:
    """El trigger [INM] no reabre, no adelanta al pasado ni toca lo terminado (ni el migrador)."""
    sku = variant(world.a)
    ended, ruling = put(world, sku, "10"), put(world, sku, "11")
    now = timezone.now()

    def close(pk: UUID, ends_at: datetime | None) -> None:
        with tenant_scope(ctx(world.a)), connection.cursor() as cursor:
            cursor.execute("UPDATE variant_costs SET ends_at = %s WHERE id = %s", [ends_at, pk])

    for pk, ends_at in (
        (ended.pk, None),  # reabrir uno que ya terminó
        (ended.pk, now + 9 * DAY),  # moverle el fin, aunque sea hacia delante
        (ruling.pk, now - timedelta(seconds=1)),  # cerrar en el pasado
    ):
        with pytest.raises(IntegrityError, match="variant_costs_immutable"):
            close(pk, ends_at)
    with pytest.raises(psycopg.errors.CheckViolation, match="variant_costs_immutable"):
        migrator.execute("UPDATE variant_costs SET ends_at = NULL WHERE id = %s", [ended.pk])
    close(ruling.pk, None)  # sin cambio
    close(ruling.pk, now + 2 * DAY)  # cerrarlo en el futuro
    close(ruling.pk, now + DAY)  # y acortarlo, mientras no termine
    with pytest.raises(IntegrityError, match="variant_costs_immutable"):
        close(ruling.pk, None)  # cerrado no se reabre
    assert [ends for _, _, ends in rows(world.a)] == [ruling.starts_at, now + DAY]


def test_what_the_database_refuses_even_without_the_service(world: Any) -> None:
    sku = variant(world.a)
    put(world, sku)
    start = timezone.now() + DAY
    base = {"variant_id": sku, "amount": Decimal("5"), "currency": "PEN", "source": "MANUAL"}
    base |= {"starts_at": start - 9 * DAY, "ends_at": start - 8 * DAY, "change_reason": "manual"}
    for broken, name in (
        ({"starts_at": start, "ends_at": None}, "variant_costs_no_overlap_ex"),  # solapa al vigente
        ({"amount": Decimal("0")}, "variant_costs_amount_ck"),
        ({"ends_at": base["starts_at"]}, "variant_costs_validity_ck"),
        ({"source": "OTRO"}, "variant_costs_source_ck"),
        ({"currency": "pen"}, "variant_costs_currency_ck"),
        ({"variant_id": variant(world.b, "AJENA")}, "variant_costs_variant_fk"),
        ({"created_by_user_id": uuid4()}, "variant_costs_created_by_fk"),
    ):
        with pytest.raises(IntegrityError, match=name), tenant_scope(ctx(world.a)):
            VariantCost.objects.create(**{**base, **broken})
    for index, source in enumerate(("IMPORT", "PURCHASE")):  # reservados, pero la base los admite
        window = {"starts_at": start - (index + 6) * DAY, "ends_at": start - (index + 5) * DAY}
        with tenant_scope(ctx(world.a)):
            VariantCost.objects.create(**{**base, **window, "source": source})
    assert len(rows(world.a)) == 3


def test_each_organization_sees_and_costs_only_its_own(world: Any) -> None:
    put(world, variant(world.a))
    with tenant_scope(ctx(world.b)):
        assert VariantCost.objects.count() == 0


def test_two_changes_of_the_same_variant_at_once_go_one_after_the_other(world: Any) -> None:
    sku = variant(world.a)
    current = put(world, sku, "10")

    def change(amount: str) -> Any:
        return lambda tenant: costs.set_cost(
            tenant, variant_id=sku, amount=amount, reason="A la vez"
        )

    first, second = race(world.a, world.ana, [change("11"), change("12")])
    assert current.starts_at < first.starts_at < second.starts_at
    assert [amount for amount, *_ in rows(world.a)] == [Decimal(a) for a in ("10", "11", "12")]
    same = race(world.a, world.ana, [change("13"), change("13")])  # el segundo ya no cambia nada
    assert (same[0].amount, same[1]) == (Decimal("13"), "CostUnchanged")
    assert len(rows(world.a)) == 4 and [e for *_, e in rows(world.a)][-1] is None


def test_an_overlap_that_reaches_the_database_is_a_conflict(world: Any) -> None:
    """Una fila escrita por fuera del servicio y sin confirmar hace esperar a la `EXCLUDE`, que al
    confirmarse da `CostConflict` y no un 500."""
    sku = variant(world.a)
    sql = (
        "INSERT INTO variant_costs (id, organization_id, variant_id, amount, currency, "
        "tax_included, starts_at, source, change_reason, created_at) "
        "VALUES (%s, %s, %s, 50, 'PEN', true, now(), 'MANUAL', 'por fuera', now())"
    )

    def outside(tenant: Any) -> Any:
        return connection.cursor().execute(sql, [uuid4(), world.a, sku])

    def service(tenant: Any) -> Any:
        return costs.set_cost(tenant, variant_id=sku, amount="60", reason="A la vez")

    outcome = race(world.a, world.ana, [outside, service])
    assert outcome[1] == "CostConflict"
    assert [amount for amount, *_ in rows(world.a)] == [Decimal("50")]


def test_the_sensitive_permissions_exist_in_the_catalog() -> None:
    sensitive = {p.code: p.is_sensitive for p in PERMISSIONS if p.code.startswith("product_cost.")}
    assert sensitive == {"product_cost.view": True, "product_cost.manage": True}


def test_only_costs_touches_variant_costs() -> None:
    """Ni el motor ni ningún otro módulo nombra el modelo ni la tabla: solo `costs` (y su
    definición y sus migraciones)."""
    allowed = {"apps/pricing/costs.py", "apps/pricing/models.py"}
    found: list[str] = []
    for path in (BACKEND / "apps").rglob("*.py"):
        name = str(path.relative_to(BACKEND))
        if "/migrations/" in name or name in allowed:
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            text = node.value if isinstance(node, ast.Constant) else getattr(node, "id", "")
            names = [alias.name for alias in getattr(node, "names", [])]
            if "VariantCost" in names or "variant_costs" in str(text) or text == "VariantCost":
                found.append(name)
    assert found == []


def test_the_import_contracts_keep_costs_away_from_the_engine() -> None:
    config = tomllib.loads((BACKEND / "pyproject.toml").read_text())
    contracts = {c["name"]: c for c in config["tool"]["importlinter"]["contracts"]}
    protected = [
        c for c in contracts.values() if "apps.pricing.costs" in c.get("protected_modules", [])
    ]
    assert [(c["type"], c["allowed_importers"]) for c in protected] == [
        ("protected", ["apps.pricing.api"])
    ]
    engine = [
        c for c in contracts.values() if "apps.pricing.costs" in c.get("forbidden_modules", [])
    ]
    assert [(c["type"], sorted(c["source_modules"])) for c in engine] == [
        ("forbidden", ["apps.pricing.selectors", "apps.pricing.services"])
    ]
