"""F5-09a (E13-01): las tablas `quotes` y `quote_items` y sus comandos, con PostgreSQL real y el
rol `crm_app`, también con transacciones a la vez. Sin API: llega con F5-09d y F5-09e."""

import json
import threading
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from functools import partial
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from django.db import connection
from django.utils import timezone

from apps.contacts.commands import delete_contact
from apps.contacts.models import Contact
from apps.deals.models import Opportunity
from apps.deals.opportunities import delete_opportunity
from apps.quotes import commands
from apps.quotes.commands import BadReference, QuoteNotDraft, QuoteTerminal, StaleVersion
from apps.quotes.models import Quote, QuoteItem
from core.tenancy.scope import tenant_scope
from tests import test_opportunities_tables as tables
from tests.test_inventory_stock import race
from tests.test_memberships import ctx
from tests.test_opportunity_commands import change
from tests.test_prices import price_list
from tests.test_pricing_engine import item, person, row
from tests.test_promotion_tables import promo

world = tables.world
pytestmark = pytest.mark.usefixtures("tenant_db")
type DB = psycopg.Connection[Any]
NOW = timezone.now()
LONG_AGO = NOW - timedelta(days=90)
SECRET = "Cliente Juan Perez, DNI 12345678"  # noqa: S105 — texto de prueba


@pytest.fixture
def shop(world: SimpleNamespace) -> SimpleNamespace:
    """Quien cotiza (con `prices.view`), la lista predeterminada y dos variantes con precio."""
    quoter = person(world, {"prices.view": None})
    listed = price_list(world.a, is_default=True)
    iphone, case = item(world.a, "IP16"), item(world.a, "CASE")
    row(world.a, (iphone, listed), "4399", LONG_AGO)
    row(world.a, (case, listed), "100.50", LONG_AGO)
    return SimpleNamespace(**vars(world), quoter=quoter, listed=listed, iphone=iphone, case=case)


def lines(*pairs: tuple[Any, int]) -> list[dict[str, Any]]:
    return [{"variant_id": variant, "quantity": quantity} for variant, quantity in pairs]


def run(shop: SimpleNamespace, command: Any, org: str = "a", **fields: Any) -> Any:
    with tenant_scope(ctx(getattr(shop, org), shop.quoter if org == "a" else None)) as tenant:
        return command(tenant, **fields)


def new(shop: SimpleNamespace, **fields: Any) -> Quote:
    wanted = {"contact_id": tables.contact(shop.a).pk, "items": lines((shop.iphone, 1))}
    return run(shop, commands.create_quote, **{**wanted, **fields})  # type: ignore[no-any-return]


def move(shop: SimpleNamespace, command: Any, quote: Any, version: Any = 1, **fields: Any) -> Quote:
    return run(shop, command, quote_id=quote.pk, version=version, **fields)  # type: ignore[no-any-return]


def stored(org: Any, quote: Quote) -> list[QuoteItem]:
    with tenant_scope(ctx(org)):
        return list(QuoteItem.objects.filter(quote_id=quote.pk).order_by("sort_order"))


def audit(migrator: DB) -> list[tuple[str, dict[str, Any]]]:
    found = migrator.execute(
        "SELECT action, changes::text FROM audit_logs WHERE entity_type = 'quote'"
        " ORDER BY occurred_at, id"
    ).fetchall()
    return [(action, json.loads(changes)) for action, changes in found]


