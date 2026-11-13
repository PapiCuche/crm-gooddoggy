"""F5-09c4: revisar una cotización (`apps.quotes.lifecycle.revise_quote`) y lo que la base protege
de las revisiones (`quotes/0007`), con PostgreSQL real, `crm_app`, hilos a la vez y la migración de
ida y vuelta."""

import difflib
import json
from datetime import timedelta
from decimal import Decimal as D
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock
from uuid import uuid4

import psycopg
import pytest
from django.core.management import call_command
from django.db import DatabaseError, IntegrityError, connection, transaction
from django.utils import timezone

from apps.accounts.models import User
from apps.organizations.models import OrganizationMembership as Membership
from apps.quotes import answers, commands, decisions, lifecycle
from apps.quotes.commands import BadReference, NotAllowed, StaleVersion
from apps.quotes.lifecycle import NotRevisable
from apps.quotes.models import Quote, QuoteApproval, QuoteItem
from core.tenancy.scope import tenant_scope
from tests import test_opportunities_tables as tables
from tests.test_authorization import give
from tests.test_inventory_stock import race
from tests.test_memberships import ctx
from tests.test_pipelines_tables import as_migrator
from tests.test_prices import price_list
from tests.test_promotion_tables import promo
from tests.test_quote_answers import accept, decline, lapse, last, sold
from tests.test_quote_commands import DB, audit, shop
from tests.test_quote_commands import world as world  # noqa: PLC0414
from tests.test_quote_decisions import boss
from tests.test_quote_discounts import act, discount, draft, reread, submit
from tests.test_quote_guards import go
from tests.test_quote_hardening import ai_draft, constraint, plain
from tests.test_quote_issue import SECRET, SEND, installed, issue, sender, sql
from tests.test_quote_tables import quote

_ = shop
pytestmark = pytest.mark.usefixtures("tenant_db")
MIGRATION = __import__("apps.quotes.migrations.0007_revision", fromlist=["GUARD"])
WRITE = {"quotes.create": None, "quotes.view": "ORGANIZATION", "quotes.update": "ORGANIZATION",
         "quotes.cancel": "ORGANIZATION"}  # fmt: skip


def reviser(shop: SimpleNamespace, **grants: Any) -> User:
    """Quien emite (`sender`) y además cancela: revisar cancela la anterior."""
    return sender(shop, **{"quotes.cancel": "ORGANIZATION", **grants})


def revise(shop: SimpleNamespace, who: User, made: Quote, version: Any = 2) -> Any:
    return act(shop, who, lifecycle.revise_quote, quote_id=made.pk, version=version)


def family(shop: SimpleNamespace, made: Quote) -> list[tuple[int, str, Any]]:
    """Las revisiones del número de `made`: (revisión, estado, autor)."""
    with tenant_scope(ctx(shop.a)):
        rows = Quote.objects.filter(number=made.number).order_by("revision")
        return list(rows.values_list("revision", "status", "created_by_user_id"))


def outbox(migrator: DB, made: Any) -> list[tuple[str, dict[str, Any]]]:
    found = migrator.execute(
        "SELECT event_type, payload::text FROM outbox_events WHERE aggregate_id = %s ORDER BY id",
        [made.pk],
    ).fetchall()
    return [(kind, json.loads(payload)) for kind, payload in found]


def reach(shop: SimpleNamespace, migrator: DB, who: User, state: str) -> tuple[Quote, int]:
    """Una cotización de `who` en `state`, con su versión."""
    if state in ("DRAFT", "APPROVED", "CANCELLED", "PENDING_APPROVAL"):
        made, phone, _ = draft(shop, who)
        if state == "APPROVED":
            submit(shop, who, made)
        elif state == "CANCELLED":
            act(shop, who, commands.cancel_quote, quote_id=made.pk, version=1)
        elif state == "PENDING_APPROVAL":  # sobre su límite del 10 %
            discount(shop, who, made, phone, "PERCENT", "50")
        return made, 1 if state == "DRAFT" else 2
    made = sold(shop, who)
    if state == "VIEWED":
        sql(shop.a, "UPDATE quotes SET status = 'VIEWED' WHERE id = %s", made.pk)
    elif state == "ACCEPTED":
        accept(shop, who, made)
    elif state == "REJECTED":
        decline(shop, who, made, "Caro")
    elif state == "EXPIRED":
        lapse(migrator, made)
        with tenant_scope(ctx(shop.a)) as system:
            assert answers.expire_quote(system, made.pk)
    return made, 2 if state in ("SENT", "VIEWED") else 3


