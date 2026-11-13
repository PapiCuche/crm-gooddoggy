"""F5-09c2: emitir una cotización (`apps.quotes.lifecycle.issue_quote`) y lo que la base protege de
lo emitido (`quotes/0005`), con PostgreSQL real, `crm_app`, transacciones a la vez y la migración
de ida y vuelta."""

import difflib
import json
from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from django.core.management import call_command
from django.db import IntegrityError, connection, transaction
from django.utils import timezone

from apps.accounts.models import User
from apps.organizations.models import OrganizationMembership as Membership
from apps.pricing import costs
from apps.quotes import commands, decisions, lifecycle
from apps.quotes.commands import BadReference, StaleVersion
from apps.quotes.discounts import PriceRefused
from apps.quotes.lifecycle import NeedsApproval, NotIssuable
from apps.quotes.models import Quote
from core.tenancy.scope import tenant_scope
from tests.test_authorization import give
from tests.test_inventory_stock import race
from tests.test_memberships import ctx
from tests.test_pipelines_tables import as_migrator
from tests.test_quote_commands import DB, audit, shop
from tests.test_quote_commands import world as world  # noqa: PLC0414
from tests.test_quote_decisions import asked, boss
from tests.test_quote_discounts import (
    act,
    back_to_draft,
    cost,
    discount,
    draft,
    manual,
    member,
    reread,
    submit,
)
from tests.test_quote_guards import approval, go
from tests.test_quote_hardening import ai_draft, constraint, events, plain
from tests.test_quote_tables import issued, quote

_ = shop
pytestmark = pytest.mark.usefixtures("tenant_db")
MIGRATION = __import__("apps.quotes.migrations.0005_issue", fromlist=["GUARD"])
SEND: dict[str, Any] = {"quotes.send": "ORGANIZATION", "contacts.view": "ORGANIZATION"}
SECRET = "Pago 50 % DNI 87654321"  # noqa: S105 — texto de prueba


def sender(shop: SimpleNamespace, percent: str | None = "10", **grants: Any) -> User:
    return member(shop, percent, **{**SEND, **grants})


def later(days: int = 30) -> Any:
    return timezone.now() + timedelta(days=days)


def issue(shop: SimpleNamespace, who: User, made: Quote, version: int = 1, **fields: Any) -> Any:
    fields.setdefault("valid_until", later())
    return act(shop, who, lifecycle.issue_quote, quote_id=made.pk, version=version, **fields)


def sql(org: Any, statement: str, *params: Any, user: User | None = None) -> None:
    """Un SQL directo de `crm_app` en su transacción (un error no deja la conexión rota)."""
    with tenant_scope(ctx(org, user)), connection.cursor() as cursor, transaction.atomic():
        cursor.execute(statement, list(params))


def refused(org: Any, name: str, statement: str, *params: Any) -> None:
    with pytest.raises(IntegrityError) as raised:
        sql(org, statement, *params)
    assert constraint(raised) == name


def test_issuing_freezes_the_snapshots_and_audits_and_tells_only_ids(
    shop: SimpleNamespace, migrator: DB
) -> None:
    seller = sender(shop)
    made, _, _ = draft(shop, seller)
    migrator.execute("UPDATE contacts SET document_type = 'DNI', document_number = '12345678',"
                     " display_name = 'Ana Ruiz' WHERE id = %s", [made.contact_id])  # fmt: skip
    until = later()
    done = issue(shop, seller, made, valid_until=until, terms=f"  {SECRET} ", warranty="12 meses")
    assert (done.status, done.version, done.valid_until, done.terms_snapshot) == (
        "SENT", 2, until, SECRET,
    )  # fmt: skip
    stored = reread(shop, made)
    [(org,)] = migrator.execute("SELECT name FROM organizations WHERE id = %s", [shop.a]).fetchall()
    assert stored.company_snapshot == {"name": org} and stored.sent_at is not None
    assert stored.customer_snapshot == {"display_name": "Ana Ruiz", "document_type": "DNI",
                                        "document_number": "12345678"}  # fmt: skip
    action, changes = audit(migrator)[-1]
    assert action == "quote.issued" and changes["status"] == ["DRAFT", "SENT"]
    assert changes["terms"] == changes["warranty"] == [None, "[texto]"]
    shown = json.dumps(changes)
    assert not any(word in shown for word in ("12345678", "Ana", "Pago", "meses", "cost"))
    assert events(migrator, made) == ["quote.issued"]
    with pytest.raises(NotIssuable):  # y no se emite dos veces
        issue(shop, seller, made, version=2)