def test_create_opens_a_numbered_draft_with_priced_lines_and_audits_without_texts(
    shop: SimpleNamespace, migrator: DB
) -> None:
    contact = tables.contact(shop.a).pk
    made = new(shop, contact_id=contact, items=lines((shop.iphone, 2), (shop.case, 3)),
               notes_to_customer=SECRET, internal_notes=f" {SECRET}\n\nOtra \n\n ")  # fmt: skip
    assert (made.number, made.revision, made.status, made.version) == ("COT-000001", 1, "DRAFT", 1)
    assert (made.contact_id, made.opportunity_id, made.price_list_id) == (
        contact,
        None,
        shop.listed,
    )
    assert (made.currency, made.tax_mode, made.tax_rate) == ("PEN", "INCLUDED", Decimal("0.18"))
    assert made.created_by_user_id == made.updated_by_user_id == shop.quoter.pk
    assert (made.notes_to_customer, made.internal_notes) == (SECRET, f"{SECRET}\n\nOtra")
    # 2 x 4399.00 + 3 x 100.50 = 9099.50; el IGV que lleva: 9099.50 - 9099.50 / 1.18
    assert (made.total, made.tax_total, made.subtotal) == (
        Decimal("9099.50"), Decimal("1388.06"), Decimal("7711.44"),
    )  # fmt: skip
    assert made.discount_total == Decimal("0.00")
    first, second = stored(shop.a, made)
    assert (first.sort_order, first.variant_id, first.quantity) == (1, shop.iphone, 2)
    assert (first.product_name_snapshot, first.sku_snapshot) == ("IP16", "IP16")
    assert (first.list_unit_price, first.final_unit_price, first.line_total) == (
        Decimal("4399.00"), Decimal("4399.00"), Decimal("8798.00"),
    )  # fmt: skip
    assert (second.line_total, second.currency, second.promotion_id_snapshot) == (
        Decimal("301.50"), "PEN", None,
    )  # fmt: skip
    [(action, changes)] = audit(migrator)
    assert action == "quote.created" and changes["status"] == [None, "DRAFT"]
    assert changes["notes_to_customer"] == [None, "[texto]"] and changes["total"][1] == "9099.50"
    assert "12345678" not in json.dumps(changes) and "IP16" not in json.dumps(changes)
    assert new(shop).number == "COT-000002"


def test_a_promotion_lowers_the_final_price_and_is_kept_in_the_snapshot(
    shop: SimpleNamespace,
) -> None:
    made_promo = promo(shop.a, [shop.listed], [{"variant_id": shop.iphone}],
                       starts=NOW - timedelta(days=1), ends=NOW + timedelta(days=1))  # fmt: skip
    made = new(shop, items=lines((shop.iphone, 2), (shop.case, 1)))
    first, second = stored(shop.a, made)
    assert (first.list_unit_price, first.final_unit_price) == (
        Decimal("4399.00"),
        Decimal("3959.10"),
    )
    assert (first.promotion_id_snapshot, first.promotion_name_snapshot) == (made_promo, "Promo")
    assert second.promotion_id_snapshot is None
    assert made.discount_total == Decimal("879.80")  # lo que se ahorra frente a la lista
    assert made.total == Decimal("8018.70")


def test_the_links_take_the_contact_of_the_opportunity_and_both_must_agree(
    shop: SimpleNamespace,
) -> None:
    opp = tables.make(shop.a)
    by_opportunity = new(shop, contact_id=None, opportunity_id=opp.pk)
    assert (by_opportunity.opportunity_id, by_opportunity.contact_id) == (opp.pk, opp.contact_id)
    assert new(shop, opportunity_id=opp.pk, contact_id=opp.contact_id).contact_id == opp.contact_id
    with pytest.raises(BadReference) as bad:
        new(shop, opportunity_id=opp.pk, contact_id=tables.contact(shop.a).pk)
    assert bad.value.field == "opportunity_id"
    with pytest.raises(ValueError):  # noqa: PT011 — el motivo lo dice el comando
        new(shop, contact_id=None)


def unseen_list(org: Any) -> Any:
    return price_list(org, "INTERNA", required_permission="prices.view_internal")