def test_without_both_permissions_nothing_is_read_not_even_malformed_input(
    shop: SimpleNamespace,
) -> None:
    made = sold(shop, reviser(shop))
    for missing in ("quotes.create", "quotes.update"):
        grants = {k: v for k, v in {**WRITE, **SEND}.items() if k != missing}
        with pytest.raises(NotAllowed):
            act(shop, plain(shop, grants), lifecycle.revise_quote, quote_id="x", version="y")
    assert reread(shop, made).version == 2 and len(family(shop, made)) == 1


def test_the_permission_does_not_give_the_scope_and_one_error_hides_which_case_it_is(
    shop: SimpleNamespace,
) -> None:
    made = sold(shop, reviser(shop))
    update_own = plain(shop, {**WRITE, **SEND, "quotes.update": "OWN"})
    view_own = plain(shop, {**WRITE, **SEND, "quotes.view": "OWN"})
    with tenant_scope(ctx(shop.b)):
        give(shop.b, Membership.objects.get(user=shop.luis).pk, {**WRITE, **SEND})
    seen = set()
    cases = ((shop.a, update_own, made.pk), (shop.a, view_own, made.pk),
             (shop.a, update_own, uuid4()), (shop.b, shop.luis, made.pk))  # fmt: skip
    for org, who, target in cases:
        with tenant_scope(ctx(org, who)) as tenant, pytest.raises(Quote.DoesNotExist) as caught:
            lifecycle.revise_quote(tenant, quote_id=target, version=9)
        seen.add((type(caught.value), str(caught.value), caught.value.args))
    assert len(seen) == 1 and reread(shop, made).version == 2 and len(family(shop, made)) == 1
    with pytest.raises(NotAllowed):  # ya la tiene el cliente: también `quotes.send`, en su alcance
        revise(shop, plain(shop, WRITE), made)
    with pytest.raises(NotAllowed):
        revise(shop, plain(shop, {**WRITE, "quotes.send": "OWN"}), made)
    for cancel in ({}, {"quotes.cancel": "OWN"}):  # revisar cancela: `quotes.cancel` en su alcance
        grants = {k: v for k, v in {**WRITE, **SEND}.items() if k != "quotes.cancel"} | cancel
        with pytest.raises(NotAllowed, match="quotes.cancel"):
            revise(shop, plain(shop, grants), made)
    assert reread(shop, made).version == 2 and len(family(shop, made)) == 1
    with pytest.raises(StaleVersion):  # quien sí la alcanza sabe que su versión es vieja
        revise(shop, plain(shop, {**WRITE, **SEND}), made, version=9)


