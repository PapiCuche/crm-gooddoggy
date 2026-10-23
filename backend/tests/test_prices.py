"""F3-42: la tabla `product_prices`, `set_price` y sus lecturas, contra PostgreSQL real con el rol
`crm_app`, también con dos transacciones a la vez (un hilo y una conexión cada una)."""

import threading
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from django.db import DatabaseError, IntegrityError, connection
from django.utils import timezone

from apps.access.selectors import execution_context
from apps.catalog.models import Product, ProductVariant
from apps.pricing import prices
from apps.pricing.models import PriceList, ProductPrice
from apps.pricing.selectors import next_price, price_at, price_history
from core.tenancy.scope import tenant_scope
from tests import test_authorization
from tests.test_authorization import give
from tests.test_inventory_stock import race
from tests.test_memberships import ctx

world = test_authorization.world
pytestmark = pytest.mark.usefixtures("tenant_db")
DAY, READ = timedelta(days=1), "prices.view"


def variant(org: UUID, sku: str = "IP16", status: str = "ACTIVE") -> UUID:
    with tenant_scope(ctx(org)):
        product = Product.objects.create(name=sku, slug=sku.lower(), product_type="PHYSICAL")
        made = ProductVariant.objects.create(product=product, sku=sku, name=sku, status=status)
    return UUID(str(made.pk))


def price_list(org: UUID, code: str = "REGULAR", **fields: Any) -> UUID:
    wanted = {"name": code, "kind": "RETAIL", "currency": "PEN", "required_permission": READ}
    with tenant_scope(ctx(org)):
        return UUID(str(PriceList.objects.create(code=code, **{**wanted, **fields}).pk))


def pair(world: Any) -> tuple[UUID, UUID]:
    return variant(world.a), price_list(world.a)


def put(world: Any, where: tuple[UUID, UUID], amount: Any, **fields: Any) -> ProductPrice:
    wanted = {"reason": "Ajuste de lista", **fields}
    with tenant_scope(ctx(world.a, world.ana)) as tenant:
        return prices.set_price(
            tenant, variant_id=where[0], price_list_id=where[1], amount=amount, **wanted
        )


def rows(org: UUID) -> list[tuple[Decimal, datetime, datetime | None, UUID | None]]:
    with tenant_scope(ctx(org)):
        found = ProductPrice.objects.order_by("starts_at")
        return list(found.values_list("amount", "starts_at", "ends_at", "previous_price_id"))


def at(world: Any, where: tuple[UUID, UUID], when: datetime) -> Decimal | None:
    with tenant_scope(ctx(world.a)):
        found = price_at(*where, when)
        return found.amount if found else None


def logged(migrator: psycopg.Connection[Any], table: str, columns: str) -> list[tuple[Any, ...]]:
    query = f"SELECT {columns} FROM {table} ORDER BY id"  # noqa: S608 — nombres fijos del test
    return list(migrator.execute(query).fetchall())


def test_4599_then_4399_leaves_two_rows_and_their_history(
    world: Any, migrator: psycopg.Connection[Any]
) -> None:
    """La aceptación de F3-42 en el servicio: la ruta llega en F3-44."""
    where = pair(world)
    first = put(world, where, "4599", reason="Precio de lanzamiento")
    second = put(world, where, Decimal("4399.00"), reason=" Promoción  de octubre ")
    assert (first.currency, first.tax_included) == ("PEN", True)
    assert first.created_by_user_id == world.ana.pk
    assert rows(world.a) == [
        (Decimal("4599.00"), first.starts_at, second.starts_at, None),  # cerrada al cambiar
        (Decimal("4399.00"), second.starts_at, None, first.pk),
    ]
    give(world.a, world.membership, {"prices.view": None})
    with tenant_scope(ctx(world.a, world.ana)) as tenant:
        history = price_history(execution_context(tenant), where[0])
        seen = list(history.values_list("amount", "change_reason", "created_by_user_id"))
    assert seen == [  # el autor por su id: su nombre lo resuelve quien tenga `users.view`
        (Decimal("4399.00"), "Promoción de octubre", world.ana.pk),
        (Decimal("4599.00"), "Precio de lanzamiento", world.ana.pk),
    ]
    columns = "action, entity_id, entity_label, changes, metadata"
    audit = [row for row in logged(migrator, "audit_logs", columns) if row[0].startswith("price.")]
    assert [row[:4] for row in audit] == [
        ("price.changed", first.pk, "IP16 @ REGULAR", {"amount": [None, "4599.00"]}),
        ("price.changed", second.pk, "IP16 @ REGULAR", {"amount": ["4599.00", "4399.00"]}),
    ]
    assert audit[1][4]["reason"] == "Promoción de octubre"
    assert (audit[1][4]["variant_id"], audit[1][4]["price_list_id"]) == tuple(map(str, where))
    events = logged(migrator, "outbox_events", "event_type, aggregate_type, aggregate_id, payload")
    assert [event[:3] for event in events] == [
        ("price.changed", "product_price", first.pk),
        ("price.changed", "product_price", second.pk),
    ]
    assert set(events[1][3]) == {"variant_id", "price_list_id", "starts_at"}  # sin importes