@pytest.mark.parametrize(
    ("field", "make_bad"),
    [
        ("contact_id", lambda s: tables.contact(s.b).pk),  # de otra organización
        ("contact_id", lambda s: change(s, Contact, tables.contact(s.a).pk, deleted_at=NOW)),
        ("contact_id", lambda s: uuid4()),
        ("opportunity_id", lambda s: tables.make(s.b).pk),
        ("opportunity_id", lambda s: change(s, Opportunity, tables.make(s.a).pk, deleted_at=NOW)),
        ("price_list_id", lambda s: price_list(s.b, "OTRA")),
        ("price_list_id", lambda s: uuid4()),
        ("price_list_id", lambda s: unseen_list(s.a)),  # quien actúa no la ve
        ("variant_id", lambda s: item(s.b, "AJENA")),
        ("variant_id", lambda s: item(s.a, "SINPRECIO")),  # sin precio vigente en la lista
        ("variant_id", lambda s: uuid4()),
    ],
)  # fmt: skip
def test_a_reference_that_does_not_fit_writes_nothing(
    shop: SimpleNamespace, migrator: DB, field: str, make_bad: Any
) -> None:
    bad = make_bad(shop)
    fields = {"items": lines((bad, 1))} if field == "variant_id" else {field: bad}
    with pytest.raises(BadReference) as refused:
        new(shop, **fields)
    assert refused.value.field == field
    count = migrator.execute("SELECT count(*), (SELECT count(*) FROM quote_items) FROM quotes")
    assert count.fetchone() == (0, 0) and audit(migrator) == []
    assert new(shop).number == "COT-000001"  # un alta fallida no gasta su número


def test_without_prices_view_the_price_list_does_not_exist(shop: SimpleNamespace) -> None:
    shop.quoter = person(shop, {"products.view": None})
    with pytest.raises(BadReference) as refused:
        new(shop)
    assert refused.value.field == "price_list_id"


@pytest.mark.parametrize(
    "bad",
    [
        {"items": []}, {"items": "x"}, {"items": None}, {"items": [5]}, {"items": [{}]},
        {"items": lines(("x", 1))}, {"items": [{"variant_id": uuid4(), "quantity": 1, "x": 1}]},
        {"items": [{"variant_id": uuid4(), "quantity": 0}]},
        {"items": [{"variant_id": uuid4(), "quantity": True}]},
        {"items": [{"variant_id": uuid4(), "quantity": 1.5}]},
        {"items": [{"variant_id": uuid4(), "quantity": 10**9}]},
        {"items": lines(*[(uuid4(), 1)] * 2)},  # variante repetida
        {"items": lines(*[(uuid4(), 1) for _ in range(51)])},
        {"valid_until": datetime(2026, 11, 1)}, {"valid_until": "2026-11-01"},
        {"valid_until": datetime(3000, 1, 1, tzinfo=UTC)},
        {"notes_to_customer": 5}, {"notes_to_customer": "x" * 2001}, {"internal_notes": "\x00"},
        {"contact_id": "x"}, {"contact_id": 5}, {"opportunity_id": "x"}, {"price_list_id": "x"},
        {"status": "SENT"}, {"number": "COT-9"}, {"total": 1}, {"organization_id": uuid4()},
    ],
)  # fmt: skip
def test_hostile_values_are_a_value_error_and_nothing_is_written(
    shop: SimpleNamespace, migrator: DB, bad: dict[str, Any]
) -> None:
    with pytest.raises(ValueError) as refused:  # noqa: PT011 — el motivo lo dice el comando
        new(shop, **bad)
    assert not isinstance(refused.value, BadReference)  # se rechaza antes de mirar la base
    assert migrator.execute("SELECT count(*) FROM quotes").fetchone() == (0,)