def test_revising_an_issued_quote_cancels_it_and_opens_the_next_revision_priced_today(
    shop: SimpleNamespace, migrator: DB
) -> None:
    seller = reviser(shop)
    made, phone, _ = draft(shop, seller)
    discount(shop, seller, made, phone, "PERCENT", "5")  # dentro de su 10 %: no se copia
    submit(shop, seller, made, version=2)
    issue(shop, seller, made, version=3, terms=SECRET)
    before, boss_user = reread(shop, made), boss(shop, **{**WRITE, **SEND})
    new = revise(shop, boss_user, made, version=4)  # otro, con alcance de toda la organización
    old = reread(shop, made)
    assert (old.status, old.version, old.sent_at) == ("CANCELLED", 5, before.sent_at)
    assert old.cancelled_at is not None and old.cancel_reason is None
    assert (new.number, new.revision, new.status, new.version) == (made.number, 2, "DRAFT", 1)
    assert (new.created_by_user_id, new.updated_by_user_id) == (seller.pk, boss_user.pk)
    assert (new.sent_at, new.valid_until, new.terms_snapshot, new.customer_snapshot) == (None,) * 4
    with tenant_scope(ctx(shop.a)):
        items = list(QuoteItem.objects.filter(quote_id=new.pk).order_by("sort_order"))
        assert not QuoteApproval.objects.filter(quote_id=new.pk).exists()
    assert [(i.variant_id, i.quantity, i.discount_type, i.final_unit_price) for i in items] == [
        (shop.iphone, 1, "NONE", 4399), (shop.case, 2, "NONE", D("100.50"))]  # fmt: skip
    assert new.total == D("4600.00") > before.total and new.total == new.subtotal + new.tax_total
    actions = audit(migrator)[-2:]
    assert actions[0] == ("quote.cancelled", {"status": ["SENT", "CANCELLED"]})
    assert actions[1][0] == "quote.revised" and actions[1][1]["revision"] == [1, 2]
    assert actions[1][1]["total"] == [str(before.total), "4600.00"]
    assert SECRET not in json.dumps(actions) and last(migrator)[2] == {
        "previous_quote_id": str(made.pk), "total_rose": True}  # fmt: skip
    assert [k for k, _ in outbox(migrator, made)][-1] == "quote.cancelled"
    [(kind, payload)] = outbox(migrator, new)
    assert (kind, payload["previous_quote_id"], payload["revision"]) == (
        "quote.revised", str(made.pk), 2)  # fmt: skip
    assert set(payload) == {"quote_id", "contact_id", "opportunity_id", "status", "revision",
                            "previous_quote_id"}  # fmt: skip
    # la revisión vuelve a pasar por enviar (sin aprobación copiada) y se revisa otra vez
    submit(shop, seller, new)
    issue(shop, seller, new, version=2)
    third = revise(shop, seller, new, version=3)
    assert [r[:2] for r in family(shop, made)] == [(1, "CANCELLED"), (2, "CANCELLED"),
                                                     (3, "DRAFT")]  # fmt: skip
    assert third.created_by_user_id == seller.pk


def test_a_lower_price_today_is_the_new_total_and_does_not_count_as_a_rise(
    shop: SimpleNamespace, migrator: DB
) -> None:
    seller = reviser(shop)
    made, version = reach(shop, migrator, seller, "APPROVED")
    now = timezone.now()
    promo(shop.a, [shop.listed], [{"variant_id": shop.iphone}],
          starts=now - timedelta(days=1), ends=now + timedelta(days=1))  # fmt: skip
    new = revise(shop, seller, made, version)
    assert new.total == D("4160.10") < reread(shop, made).total
    assert last(migrator)[2]["total_rose"] is False and audit(migrator)[-2][1] == {
        "status": ["APPROVED", "CANCELLED"]}  # fmt: skip


@pytest.mark.parametrize("state", ["DRAFT", "PENDING_APPROVAL", "ACCEPTED", "REJECTED", "EXPIRED",
                                   "CANCELLED", "APPROVED", "SENT", "VIEWED"])  # fmt: skip
def test_only_an_open_approved_or_issued_quote_is_revised_and_a_draft_is_kept(
    shop: SimpleNamespace, migrator: DB, state: str
) -> None:
    seller = reviser(shop)
    made, version = reach(shop, migrator, seller, state)
    before = (len(audit(migrator)), reread(shop, made).version)
    if state in lifecycle.REVISABLE:
        assert revise(shop, seller, made, version).revision == 2
        assert family(shop, made)[0][1] == "CANCELLED"
        assert last(migrator)[2]["total_rose"] is False  # el mismo total no es una subida
        return
    if state == "DRAFT":  # se sigue editando: ninguna fila nueva, nada escrito
        assert revise(shop, seller, made, version).pk == made.pk
    else:
        with pytest.raises(NotRevisable):
            revise(shop, seller, made, version)
    assert (len(audit(migrator)), reread(shop, made).version) == before
    assert family(shop, made) == [(1, state, seller.pk)]  # lo terminal sigue terminal