def test_a_price_rules_from_its_start_until_just_before_its_end(world: Any) -> None:
    where = pair(world)
    now = put(world, where, "100").starts_at
    later = timezone.now() + DAY
    scheduled = put(world, where, "120", starts_at=later)
    put(world, where, "125", starts_at=later + DAY)  # el siguiente es el más cercano
    assert scheduled.starts_at == later
    assert at(world, where, now - timedelta(microseconds=1)) is None
    assert at(world, where, now) == Decimal("100")  # el inicio incluido
    assert at(world, where, later - timedelta(microseconds=1)) == Decimal("100")
    assert at(world, where, later) == Decimal("120")  # el fin excluido: ya rige el siguiente
    with tenant_scope(ctx(world.a)):
        assert next_price(*where, now) == scheduled
        assert next_price(*where, later + DAY) is None


def test_a_price_before_a_scheduled_one_ends_where_that_one_starts(
    world: Any, migrator: psycopg.Connection[Any]
) -> None:
    where, base = pair(world), timezone.now()
    current = put(world, where, "100")
    far = put(world, where, "130", starts_at=base + 10 * DAY)
    near = put(world, where, "120", starts_at=base + 5 * DAY)
    assert (near.ends_at, near.previous_price_id) == (far.starts_at, current.pk)
    with pytest.raises(prices.PriceAlreadyScheduled):  # mientras no exista «cancelar»
        put(world, where, "125", starts_at=base + 10 * DAY)
    today = put(world, where, "110")
    assert (today.ends_at, today.previous_price_id) == (near.starts_at, current.pk)
    after = put(world, where, "140", starts_at=base + 20 * DAY)
    assert (after.ends_at, after.previous_price_id) == (None, far.pk)
    assert rows(world.a) == [
        (Decimal("100"), current.starts_at, today.starts_at, None),
        (Decimal("110"), today.starts_at, near.starts_at, current.pk),
        (Decimal("120"), near.starts_at, far.starts_at, current.pk),
        (Decimal("130"), far.starts_at, after.starts_at, current.pk),  # su cambio se conserva
        (Decimal("140"), after.starts_at, None, far.pk),
    ]
    audit = logged(migrator, "audit_logs", "action")
    assert [action for (action,) in audit if action.startswith("price.")] == [
        "price.changed",
        "price.scheduled",
        "price.scheduled",
        "price.changed",
        "price.scheduled",
    ]


def test_no_retroactive_price_and_no_row_without_a_change(
    world: Any, migrator: psycopg.Connection[Any]
) -> None:
    where = pair(world)
    put(world, where, "4599.00")
    later = put(world, where, "4699", starts_at=timezone.now() + 3 * DAY)
    before = (
        rows(world.a),
        logged(migrator, "audit_logs", "id"),
        logged(migrator, "outbox_events", "id"),
    )
    for amount, fields in (
        ("4599", {}),  # el mismo importe que ya rige
        ("4699.0", {"starts_at": later.starts_at + DAY}),  # el que regirá en ese instante
    ):
        with pytest.raises(prices.PriceUnchanged):
            put(world, where, amount, **fields)
    with pytest.raises(prices.RetroactivePrice):
        put(world, where, "4000", starts_at=timezone.now() - timedelta(seconds=1))
    with pytest.raises(ValueError, match="zona horaria"):
        put(world, where, "4000", starts_at=datetime(2030, 1, 1))  # noqa: DTZ001
    after = (
        rows(world.a),
        logged(migrator, "audit_logs", "id"),
        logged(migrator, "outbox_events", "id"),
    )
    assert after == before