def test_update_replaces_the_notes_and_the_lines_and_reprices_them(
    shop: SimpleNamespace, migrator: DB
) -> None:
    made = new(shop)
    when = NOW + timedelta(days=5)
    changed = move(shop, commands.update_quote, made, valid_until=when, notes_to_customer=SECRET,
                   items=lines((shop.case, 4), (shop.iphone, 1)))  # fmt: skip
    assert (changed.version, changed.status, changed.valid_until) == (2, "DRAFT", when)
    assert changed.updated_by_user_id == shop.quoter.pk and changed.total == Decimal("4801.00")
    assert [(i.sort_order, i.variant_id, i.quantity) for i in stored(shop.a, made)] == [
        (1, shop.case, 4), (2, shop.iphone, 1),
    ]  # fmt: skip
    [_, (action, changes)] = audit(migrator)
    assert action == "quote.updated" and changes["notes_to_customer"] == ["[texto]", "[texto]"]
    assert changes["total"] == ["4399.00", "4801.00"] and "12345678" not in json.dumps(changes)
    same = move(
        shop, commands.update_quote, made, version=2, items=lines((shop.case, 4), (shop.iphone, 1))
    )
    assert same.version == 2 and len(audit(migrator)) == 2  # sin cambios: ni escribe ni audita
    cleared = move(shop, commands.update_quote, made, version=2, valid_until=None)
    assert (cleared.version, cleared.valid_until) == (3, None)


def test_update_reprices_with_the_price_of_now_and_keeps_the_totals_in_step(
    shop: SimpleNamespace,
) -> None:
    made = new(shop, items=lines((shop.iphone, 1)))
    promo(shop.a, [shop.listed], [{"variant_id": shop.iphone}],
          starts=NOW - timedelta(days=1), ends=NOW + timedelta(days=1))  # fmt: skip
    again = move(shop, commands.update_quote, made, items=lines((shop.iphone, 1)))
    assert (again.version, again.total, again.discount_total) == (
        2, Decimal("3959.10"), Decimal("439.90"),
    )  # fmt: skip
    assert again.total == again.subtotal + again.tax_total


@pytest.mark.parametrize(
    "bad",
    [{"status": "SENT"}, {"number": "COT-9"}, {"contact_id": uuid4()}, {"total": 1},
     {"items": []}, {"valid_until": datetime(2026, 11, 1)}, {"internal_notes": 5}],
)  # fmt: skip
def test_update_refuses_what_it_does_not_change_and_hostile_values(
    shop: SimpleNamespace, bad: dict[str, Any]
) -> None:
    made = new(shop)
    with pytest.raises(ValueError):  # noqa: PT011
        move(shop, commands.update_quote, made, **bad)
    for version in (0, True, "1", None, 2**31):
        with pytest.raises(ValueError):  # noqa: PT011
            move(shop, commands.update_quote, made, version=version, internal_notes="x")


def test_a_stale_version_a_closed_quote_and_a_missing_one_are_refused(
    shop: SimpleNamespace,
) -> None:
    made = new(shop)
    with pytest.raises(StaleVersion):
        move(shop, commands.update_quote, made, version=7, internal_notes="x")
    move(shop, commands.update_quote, made, internal_notes="x")  # ahora va por la 2
    with pytest.raises(StaleVersion):  # una versión anterior también es vieja
        move(shop, commands.update_quote, made, internal_notes="y")
    with pytest.raises(StaleVersion):
        move(shop, commands.cancel_quote, made)
    with pytest.raises(StaleVersion):
        move(shop, commands.cancel_quote, made, version=7)
    with tenant_scope(ctx(shop.a)):
        Quote.objects.filter(pk=made.pk).update(status="SENT")
    with pytest.raises(QuoteNotDraft):  # solo un borrador se edita
        move(shop, commands.update_quote, made, version=2, internal_notes="x")
    for command, extra in (
        (commands.update_quote, {"internal_notes": "x"}),
        (commands.cancel_quote, {}),
    ):
        with pytest.raises(Quote.DoesNotExist):
            run(shop, command, quote_id=uuid4(), version=1, **extra)