def test_two_revisions_at_once_leave_exactly_one(shop: SimpleNamespace, migrator: DB) -> None:
    seller = reviser(shop)
    made = sold(shop, seller)
    first, second = race(shop.a, seller, [
        lambda t: lifecycle.revise_quote(t, quote_id=made.pk, version=2),
        lambda t: lifecycle.revise_quote(t, quote_id=made.pk, version=2),
    ])  # fmt: skip
    assert (first.revision, second) == (2, "StaleVersion")
    assert [r[:2] for r in family(shop, made)] == [(1, "CANCELLED"), (2, "DRAFT")]
    with pytest.raises(NotRevisable):  # la cancelada no se revisa otra vez
        revise(shop, seller, made, version=3)
    top = sold(shop, seller)
    migrator.execute("UPDATE quotes SET revision = 32767 WHERE id = %s", [top.pk])
    with pytest.raises(NotRevisable):  # la última que cabe en la columna: no un error de la base
        revise(shop, seller, top)
    empty = sold(shop, seller)
    migrator.execute("DELETE FROM quote_items WHERE quote_id = %s", [empty.pk])
    with pytest.raises(NotRevisable):  # sin líneas no hay nada que revalorar
        revise(shop, seller, empty)


def test_what_the_ai_wrote_stays_the_ais_and_needs_a_person_to_approve_it_again(
    shop: SimpleNamespace, migrator: DB
) -> None:
    who = reviser(shop, owner=True)
    made = ai_draft(shop, migrator, who)
    submit(shop, who, made)
    act(shop, boss(shop), decisions.approve_quote, quote_id=made.pk, version=2)
    new = revise(shop, who, made, version=3)
    assert (new.created_by_user_id, family(shop, made)[1][2]) == (None, None)
    assert submit(shop, who, new).status == "PENDING_APPROVAL"  # ni el Owner la aprueba solo
    with tenant_scope(ctx(shop.a)):
        assert QuoteApproval.objects.get(quote_id=new.pk).reason == "AI_CREATED"


@pytest.mark.parametrize("failing", ["record", "_event", "_links:contact", "price"])
def test_a_failure_while_revising_writes_nothing(
    shop: SimpleNamespace, migrator: DB, monkeypatch: pytest.MonkeyPatch, failing: str
) -> None:
    seller = reviser(shop)
    made = sold(shop, seller)
    before = len(audit(migrator))
    if failing == "price":  # la variante ya no tiene precio vigente
        migrator.execute("UPDATE product_prices SET ends_at = now() WHERE variant_id = %s",
                         [shop.iphone])  # fmt: skip
        with pytest.raises(BadReference, match="variant_id"):
            revise(shop, seller, made)
    elif failing == "_links:contact":  # el contacto se borró
        migrator.execute("UPDATE contacts SET deleted_at = now() WHERE id = %s", [made.contact_id])
        with pytest.raises(BadReference, match="contact_id"):
            revise(shop, seller, made)
    else:
        monkeypatch.setattr(lifecycle, failing, Mock(side_effect=DatabaseError("boom")))
        with pytest.raises(DatabaseError, match="boom"):
            revise(shop, seller, made)
    assert (reread(shop, made).status, reread(shop, made).version) == ("SENT", 2)
    assert len(audit(migrator)) == before and family(shop, made) == [(1, "SENT", seller.pk)]


# --- La base (`crm_app`, SQL directo) -------------------------------------------------------------