def test_the_input_and_what_is_priced(world: Any) -> None:
    where = pair(world)
    for amount in ("0", "0.00", "4399.999", "-1", "1e3", "١٢", "1234567890123", ""):
        with pytest.raises(ValueError):
            put(world, where, amount)
    with pytest.raises(TypeError):
        put(world, where, 4399.0)
    for reason in ("ab", "  ", "<b>Promo</b>", "Promo<!-- x -->", "dos\nlíneas", "x" * 501):
        with pytest.raises(ValueError):
            put(world, where, "10", reason=reason)
    assert put(world, where, "10", reason="Precio < costo").change_reason == "Precio < costo"
    other = variant(world.a, "IP15")
    with tenant_scope(ctx(world.a)):
        ProductVariant.objects.filter(pk=other).update(deleted_at=timezone.now())
    with pytest.raises(ProductVariant.DoesNotExist):
        put(world, (other, where[1]), "10")
    for change in ({"is_active": False}, {"deleted_at": timezone.now()}):
        off = price_list(world.a, f"L{uuid4().hex[:6].upper()}")
        with tenant_scope(ctx(world.a)):
            PriceList.objects.filter(pk=off).update(**change)
        with pytest.raises(PriceList.DoesNotExist):
            put(world, (where[0], off), "10")
    for status in ("INACTIVE", "DISCONTINUED"):  # sí acepta precio: el motor no la ofrece
        assert put(world, (variant(world.a, status), where[1]), "10").amount == Decimal("10")
    dollars = price_list(world.a, "DOLARES", currency="USD")  # la moneda es la de su lista
    assert put(world, (where[0], dollars), "10").currency == "USD"
    assert len(rows(world.a)) == 4


def test_the_runtime_only_closes_a_validity(world: Any) -> None:
    """[INM]: con el rol real (`crm_app`), sin `DELETE` ni `UPDATE` salvo de `ends_at`."""
    where = pair(world)
    price = put(world, where, "100")
    with tenant_scope(ctx(world.a)), connection.cursor() as cursor:
        cursor.execute("SELECT current_user")
        assert cursor.fetchone() == ("crm_app",)
    for change in (
        "UPDATE product_prices SET amount = 1",
        "UPDATE product_prices SET change_reason = 'otro'",
        "UPDATE product_prices SET starts_at = starts_at - interval '1 day'",
        "DELETE FROM product_prices",
        "TRUNCATE product_prices",
    ):
        with pytest.raises(DatabaseError, match="permission denied"), tenant_scope(ctx(world.a)):
            connection.cursor().execute(change)
    closed = price.starts_at + DAY
    with tenant_scope(ctx(world.a)):
        assert ProductPrice.objects.filter(pk=price.pk).update(ends_at=closed) == 1
    assert rows(world.a) == [(Decimal("100"), price.starts_at, closed, None)]


def test_a_validity_only_closes_forwards(world: Any) -> None:
    """El trigger [INM], con `crm_app`: no reabre, no adelanta al pasado ni toca lo terminado."""
    where = pair(world)
    ended = put(world, where, "100")
    ruling = put(world, where, "110")
    now = timezone.now()

    def close(pk: UUID, ends_at: datetime | None) -> None:
        with tenant_scope(ctx(world.a)), connection.cursor() as cursor:
            cursor.execute("UPDATE product_prices SET ends_at = %s WHERE id = %s", [ends_at, pk])

    for pk, ends_at in (
        (ended.pk, None),  # reabrir uno que ya terminó
        (ended.pk, now + 9 * DAY),  # moverle el fin, aunque sea hacia delante
        (ruling.pk, now - timedelta(seconds=1)),  # cerrar en el pasado
    ):
        with pytest.raises(IntegrityError, match="product_prices_immutable"):
            close(pk, ends_at)
    close(ruling.pk, None)  # sin cambio
    close(ruling.pk, now + 2 * DAY)  # cerrarlo en el futuro
    close(ruling.pk, now + DAY)  # y acortarlo, mientras no termine
    with pytest.raises(IntegrityError, match="product_prices_immutable"):
        close(ruling.pk, None)  # cerrado no se reabre
    assert [ends for _, _, ends, _ in rows(world.a)] == [ruling.starts_at, now + DAY]