def test_an_approved_quote_is_issued_with_the_validity_it_already_had(
    shop: SimpleNamespace,
) -> None:
    seller, until = sender(shop), later(10)
    made, _, _ = draft(shop, seller)
    act(shop, seller, commands.update_quote, quote_id=made.pk, version=1, valid_until=until)
    assert submit(shop, seller, made, version=2).status == "APPROVED"
    done = issue(shop, seller, made, version=3, valid_until=None)
    assert (done.status, done.valid_until, done.terms_snapshot) == ("SENT", until, None)


def test_the_permission_does_not_give_the_scope_and_one_error_hides_which_case_it_is(
    shop: SimpleNamespace,
) -> None:
    made = draft(shop, sender(shop))[0]  # de otra persona
    send_own = plain(shop, {**SEND, "quotes.send": "OWN", "quotes.view": "ORGANIZATION"})
    view_own = plain(shop, {**SEND, "quotes.view": "OWN"})
    with tenant_scope(ctx(shop.b)):
        give(shop.b, Membership.objects.get(user=shop.luis).pk,
             {**SEND, "quotes.view": "ORGANIZATION"})  # fmt: skip
    seen = set()
    cases = ((shop.a, send_own, made.pk), (shop.a, view_own, made.pk),
             (shop.a, send_own, uuid4()), (shop.b, shop.luis, made.pk))  # fmt: skip
    for org, who, target in cases:
        with tenant_scope(ctx(org, who)) as tenant, pytest.raises(Quote.DoesNotExist) as caught:
            lifecycle.issue_quote(tenant, quote_id=target, version=9, valid_until=later())
        seen.add((type(caught.value), str(caught.value), caught.value.args))
    assert len(seen) == 1 and reread(shop, made).version == 1
    with pytest.raises(StaleVersion):  # quien sí la alcanza sabe que su versión es vieja
        issue(shop, sender(shop), made, version=9)


def test_only_a_draft_or_an_approved_quote_is_issued(shop: SimpleNamespace) -> None:
    seller = sender(shop, "3", **{"quotes.cancel": "ORGANIZATION"})
    waiting, _ = asked(shop, seller)
    with pytest.raises(NotIssuable):
        issue(shop, seller, waiting, version=2)
    gone = draft(shop, seller)[0]
    act(shop, seller, commands.cancel_quote, quote_id=gone.pk, version=1)
    with pytest.raises(NotIssuable):
        issue(shop, seller, gone, version=2)