def test_the_database_keeps_one_live_revision_that_follows_the_one_it_cancels(
    world: SimpleNamespace, migrator: DB
) -> None:
    first = quote(world, number="COT-970001")
    same = (
        "organization_id, number, currency, tax_mode, tax_rate,"
        " subtotal, discount_total, tax_total, total, notes_to_customer, internal_notes"
    )
    copy = (f"INSERT INTO quotes (id, status, version, created_at, updated_at, revision,"  # noqa: S608
            f" contact_id, created_by_user_id, price_list_id, opportunity_id, {same}) SELECT"
            f" gen_random_uuid(), 'DRAFT', 1, now(), now(), {{rev}}, {{contact}}, {{author}},"
            f" {{price_list}}, {{opportunity}}, {same} FROM quotes"
            " WHERE id = %s")  # fmt: skip
    legit = {"rev": "revision + 1", "contact": "contact_id", "author": "created_by_user_id",
             "price_list": "price_list_id", "opportunity": "opportunity_id"}  # fmt: skip
    other_list, opp = price_list(world.a, "OTRA"), tables.make(world.a)
    cancel = "UPDATE quotes SET status = 'CANCELLED', cancelled_at = {at} WHERE id = %s"

    def attempt(at: str = "now()", **over: str) -> None:
        with tenant_scope(ctx(world.a)), transaction.atomic(), connection.cursor() as cursor:
            if at:
                cursor.execute(cancel.format(at=at), [first.pk])
            cursor.execute(copy.format(**{**legit, **over}), [first.pk])
            transaction.set_rollback(True)  # cada intento empieza de la misma fila viva

    for at, over in (
        ("", {}),  # la anterior sigue viva
        ("now() - interval '1 second'", {}),  # cancelada, pero no en esta transacción
        ("now()", {"rev": "revision + 2"}),  # se salta una
        ("now()", {"contact": f"'{tables.contact(world.a).pk}'"}),
        ("now()", {"author": "NULL"}),
        ("now()", {"price_list": f"'{other_list}'"}),  # otra lista
        ("now()", {"opportunity": f"'{opp.pk}'"}),  # otra oportunidad (viva)
    ):  # la de una persona no pasa a ser de la IA
        with pytest.raises(IntegrityError) as raised:
            attempt(at, **over)
        assert constraint(raised) == "quotes_revision_chain"
    attempt()  # en una transacción: cancelada ahora y la siguiente, del mismo contacto y autor
    with pytest.raises(psycopg.errors.UniqueViolation) as dup:  # a todo rol: nunca dos vivas
        migrator.execute(copy.format(**legit), [first.pk])
    assert dup.value.diag.constraint_name == "quotes_one_live_uq"
    for n, state in enumerate(("ACCEPTED", "REJECTED", "EXPIRED", "CANCELLED")):
        done = quote(world, number=f"COT-97100{n}")  # lo terminal no se reabre para revisarlo
        go(world, done, *(("SENT", state) if state != "CANCELLED" else ()))
        if state == "CANCELLED":
            migrator.execute("UPDATE quotes SET status = 'CANCELLED', cancelled_at = now()"
                             " WHERE id = %s", [done.pk])  # fmt: skip
        for target in ("DRAFT", "APPROVED", "SENT"):
            with pytest.raises(IntegrityError) as raised:
                sql(world.a, f"UPDATE quotes SET status = '{target}' WHERE id = %s", done.pk)  # noqa: S608
            assert constraint(raised) == "quotes_status_transition"


def test_the_migration_only_adds_rules_refuses_duplicates_and_goes_back_exactly(
    world: SimpleNamespace, migrator: DB, monkeypatch: pytest.MonkeyPatch
) -> None:
    new = installed(migrator)
    assert new == MIGRATION.body(MIGRATION.GUARD)
    with as_migrator():
        call_command("migrate", "quotes", "0006", verbosity=0)
    old = installed(migrator)
    assert old == MIGRATION.body(MIGRATION.PREVIOUS.GUARD)  # exactamente la de 0006
    removed = [x for x in difflib.ndiff(old.splitlines(), new.splitlines()) if x.startswith("- ")]
    assert removed == [] and old != new  # ninguna regla anterior se perdió
    twin = quote(world, number="COT-980001")
    quote(world, number="COT-980001", revision=2, price_list_id=twin.price_list_id)  # sin 0007
    with as_migrator(), pytest.raises(RuntimeError, match="1 quote numbers"):
        call_command("migrate", "quotes", verbosity=0)
    migrator.execute("UPDATE quotes SET status = 'CANCELLED', cancelled_at = now() WHERE id = %s",
                     [twin.pk])  # fmt: skip
    with as_migrator():
        call_command("migrate", "quotes", verbosity=0)
    assert installed(migrator) == new
    with connection.schema_editor() as editor:
        with pytest.raises(RuntimeError, match="not the expected one"):
            MIGRATION._replace(None, editor)  # instalada otra que la de 0006: no reemplaza nada
        with pytest.raises(RuntimeError, match="BYPASSRLS"):  # `crm_app` no ve todas
            MIGRATION._refuse_duplicates(None, editor)
    monkeypatch.setattr(MIGRATION.PREVIOUS, "GUARD", "otra función")
    with pytest.raises(RuntimeError, match="anchor"):  # si 0006 cambiara, no añade nada
        MIGRATION._guard()