def test_what_the_database_refuses_even_without_the_service(world: Any) -> None:
    where = pair(world)
    put(world, where, "100")
    start = timezone.now() + DAY
    base = {"variant_id": where[0], "price_list_id": where[1], "amount": Decimal("5")}
    base |= {"currency": "PEN", "starts_at": start, "change_reason": "manual"}
    foreign_list = price_list(world.b)
    with tenant_scope(ctx(world.b)):
        foreign_price = ProductPrice.objects.create(
            **{**base, "variant_id": variant(world.b), "price_list_id": foreign_list}
        )
    past = {"starts_at": start - 9 * DAY, "ends_at": start - 8 * DAY}  # antes del vigente
    for broken, name in (
        ({"starts_at": start}, "product_prices_no_overlap_ex"),  # se solapa con el vigente
        ({**past, "amount": Decimal("0")}, "product_prices_amount_ck"),
        ({**past, "ends_at": past["starts_at"]}, "product_prices_validity_ck"),
        ({**past, "currency": "USD"}, "product_prices_price_list_fk"),  # no es la de su lista
        ({**past, "price_list_id": foreign_list}, "product_prices_price_list_fk"),
        ({**past, "variant_id": variant(world.b, "AJENA")}, "product_prices_variant_fk"),
        ({**past, "previous_price_id": foreign_price.pk}, "product_prices_previous_fk"),
        ({**past, "created_by_user_id": uuid4()}, "product_prices_created_by_fk"),
    ):
        with pytest.raises(IntegrityError, match=name), tenant_scope(ctx(world.a)):
            ProductPrice.objects.create(**{**base, **broken})
    assert len(rows(world.a)) == 1


def test_each_organization_sees_and_prices_only_its_own(world: Any) -> None:
    where = pair(world)
    put(world, where, "100")
    give(world.a, world.membership, {"prices.manage": None})
    with tenant_scope(ctx(world.b)):
        assert ProductPrice.objects.count() == 0
        assert price_at(*where, timezone.now()) is None
    with pytest.raises(PriceList.DoesNotExist):  # la lista de otra organización
        put(world, (where[0], price_list(world.b)), "10")
    with pytest.raises(ProductVariant.DoesNotExist):
        put(world, (variant(world.b), where[1]), "10")
    assert len(rows(world.a)) == 1


def test_history_hides_the_lists_the_actor_does_not_see(world: Any) -> None:
    sku = variant(world.a)
    regular, wholesale = (
        price_list(world.a),
        price_list(world.a, "MAYORISTA", required_permission="prices.view_wholesale"),
    )
    put(world, (sku, regular), "100")
    put(world, (sku, wholesale), "90")
    give(world.a, world.membership, {"prices.view": None})
    with tenant_scope(ctx(world.a, world.ana)) as tenant:
        seen = list(price_history(execution_context(tenant), sku).values_list("price_list_id"))
    assert seen == [(regular,)]


def test_two_changes_of_the_same_pair_at_once_go_one_after_the_other(world: Any) -> None:
    where = pair(world)
    current = put(world, where, "100")

    def change(amount: str) -> Any:
        return lambda tenant: prices.set_price(
            tenant, variant_id=where[0], price_list_id=where[1], amount=amount, reason="A la vez"
        )

    first, second = race(world.a, world.ana, [change("110"), change("120")])
    assert first.starts_at < second.starts_at  # "ahora" se lee con el bloqueo ya tomado
    assert (first.previous_price_id, second.previous_price_id) == (current.pk, first.pk)
    assert [amount for amount, *_ in rows(world.a)] == [Decimal(a) for a in ("100", "110", "120")]
    assert at(world, where, timezone.now()) == Decimal("120")