def test_the_validity_is_in_the_future_by_the_clock_of_the_command(
    shop: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    seller = sender(shop)
    made, _, _ = draft(shop, seller)
    for bad in (None, timezone.now() - timedelta(seconds=1), "2030-01-01", later(-1)):
        with pytest.raises(ValueError):  # noqa: PT011 — sin validez, pasada o sin zona
            issue(shop, seller, made, valid_until=bad)
    frozen = later(1)
    monkeypatch.setattr(lifecycle, "timezone", SimpleNamespace(now=lambda: frozen))
    with pytest.raises(ValueError):  # noqa: PT011 — justo ahora no es futuro
        issue(shop, seller, made, valid_until=frozen)
    done = issue(shop, seller, made, valid_until=frozen + timedelta(microseconds=1))
    assert (done.status, done.sent_at) == ("SENT", frozen)


@pytest.mark.parametrize("scheduled", [False, True])
def test_a_contact_out_of_reach_is_one_error_before_the_cost_which_is_checked_again(
    shop: SimpleNamespace, migrator: DB, scheduled: bool
) -> None:
    pricer = member(shop, "100", **{"quotes.override_price": None})
    cost(shop, shop.iphone, "1000")
    made, phone, _ = draft(shop, pricer)
    manual(shop, pricer, made, phone, "3500")  # cubre 1000: espera aprobación
    act(shop, boss(shop, "100"), decisions.approve_quote, quote_id=made.pk, version=2)
    if scheduled:  # un costo de 4000 programado dentro de tres días
        when = timezone.now() + timedelta(days=3)
        migrator.execute("UPDATE variant_costs SET ends_at = %s", [when])
        cost(shop, shop.iphone, "4000", starts_at=when, source="IMPORT")
    else:  # el costo de hoy subió después de aprobarla
        act(shop, shop.ana, costs.set_cost, variant_id=shop.iphone, amount="4000", reason="Subió")
    blind = sender(shop, **{"contacts.view": "OWN"})  # el contacto no es suyo
    seen = set()
    for who in (blind, plain(shop, {"quotes.send": "ORGANIZATION",
                                    "quotes.view": "ORGANIZATION"})):  # fmt: skip
        with pytest.raises(BadReference) as caught:
            issue(shop, who, made, version=3)
        seen.add((type(caught.value), caught.value.args, caught.value.field))
    migrator.execute("UPDATE contacts SET deleted_at = now() WHERE id = %s", [made.contact_id])
    with pytest.raises(BadReference) as caught:
        issue(shop, sender(shop), made, version=3)
    seen.add((type(caught.value), caught.value.args, caught.value.field))
    assert seen == {(BadReference, ("contact_id",), "contact_id")}
    migrator.execute("UPDATE contacts SET deleted_at = NULL WHERE id = %s", [made.contact_id])
    with pytest.raises(PriceRefused) as price:
        issue(shop, sender(shop), made, version=3)
    assert price.value.args == () and reread(shop, made).status == "APPROVED"


def test_a_draft_is_measured_again_with_the_limit_of_who_issues_it(
    shop: SimpleNamespace, migrator: DB
) -> None:
    senior, careful = sender(shop, "10"), sender(shop, "3")
    made, phone, _ = draft(shop, senior)
    discount(shop, senior, made, phone, "PERCENT", "5")  # cabe en el 10 % de quien lo puso
    before = len(audit(migrator))
    with pytest.raises(NeedsApproval):  # no en el 3 % de quien emite
        issue(shop, careful, made, version=2)
    assert (reread(shop, made).status, len(audit(migrator))) == ("DRAFT", before)
    assert issue(shop, senior, made, version=2).status == "SENT"
    pricer = member(shop, "100", **{"quotes.override_price": None})
    cost(shop, shop.iphone, "1000")
    priced, line, _ = draft(shop, pricer)
    manual(shop, pricer, priced, line, "3500")
    back_to_draft(shop, priced)  # un borrador con precio manual
    with pytest.raises(NeedsApproval):  # ni el Owner lo emite sin aprobación
        issue(shop, sender(shop, owner=True), priced, version=3)


def test_what_the_ai_wrote_goes_out_only_after_a_person_approved_it(
    shop: SimpleNamespace, migrator: DB
) -> None:
    who = sender(shop, owner=True)
    made = ai_draft(shop, migrator, who)
    with pytest.raises(NeedsApproval):  # sin descuento ni precio manual, y con el Owner
        issue(shop, who, made)
    submit(shop, who, made)
    act(shop, boss(shop), decisions.approve_quote, quote_id=made.pk, version=2)
    migrator.execute(  # una solicitud posterior, cancelada: la que manda es la última
        "INSERT INTO quote_approvals SELECT (r).* FROM (SELECT jsonb_populate_record(a,"
        " jsonb_build_object('id', gen_random_uuid(), 'status', 'CANCELLED', 'created_at',"
        " now() + interval '1 minute')) AS r FROM quote_approvals a WHERE quote_id = %s) AS x",
        [made.pk],
    )  # fmt: skip
    with pytest.raises(NeedsApproval):
        issue(shop, who, made, version=3)
    migrator.execute("DELETE FROM quote_approvals WHERE status = 'CANCELLED'")
    assert issue(shop, who, made, version=3).status == "SENT"
    loose = ai_draft(shop, migrator, who)  # aprobada sin solicitud (antes de 0005, o a mano)
    migrator.execute("UPDATE quotes SET status = 'APPROVED' WHERE id = %s", [loose.pk])
    with pytest.raises(NeedsApproval):
        issue(shop, who, loose)


def test_two_issues_or_an_issue_and_a_cancel_at_once_leave_one(shop: SimpleNamespace) -> None:
    seller = sender(shop, **{"quotes.cancel": "ORGANIZATION"})
    until = later()
    for rival in (lifecycle.issue_quote, commands.cancel_quote):
        made = draft(shop, seller)[0]
        extra = {"valid_until": until} if rival is lifecycle.issue_quote else {}
        first, second = race(shop.a, seller, [
            lambda t, m=made: lifecycle.issue_quote(t, quote_id=m.pk, version=1, valid_until=until),
            lambda t, m=made, r=rival, e=extra: r(t, quote_id=m.pk, version=1, **e),
        ])  # fmt: skip
        assert (first.status, second) == ("SENT", "StaleVersion")
        assert (reread(shop, made).status, reread(shop, made).version) == ("SENT", 2)


@pytest.mark.parametrize("failing", ["record", "_event"])
def test_a_failure_while_issuing_writes_nothing(
    shop: SimpleNamespace, migrator: DB, monkeypatch: pytest.MonkeyPatch, failing: str
) -> None:
    seller = sender(shop)
    made, _, _ = draft(shop, seller)
    before = len(audit(migrator))

    def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(lifecycle, failing, boom)
    with pytest.raises(RuntimeError, match="boom"):
        issue(shop, seller, made)
    after = reread(shop, made)
    assert (after.status, after.version, after.sent_at, after.customer_snapshot) == (
        "DRAFT", 1, None, None,
    )  # fmt: skip
    assert len(audit(migrator)) == before and events(migrator, made) == []


# --- La base (`crm_app`, SQL directo) -------------------------------------------------------------


def test_a_quote_is_born_without_snapshots_even_by_sql(world: SimpleNamespace) -> None:
    for field, value in issued(terms_snapshot="x", warranty_snapshot="y").items():
        if field != "valid_until":
            with pytest.raises(IntegrityError) as raised:
                quote(world, number="COT-910001", **{field: value})
            assert constraint(raised) == "quotes_born_clean"


def test_the_snapshots_are_written_only_when_issuing_and_never_again(
    world: SimpleNamespace,
) -> None:
    made = quote(world)
    change = "UPDATE quotes SET {} = %s WHERE id = %s"
    for field, value in issued(terms_snapshot="x").items():  # en borrador, tampoco
        if field != "valid_until":
            value = json.dumps(value) if isinstance(value, dict) else value
            refused(world.a, "quotes_issued_frozen", change.format(field), value, made.pk)
    go(world, made, "SENT")  # sin condiciones: el nulo tampoco se reescribe
    worded = quote(world, number="COT-910002")
    sql(world.a, "UPDATE quotes SET status = 'SENT', terms_snapshot = 'x', company_snapshot ="
        " '{}', customer_snapshot = '{}', sent_at = now(), valid_until = now() + interval '1 day'"
        " WHERE id = %s", worded.pk)  # fmt: skip
    for target, field, value in (
        (made, "company_snapshot", '{"name": "Otra"}'), (made, "customer_snapshot", "{}"),
        (made, "sent_at", timezone.now()), (made, "terms_snapshot", "x"),
        (worded, "terms_snapshot", None), (made, "warranty_snapshot", "y"),
    ):  # fmt: skip
        refused(world.a, "quotes_issued_frozen", change.format(field), value, target.pk)
    for field, value in (("notes_to_customer", "otra"), ("valid_until", later(60))):
        refused(world.a, "quotes_terms_frozen", change.format(field), value, made.pk)
    sql(world.a, change.format("internal_notes"), "visto", made.pk)  # decisión del propietario


def test_what_the_customer_reads_is_frozen_from_pending_approval(world: SimpleNamespace) -> None:
    change = "UPDATE quotes SET {} WHERE id = %s"
    bare, dated = quote(world), quote(world, number="COT-910003", valid_until=later())
    for made in (bare, dated):
        approval(world, made)
        go(world, made, "PENDING_APPROVAL")
    for made, assignment in ((bare, "notes_to_customer = 'otra'"), (bare, "valid_until = now()"),
                             (dated, "valid_until = NULL")):  # fmt: skip
        refused(world.a, "quotes_terms_frozen", change.format(assignment), made.pk)
    approved = quote(world, number="COT-910004", valid_until=later())
    go(world, approved, "APPROVED")
    refused(world.a, "quotes_terms_frozen", change.format("valid_until = NULL"), approved.pk)
    refused(world.a, "quotes_terms_frozen", change.format("notes_to_customer = 'otra'"),
            approved.pk)  # fmt: skip
    go(world, approved, "SENT")  # al emitir, la validez sí se fija
    assert reread(world, approved).valid_until > later(6)


def test_a_cancellation_is_never_rewritten(world: SimpleNamespace) -> None:
    change = "UPDATE quotes SET {} WHERE id = %s"
    said, silent = quote(world), quote(world, number="COT-910005")
    for made, why in ((said, "'Caro'"), (silent, "NULL")):
        sql(world.a, change.format(f"status = 'CANCELLED', cancelled_at = now(), "
                                   f"cancel_reason = {why}"), made.pk)  # fmt: skip
    for made, assignment in ((said, "cancel_reason = 'Otro'"), (said, "cancel_reason = NULL"),
                             (silent, "cancel_reason = 'Nuevo'"),
                             (said, "cancelled_at = now() - interval '1 day'")):  # fmt: skip
        refused(world.a, "quotes_cancel_frozen", change.format(assignment), made.pk)


def test_what_the_ai_wrote_never_skips_the_request_even_by_sql(
    world: SimpleNamespace, migrator: DB
) -> None:
    for target in ("APPROVED", "SENT"):
        ai = quote(world, number=f"COT-92000{len(target)}", created_by_user_id=None)
        with pytest.raises(IntegrityError) as raised:
            go(world, ai, target)
        assert constraint(raised) == "quotes_ai_needs_approval"
    migrator.execute("UPDATE quotes SET status = 'APPROVED' WHERE created_by_user_id IS NULL")
    with pytest.raises(IntegrityError) as raised:
        go(world, ai, "SENT")  # aprobada sin la última solicitud aprobada
    assert constraint(raised) == "quotes_ai_needs_approval"


def test_an_issued_quote_is_valid_until_a_future_moment_of_the_database_clock(
    world: SimpleNamespace, migrator: DB
) -> None:
    made = quote(world)
    send = (
        "UPDATE quotes SET status = 'SENT', company_snapshot = '{}', customer_snapshot = %s,"
        " sent_at = now() - interval '1 second', valid_until = now() + %s WHERE id = %s"
    )
    refused(world.a, "quotes_valid_future", send, "{}", timedelta(0), made.pk)
    refused(world.a, "quotes_issued_ck", send, None, timedelta(days=1), made.pk)  # sin cliente
    with pytest.raises(psycopg.errors.CheckViolation, match="quotes_issued_ck"):  # a todo rol
        migrator.execute("UPDATE quotes SET status = 'SENT' WHERE id = %s", [made.pk])
    sql(world.a, send, "{}", timedelta(microseconds=1), made.pk)
    assert reread(world, made).status == "SENT"


def installed(migrator: DB) -> str:
    row = migrator.execute(
        "SELECT prosrc, prosecdef FROM pg_proc WHERE oid = 'public.quotes_guard()'::regprocedure"
    ).fetchone()
    assert row is not None and row[1] is False  # INVOKER: corre con los permisos de quien escribe
    return str(row[0])


def test_the_migration_only_adds_rules_and_goes_back_only_without_issued_quotes(
    world: SimpleNamespace, migrator: DB, monkeypatch: pytest.MonkeyPatch
) -> None:
    new = installed(migrator)
    assert new == MIGRATION.body(MIGRATION.GUARD)
    legacy = quote(world)
    with as_migrator():
        call_command("migrate", "quotes", "0004", verbosity=0)
    old = installed(migrator)
    assert old == MIGRATION.body(MIGRATION.PREVIOUS.QUOTES_GUARD)  # exactamente la de 0003
    removed = [x for x in difflib.ndiff(old.splitlines(), new.splitlines()) if x.startswith("- ")]
    assert removed == [] and old != new  # ninguna regla anterior se perdió
    migrator.execute("UPDATE quotes SET status = 'SENT' WHERE id = %s", [legacy.pk])
    with as_migrator(), pytest.raises(RuntimeError, match="issued before snapshots"):
        call_command("migrate", "quotes", verbosity=0)
    migrator.execute("UPDATE quotes SET status = 'CANCELLED', cancelled_at = now()")
    with as_migrator():
        call_command("migrate", "quotes", verbosity=0)
    assert installed(migrator) == new
    sent = quote(world, number="COT-930001")
    go(world, sent, "SENT")
    with as_migrator(), pytest.raises(RuntimeError, match="refusing to go back"):
        call_command("migrate", "quotes", "0004", verbosity=0)
    assert installed(migrator) == new and reread(world, sent).sent_at is not None
    with connection.schema_editor() as editor, pytest.raises(RuntimeError, match="not the one"):
        MIGRATION._replace(None, editor)  # instalada otra que la de 0003: no reemplaza nada
    monkeypatch.setattr(MIGRATION.PREVIOUS, "QUOTES_GUARD", "otra función")
    with pytest.raises(RuntimeError, match="anchors"):  # si 0003 cambiara, no añade nada
        MIGRATION._guard()
