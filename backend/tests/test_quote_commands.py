"""F5-09a (E13-01): las tablas `quotes` y `quote_items` y sus comandos, con PostgreSQL real y el
rol `crm_app`, también con transacciones a la vez. Sin API: llega con F5-09d y F5-09e."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
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
from apps.pricing import services as pricing_services
from apps.pricing.models import PriceList
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
from tests.test_quote_tables import issued

world = tables.world
pytestmark = pytest.mark.usefixtures("tenant_db")
type DB = psycopg.Connection[Any]
NOW = timezone.now()
LONG_AGO = NOW - timedelta(days=90)
INTERNAL = "prices.view_internal"  # una lista que quien cotiza no ve
MINE: dict[str, str | None] = {  # quien cotiza: crea, ve, cambia y cancela todas las de la org.
    "quotes.create": None, "quotes.view": "ORGANIZATION", "quotes.update": "ORGANIZATION",
    "quotes.cancel": "ORGANIZATION",
}  # fmt: skip
SECRET = "Cliente Juan Perez, DNI 12345678"  # noqa: S105 — texto de prueba


@pytest.fixture
def shop(world: SimpleNamespace) -> SimpleNamespace:
    """Quien cotiza (con `prices.view`), la lista predeterminada y dos variantes con precio."""
    quoter = person(world, {"prices.view": None, **MINE})
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
    assert (made.currency, made.tax_mode, made.tax_rate) == ("PEN", "INCLUDED", D("0.18"))
    assert made.created_by_user_id == made.updated_by_user_id == shop.quoter.pk
    assert (made.notes_to_customer, made.internal_notes) == (SECRET, f"{SECRET}\n\nOtra")
    # 2 x 4399.00 + 3 x 100.50 = 9099.50; el IGV que lleva: 9099.50 - 9099.50 / 1.18
    assert (made.total, made.tax_total, made.subtotal) == (
        D("9099.50"), D("1388.06"), D("7711.44"),
    )  # fmt: skip
    first, second = stored(shop.a, made)
    assert (first.sort_order, first.variant_id, first.quantity) == (1, shop.iphone, 2)
    assert (first.product_name_snapshot, first.sku_snapshot) == ("IP16", "IP16")
    assert (first.list_unit_price, first.final_unit_price, first.line_total) == (
        D("4399.00"), D("4399.00"), D("8798.00"),
    )  # fmt: skip
    assert (second.line_total, second.currency, second.promotion_id_snapshot) == (
        D("301.50"), "PEN", None,
    )  # fmt: skip
    [(action, changes)] = audit(migrator)
    assert action == "quote.created" and changes["status"] == [None, "DRAFT"]
    assert changes["notes_to_customer"] == [None, "[texto]"] and changes["total"][1] == "9099.50"
    assert "12345678" not in json.dumps(changes) and "IP16" not in json.dumps(changes)
    assert "cost" not in json.dumps(changes).lower()  # nunca un costo (ADR-006)
    assert new(shop).number == "COT-000002"


def test_the_tax_is_taken_once_from_the_total_and_rounded_to_the_nearest_cent(
    shop: SimpleNamespace,
) -> None:
    extra = [item(shop.a, f"EXTRA{n}") for n in range(2)]
    for variant in extra:
        row(shop.a, (variant, shop.listed), "4399", LONG_AGO)
    one = new(shop, items=lines((shop.iphone, 1)))
    assert (one.total, one.tax_total, one.subtotal) == (D("4399.00"), D("671.03"), D("3727.97"))
    three = new(shop, items=lines((shop.iphone, 1), (extra[0], 1), (extra[1], 1)))
    assert (three.total, three.tax_total) == (D("13197.00"), D("2013.10"))
    # el IGV de 13197,00 es 2013,1017: redondeado una vez, no tres veces (3 x 671,03 = 2013,09)


def test_an_amount_that_does_not_fit_the_column_is_a_value_error_and_writes_nothing(
    shop: SimpleNamespace, migrator: DB
) -> None:
    big, half = item(shop.a, "BIG"), item(shop.a, "HALF")
    row(shop.a, (big, shop.listed), "999999999999.99", LONG_AGO)
    row(shop.a, (half, shop.listed), "500000000000", LONG_AGO)
    assert new(shop, items=lines((big, 1))).total == D("999999999999.99")  # el máximo cabe
    with pytest.raises(ValueError, match="importe"):  # noqa: PT011 — 2 x 5·10^11 = 10^12: no cabe
        new(shop, items=lines((half, 2)))
    with pytest.raises(ValueError, match="importe"):  # noqa: PT011 — no un error de la base
        new(shop, items=lines((big, 2)))
    assert migrator.execute("SELECT count(*) FROM quotes").fetchone() == (1,)
    made = new(shop)
    with pytest.raises(ValueError, match="importe"):  # noqa: PT011
        move(shop, commands.update_quote, made, items=lines((big, 2)))
    assert made.total == D("4399.00") and len(stored(shop.a, made)) == 1


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
        ("price_list_id", lambda s: price_list(s.a, "X", required_permission=INTERNAL)),
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
    shop.quoter = person(shop, {"products.view": None, **MINE})
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
    assert changed.updated_by_user_id == shop.quoter.pk and changed.total == D("4801.00")
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
        2, D("3959.10"), D("439.90"),
    )  # fmt: skip
    assert again.total == again.subtotal + again.tax_total
    assert (again.tax_total, again.subtotal) == (D("603.93"), D("3355.17"))  # al 18 %


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


def test_a_stale_version_a_closed_quote_and_a_missing_one_are_refused(
    shop: SimpleNamespace,
) -> None:
    made = new(shop)
    with pytest.raises(StaleVersion):
        move(shop, commands.update_quote, made, version=7, internal_notes="x")
    with pytest.raises(ValueError):  # noqa: PT011 — una versión que no es de 1 en adelante
        move(shop, commands.update_quote, made, version=0, internal_notes="x")
    move(shop, commands.update_quote, made, internal_notes="x")  # ahora va por la 2
    with pytest.raises(StaleVersion):  # una versión anterior también es vieja
        move(shop, commands.update_quote, made, internal_notes="y")
    with pytest.raises(StaleVersion):
        move(shop, commands.cancel_quote, made)
    with tenant_scope(ctx(shop.a)):
        Quote.objects.filter(pk=made.pk).update(status="SENT", **issued())
    with pytest.raises(QuoteNotDraft):  # solo un borrador se edita
        move(shop, commands.update_quote, made, version=2, internal_notes="x")
    with pytest.raises(Quote.DoesNotExist):
        run(shop, commands.cancel_quote, quote_id=uuid4(), version=1)


def test_cancel_works_from_any_open_state_and_audits_the_reason_as_text(
    shop: SimpleNamespace, migrator: DB
) -> None:
    first = move(shop, commands.cancel_quote, new(shop), reason=f"  {SECRET} ")
    assert (first.status, first.cancel_reason, first.version) == ("CANCELLED", SECRET, 2)
    assert first.cancelled_at is not None and first.updated_by_user_id == shop.quoter.pk
    sent = new(shop)
    with tenant_scope(ctx(shop.a)):
        Quote.objects.filter(pk=sent.pk).update(status="SENT", **issued())
    plain = move(shop, commands.cancel_quote, sent)  # el motivo es opcional
    assert (plain.status, plain.cancel_reason) == ("CANCELLED", None)
    cancelled = [c for a, c in audit(migrator) if a == "quote.cancelled"]
    assert cancelled[0]["status"] == ["DRAFT", "CANCELLED"] and cancelled[1]["status"][0] == "SENT"
    assert [c["cancel_reason"] for c in cancelled] == [[None, "[texto]"], [None, None]]
    assert "12345678" not in json.dumps(cancelled)
    again = new(shop)
    with tenant_scope(ctx(shop.a)):
        Quote.objects.filter(pk=again.pk).update(status="SENT", **issued())
    for state in ("ACCEPTED",):  # un estado terminal: el grafo de la base solo deja llegar por SENT
        with tenant_scope(ctx(shop.a, shop.quoter)):  # la respuesta, con su fecha (F5-09c3)
            Quote.objects.filter(pk=again.pk).update(status=state, accepted_at=timezone.now())
        with pytest.raises(QuoteTerminal):
            move(shop, commands.cancel_quote, again)
    with pytest.raises(QuoteTerminal):  # ya cancelada
        move(shop, commands.cancel_quote, first, version=2)


def test_numbers_are_consecutive_and_distinct_under_concurrency(shop: SimpleNamespace) -> None:
    threads, per_thread = 6, 3
    start, contact = threading.Barrier(threads, timeout=30), tables.contact(shop.a).pk

    def worker(_: int) -> list[str]:
        try:
            start.wait()
            return [
                run(
                    shop, commands.create_quote, contact_id=contact, items=lines((shop.iphone, 1))
                ).number  # fmt: skip
                for _ in range(per_thread)
            ]
        finally:
            connection.close()

    with ThreadPoolExecutor(threads) as pool:  # una excepción de un hilo sale aquí
        seen = sorted(n for batch in pool.map(worker, range(threads)) for n in batch)
    assert seen == [f"COT-{n:06d}" for n in range(1, threads * per_thread + 1)]


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


def test_the_totals_of_a_quote_always_agree_with_its_lines_after_every_command(
    shop: SimpleNamespace,
) -> None:
    """Ninguna restricción lo impone (haría falta un disparador diferido): lo garantizan los
    comandos, y esta comprobación lo verifica tras crear y tras cambiar las líneas."""
    promo(shop.a, [shop.listed], [{"variant_id": shop.iphone}],
          starts=NOW - timedelta(days=1), ends=NOW + timedelta(days=1))  # fmt: skip
    made = new(shop, items=lines((shop.iphone, 3), (shop.case, 7)))
    first, second = stored(shop.a, made)  # la promoción baja 4399,00 a 3959,10 y se congela
    assert (first.list_unit_price, first.final_unit_price) == (
        D("4399.00"),
        D("3959.10"),
    )
    assert (first.promotion_name_snapshot, second.promotion_id_snapshot) == ("Promo", None)
    for step in range(2):
        quote = made if step == 0 else move(shop, commands.update_quote, made,
                                            items=lines((shop.case, 2)))  # fmt: skip
        rows = stored(shop.a, quote)
        assert quote.total == sum(r.line_total for r in rows)
        assert quote.total == quote.subtotal + quote.tax_total
        assert quote.discount_total == sum(r.quantity * (r.list_unit_price - r.final_unit_price)
                                           for r in rows)  # fmt: skip
        assert all(r.line_total == r.quantity * r.final_unit_price for r in rows)
        assert all(r.discount_amount == 0 and r.unit_price == r.final_unit_price for r in rows)


def test_a_price_that_is_not_tax_included_is_never_quoted_as_if_it_were(
    shop: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, migrator: DB
) -> None:
    """D-PRC-1: los precios llevan IGV; si el motor dijera otra cosa, falla cerrada."""
    real = pricing_services.resolve_many
    monkeypatch.setattr(
        commands, "resolve_many",
        lambda *a, **k: {v: replace(r, tax_included=False) for v, r in real(*a, **k).items()},
    )  # fmt: skip
    with pytest.raises(BadReference) as refused:
        new(shop)
    assert refused.value.field == "price_list_id"
    assert migrator.execute("SELECT count(*) FROM quotes").fetchone() == (0,)


def test_without_a_price_list_and_without_a_default_one_the_error_names_the_list(
    shop: SimpleNamespace,
) -> None:
    with tenant_scope(ctx(shop.a)):
        PriceList.objects.filter(pk=shop.listed).update(is_default=False)
    with pytest.raises(BadReference) as refused:
        new(shop)  # sin `price_list_id` y sin predeterminada
    assert refused.value.field == "price_list_id"
    assert new(shop, price_list_id=shop.listed).price_list_id == shop.listed