def test_an_overlap_that_reaches_the_database_is_a_conflict(world: Any) -> None:
    """La `EXCLUDE` es la última barrera: una fila escrita por fuera del servicio (sin su
    bloqueo) y aún sin confirmar hace esperar al servicio, que al confirmarse da `PriceConflict`
    en lugar de un 500."""
    where = pair(world)
    sql = (
        "INSERT INTO product_prices (id, organization_id, variant_id, price_list_id, amount, "
        "currency, tax_included, starts_at, change_reason, created_at) "
        "VALUES (%s, %s, %s, %s, 50, 'PEN', true, now(), 'por fuera', now())"
    )

    def outside(tenant: Any) -> Any:
        return connection.cursor().execute(sql, [uuid4(), world.a, *where])

    def service(tenant: Any) -> Any:
        return prices.set_price(
            tenant, variant_id=where[0], price_list_id=where[1], amount="60", reason="A la vez"
        )

    outcome = race(world.a, world.ana, [outside, service])
    assert outcome[1] == "PriceConflict"
    assert [amount for amount, *_ in rows(world.a)] == [Decimal("50")]


def in_thread_a(world: Any, where: tuple[UUID, UUID], before: Any) -> tuple[Any, list[Any]]:
    """Fija 110 en un hilo «A», que llama a `before` dentro de su transacción antes del servicio."""
    outcome = list[Any]([None])

    def run() -> None:
        try:
            with tenant_scope(ctx(world.a, world.ana)) as tenant:
                before()
                outcome[0] = prices.set_price(
                    tenant,
                    variant_id=where[0],
                    price_list_id=where[1],
                    amount="110",
                    reason="Tarde",
                )
        except Exception as error:  # noqa: BLE001 — lo comprueba cada test
            outcome[0] = error
        finally:
            connection.close()

    thread = threading.Thread(target=run, name="A")
    thread.start()
    return thread, outcome


def test_a_change_that_began_earlier_applies_after_the_one_that_locked_first(world: Any) -> None:
    """Revisión de F3-42: la transacción de A empieza antes, pero B toma el bloqueo y confirma
    primero. A rige desde que obtiene el bloqueo, después de B: no reescribe lo que ya rigió."""
    where = pair(world)
    first = put(world, where, "100")
    begun, go = threading.Event(), threading.Event()

    def hold() -> None:  # A ya está en su transacción: B fija el suyo y confirma antes
        begun.set()
        go.wait(timeout=10)

    thread, outcome = in_thread_a(world, where, hold)
    assert begun.wait(timeout=10)
    second = put(world, where, "120")
    go.set()
    thread.join(timeout=10)
    third = outcome[0]
    assert isinstance(third, ProductPrice), third
    assert rows(world.a) == [
        (Decimal("100"), first.starts_at, second.starts_at, None),
        (Decimal("120"), second.starts_at, third.starts_at, first.pk),
        (Decimal("110"), third.starts_at, None, second.pk),
    ]


def test_now_is_read_with_the_lock_held(world: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Si A leyera «ahora» antes del bloqueo, B podría fijar el suyo entre la lectura y el bloqueo
    de A, y A se colaría debajo. A se detiene justo después de leerlo: con el bloqueo ya tomado,
    B espera a que A confirme y la cadena sigue el orden del bloqueo."""
    where = pair(world)
    first = put(world, where, "100")
    read, go, original = threading.Event(), threading.Event(), prices._now

    def paused(alias: str) -> datetime:
        now = original(alias)
        if threading.current_thread().name == "A":
            read.set()
            go.wait(timeout=1)  # con el bloqueo, B no puede avanzar: se sigue tras el plazo
        return now

    monkeypatch.setattr(prices, "_now", paused)
    thread, outcome = in_thread_a(world, where, lambda: None)
    assert read.wait(timeout=10)
    second = put(world, where, "120")
    go.set()
    thread.join(timeout=10)
    late = outcome[0]
    assert isinstance(late, ProductPrice), late
    assert rows(world.a) == [
        (Decimal("100"), first.starts_at, late.starts_at, None),
        (Decimal("110"), late.starts_at, second.starts_at, first.pk),
        (Decimal("120"), second.starts_at, None, late.pk),
    ]