def test_cancel_works_from_any_open_state_and_audits_the_reason_as_text(
    shop: SimpleNamespace, migrator: DB
) -> None:
    first = move(shop, commands.cancel_quote, new(shop), reason=f"  {SECRET} ")
    assert (first.status, first.cancel_reason, first.version) == ("CANCELLED", SECRET, 2)
    assert first.cancelled_at is not None and first.updated_by_user_id == shop.quoter.pk
    sent = new(shop)
    with tenant_scope(ctx(shop.a)):
        Quote.objects.filter(pk=sent.pk).update(status="SENT")
    plain = move(shop, commands.cancel_quote, sent)  # el motivo es opcional
    assert (plain.status, plain.cancel_reason) == ("CANCELLED", None)
    cancelled = [c for a, c in audit(migrator) if a == "quote.cancelled"]
    assert cancelled[0]["status"] == ["DRAFT", "CANCELLED"] and cancelled[1]["status"][0] == "SENT"
    assert cancelled[0]["cancel_reason"] == [None, "[texto]"] and cancelled[1]["cancel_reason"] == [
        None,
        None,
    ]
    assert "12345678" not in json.dumps(cancelled)
    for state in ("CANCELLED", "ACCEPTED", "REJECTED", "EXPIRED"):
        again = new(shop)
        with tenant_scope(ctx(shop.a)):
            Quote.objects.filter(pk=again.pk).update(
                status=state, cancelled_at=NOW if state == "CANCELLED" else None
            )
        with pytest.raises(QuoteTerminal):
            move(shop, commands.cancel_quote, again)


def test_a_quote_never_carries_a_cost_and_the_commands_do_not_read_one(
    shop: SimpleNamespace, migrator: DB
) -> None:
    for model in (Quote, QuoteItem):
        assert not [f.name for f in model._meta.get_fields() if "cost" in f.name.lower()]
    made = new(shop)
    columns = migrator.execute(
        "SELECT column_name FROM information_schema.columns"
        " WHERE table_name IN ('quotes', 'quote_items') AND column_name ILIKE '%cost%'"
    ).fetchall()
    assert columns == [] and "cost" not in json.dumps(audit(migrator)).lower()
    assert made.total == Decimal("4399.00")


def test_numbers_are_consecutive_and_distinct_under_concurrency(shop: SimpleNamespace) -> None:
    threads, per_thread, seen = 6, 3, []
    start, lock = threading.Barrier(threads, timeout=30), threading.Lock()
    errors: list[BaseException] = []
    contact = tables.contact(shop.a).pk

    def worker() -> None:
        try:
            start.wait()
            for _ in range(per_thread):
                number = run(shop, commands.create_quote, contact_id=contact,
                             items=lines((shop.iphone, 1))).number  # fmt: skip
                with lock:
                    seen.append(number)
        except BaseException as error:  # noqa: BLE001 — se re-lanza en el hilo principal
            errors.append(error)
        finally:
            connection.close()

    pool = [threading.Thread(target=worker, daemon=True) for _ in range(threads)]
    for thread in pool:
        thread.start()
    for thread in pool:
        thread.join(timeout=120)
    assert not errors, errors
    assert not any(thread.is_alive() for thread in pool)
    assert sorted(seen) == [f"COT-{n:06d}" for n in range(1, threads * per_thread + 1)]


def test_two_cancels_at_once_leave_one_and_a_stale_version(shop: SimpleNamespace) -> None:
    made = new(shop)
    cancel = partial(commands.cancel_quote, quote_id=made.pk, version=1)
    first, second = race(shop.a, shop.quoter, [cancel, cancel])
    assert (first.status, first.version, second) == ("CANCELLED", 2, "StaleVersion")


def test_deleting_the_contact_or_the_opportunity_at_once_never_leaves_a_quote_linked(
    shop: SimpleNamespace,
) -> None:
    for field, target, gone in (
        ("contact_id", tables.contact(shop.a).pk, delete_contact),
        ("opportunity_id", tables.make(shop.a).pk, delete_opportunity),
    ):
        remove = partial(gone, **{field: target})
        create = partial(commands.create_quote, items=lines((shop.iphone, 1)), **{field: target})
        assert race(shop.a, shop.quoter, [remove, create])[1] == "BadReference"
