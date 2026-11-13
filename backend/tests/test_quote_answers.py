"""F5-09c3: aceptar y rechazar por el cliente, vencer (`apps.quotes.answers` y `apps.quotes.tasks`)
y lo que la base protege de la respuesta (`quotes/0006`), con PostgreSQL real, `crm_app`,
transacciones a la vez y la migración de ida y vuelta."""

import difflib
import json
import threading
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock
from uuid import uuid4

import pytest
from django.core.management import call_command
from django.db import DatabaseError, IntegrityError, connection
from django.utils import timezone

from apps.access.selectors import AccessDenied
from apps.accounts.models import User
from apps.deals.models import Opportunity, Pipeline
from apps.deals.opportunity_stages import move_opportunity, win_opportunity
from apps.organizations.models import OrganizationMembership as Membership
from apps.quotes import answers, commands, tasks
from apps.quotes.answers import NotAnswerable, QuoteExpired
from apps.quotes.commands import NotAllowed, StaleVersion
from apps.quotes.models import Quote
from core.tenancy.context import ActorType
from core.tenancy.scope import tenant_scope
from tests import test_opportunities_tables as tables
from tests.test_authorization import give
from tests.test_inventory_stock import race
from tests.test_memberships import ctx
from tests.test_pipelines_tables import as_migrator, make_stage
from tests.test_quote_commands import DB, audit, lines, shop
from tests.test_quote_commands import world as world  # noqa: PLC0414
from tests.test_quote_discounts import act, draft, reread
from tests.test_quote_guards import go
from tests.test_quote_hardening import constraint, events, plain
from tests.test_quote_issue import SECRET, SEND, installed, issue, later, refused, sender, sql
from tests.test_quote_tables import quote

_ = shop
pytestmark = pytest.mark.usefixtures("tenant_db")
MIGRATION = __import__("apps.quotes.migrations.0006_answer", fromlist=["GUARD"])
OPPS = {"opportunities.view": "ORGANIZATION", "opportunities.update": "ORGANIZATION"}


def sold(shop: SimpleNamespace, who: User, opp: Any = None) -> Quote:
    """Una cotización emitida (versión 2) de `who`, de la oportunidad `opp` si se da."""
    link = {"opportunity_id": opp.pk} if opp else {"contact_id": tables.contact(shop.a).pk}
    made = act(shop, who, commands.create_quote, items=lines((shop.iphone, 1)), **link)
    issue(shop, who, made)
    return made  # type: ignore[no-any-return]


def accept(shop: SimpleNamespace, who: User, made: Quote, version: Any = 2) -> Any:
    return act(shop, who, answers.accept_quote, quote_id=made.pk, version=version)


def decline(shop: SimpleNamespace, who: User, made: Quote, reason: Any, version: Any = 2) -> Any:
    return act(shop, who, answers.reject_quote_by_customer, quote_id=made.pk, version=version,
               reason=reason)  # fmt: skip


def lapse(migrator: DB, made: Quote, seconds: float = 1) -> None:
    """La validez pasó hace `seconds` según la base (el migrador no pasa por el disparador)."""
    migrator.execute("UPDATE quotes SET sent_at = now() - interval '2 days', valid_until = now()"
                     " - make_interval(secs => %s) WHERE id = %s", [seconds, made.pk])  # fmt: skip


def last(migrator: DB) -> tuple[str, str, dict[str, Any]]:
    row = migrator.execute(
        "SELECT action, actor_type, metadata::text FROM audit_logs WHERE"
        " entity_type = 'quote' ORDER BY occurred_at DESC, id DESC"
    ).fetchone()
    return row[0], row[1], json.loads(row[2])  # type: ignore[index]


def clock(monkeypatch: pytest.MonkeyPatch, now: Any) -> None:
    monkeypatch.setattr(answers, "timezone", SimpleNamespace(now=lambda: now))


@pytest.fixture
def paying(shop: SimpleNamespace) -> Any:
    """La etapa «Pago pendiente» del pipeline minorista de A."""
    retail = Pipeline(pk=tables.stage(shop.a).pipeline_id)
    return make_stage(shop.a, retail, answers.STAGE, sort_order=45, default_probability=80)


def where(shop: SimpleNamespace, opp: Any) -> tuple[Any, int]:
    with tenant_scope(ctx(shop.a)):
        found = Opportunity.objects.get(pk=opp.pk)
    return found.stage_id, found.version


def test_accepting_moves_the_opportunity_and_tells_only_ids(
    shop: SimpleNamespace, migrator: DB, paying: Any
) -> None:
    seller, opp = sender(shop, **OPPS), tables.make(shop.a)
    made = sold(shop, seller, opp)
    assert (accept(shop, seller, made).version, where(shop, opp)) == (3, (paying.pk, 2))
    assert (reread(shop, made).status, bool(reread(shop, made).accepted_at)) == ("ACCEPTED", True)
    action, actor, about = last(migrator)
    assert (action, actor) == ("quote.accepted", "USER")
    assert about == {"opportunity_id": str(opp.pk), "opportunity_move": "moved"}
    assert audit(migrator)[-1][1]["status"] == ["SENT", "ACCEPTED"]
    assert events(migrator, made) == ["quote.issued", "quote.accepted"]
    with pytest.raises(StaleVersion):
        accept(shop, seller, made)
    with pytest.raises(NotAnswerable):  # y no se responde dos veces
        decline(shop, seller, made, "Caro", version=3)


def test_declining_needs_a_reason_and_is_not_the_approvers_rejection(
    shop: SimpleNamespace, migrator: DB
) -> None:
    seller = sender(shop)
    made = sold(shop, seller)
    for bad in (None, "", "  \n ", 5):
        with pytest.raises(ValueError):  # noqa: PT011
            decline(shop, seller, made, bad)
    assert reread(shop, made).status == "SENT"
    done = decline(shop, seller, made, f" {SECRET} ")
    stored = reread(shop, made)
    assert (done.status, stored.rejection_reason, stored.accepted_at) == ("REJECTED", SECRET, None)
    action, changes = audit(migrator)[-1]
    assert action == "quote.declined" and changes["rejection_reason"] == [None, "[texto]"]
    assert "87654321" not in json.dumps(changes) and events(migrator, made)[-1] == "quote.declined"


def test_only_a_person_with_quotes_send_answers_and_one_error_hides_which_case_it_is(
    shop: SimpleNamespace,
) -> None:
    made = sold(shop, sender(shop))
    with pytest.raises(NotAllowed):  # sin permiso, ni se mira lo que se envía
        act(shop, plain(shop, {"quotes.view": "ORGANIZATION"}), answers.accept_quote,
            quote_id="x", version="y")  # fmt: skip
    robot = replace(ctx(shop.a, sender(shop)), actor_type=ActorType.AI_AGENT)
    with tenant_scope(robot) as tenant, pytest.raises(AccessDenied):  # la IA no responde
        answers.accept_quote(tenant, quote_id=made.pk, version=2)
    send_own = plain(shop, {**SEND, "quotes.send": "OWN", "quotes.view": "ORGANIZATION"})
    view_own = plain(shop, {**SEND, "quotes.view": "OWN"})
    with tenant_scope(ctx(shop.b)):
        give(shop.b, Membership.objects.get(user=shop.luis).pk,
             {**SEND, "quotes.view": "ORGANIZATION"})  # fmt: skip
    seen = set()
    cases = ((shop.a, send_own, made.pk), (shop.a, view_own, made.pk),
             (shop.a, send_own, uuid4()), (shop.b, shop.luis, made.pk))  # fmt: skip
    for org, who, target in cases:
        for command, extra in ((answers.accept_quote, {}),
                               (answers.reject_quote_by_customer, {"reason": "No"})):  # fmt: skip
            with tenant_scope(ctx(org, who)) as tenant, pytest.raises(Quote.DoesNotExist) as caught:
                command(tenant, quote_id=target, version=9, **extra)
            seen.add((type(caught.value), str(caught.value), caught.value.args))
    assert len(seen) == 1 and reread(shop, made).version == 2
    with pytest.raises(StaleVersion):  # quien sí la alcanza sabe que su versión es vieja
        accept(shop, sender(shop), made, version=9)


def test_only_an_issued_quote_is_answered(shop: SimpleNamespace) -> None:
    seller = sender(shop, **{"quotes.cancel": "ORGANIZATION"})
    made = draft(shop, seller)[0]
    with pytest.raises(NotAnswerable):
        accept(shop, seller, made, version=1)
    act(shop, seller, commands.cancel_quote, quote_id=made.pk, version=1)
    with pytest.raises(NotAnswerable):
        decline(shop, seller, made, "No", version=2)


def test_the_validity_ends_at_valid_until_by_both_clocks(
    shop: SimpleNamespace, migrator: DB, monkeypatch: pytest.MonkeyPatch
) -> None:
    seller = sender(shop)
    made, late = sold(shop, seller), sold(shop, seller)
    until = reread(shop, made).valid_until
    clock(monkeypatch, until)  # justo en el límite ya venció: `<=`
    for command, extra in ((answers.accept_quote, {}),
                           (answers.reject_quote_by_customer, {"reason": "No"})):  # fmt: skip
        with pytest.raises(QuoteExpired):
            act(shop, seller, command, quote_id=made.pk, version=2, **extra)
    clock(monkeypatch, until - timedelta(microseconds=1))
    assert accept(shop, seller, made).accepted_at == until - timedelta(microseconds=1)
    lapse(migrator, late)  # la base ya la ve vencida y la aplicación todavía no
    clock(monkeypatch, reread(shop, late).valid_until - timedelta(microseconds=1))
    before = len(audit(migrator))
    with pytest.raises(QuoteExpired) as caught:  # no un 500 ni el texto de la base
        accept(shop, seller, late)
    assert caught.value.args == (late.pk,) and reread(shop, late).status == "SENT"
    assert len(audit(migrator)) == before


@pytest.mark.parametrize("case", ["forbidden:view", "forbidden:update", "no_stage", "missing",
                                  "missing_fields", "bad_reference", "same_stage", "closed",
                                  "closing_stage"])  # fmt: skip
def test_an_opportunity_that_cannot_move_leaves_the_quote_accepted_and_says_why(
    shop: SimpleNamespace, migrator: DB, paying: Any, case: str
) -> None:
    grants = {**OPPS, f"opportunities.{case[10:]}": "OWN"} if ":" in case else OPPS  # no es suya
    seller, opp = sender(shop, **grants), tables.make(shop.a)
    made = sold(shop, seller, opp)
    fix = {
        "no_stage": ("UPDATE pipeline_stages SET is_active = false WHERE id = %s", paying.pk),
        "missing_fields": ("UPDATE pipeline_stages SET requires_fields = '{\"amount\": true}'"
                           " WHERE id = %s", paying.pk),
        "missing": ("UPDATE opportunities SET deleted_at = now() WHERE id = %s", opp.pk),
        "bad_reference": ("UPDATE pipelines SET is_active = false, is_default = false"
                          " WHERE id = %s", opp.pipeline_id),
        "same_stage": ("UPDATE opportunities SET stage_id = %s WHERE id = %s", paying.pk, opp.pk),
        "closing_stage": ("UPDATE pipeline_stages SET stage_type = 'LOST', default_probability = 0"
                          " WHERE id = %s", paying.pk),
    }.get(case)  # fmt: skip
    if case == "closed":  # ganada
        with tenant_scope(ctx(shop.a, seller)) as tenant:
            win_opportunity(tenant, opportunity_id=opp.pk, version=1, amount="100")
    if fix:
        migrator.execute(fix[0], list(fix[1:]))
    stage = where(shop, opp)
    assert accept(shop, seller, made).status == "ACCEPTED"
    assert last(migrator)[2] == {"opportunity_id": str(opp.pk),
                                 "opportunity_move": f"skipped:{case.split(':')[0]}"}  # fmt: skip
    assert where(shop, opp) == stage  # la oportunidad no cambió


@pytest.mark.parametrize("failing", ["record", "_event", "move_opportunity", "move_opportunity:db"])
def test_any_other_failure_while_accepting_writes_nothing(
    shop: SimpleNamespace, migrator: DB, monkeypatch: pytest.MonkeyPatch, paying: Any,
    failing: str,
) -> None:  # fmt: skip
    seller, opp = sender(shop, **OPPS), tables.make(shop.a)
    made = sold(shop, seller, opp)
    before = len(audit(migrator))
    error = IntegrityError if ":" in failing else DatabaseError  # de `deals`: tal cual, sin mapear
    monkeypatch.setattr(answers, failing.split(":")[0], Mock(side_effect=error("boom")))
    with pytest.raises(error, match="boom"):
        accept(shop, seller, made)
    after = reread(shop, made)
    assert (after.status, after.version, after.accepted_at) == ("SENT", 2, None)
    assert len(audit(migrator)) == before and events(migrator, made) == ["quote.issued"]
    assert where(shop, opp) == (tables.stage(shop.a).pk, 1)


def test_two_answers_at_once_leave_one(shop: SimpleNamespace) -> None:
    seller = sender(shop)
    for rival in (answers.accept_quote, answers.reject_quote_by_customer):
        made = sold(shop, seller)
        extra = {} if rival is answers.accept_quote else {"reason": "No"}
        first, second = race(shop.a, seller, [
            lambda t, m=made: answers.accept_quote(t, quote_id=m.pk, version=2),
            lambda t, m=made, r=rival, e=extra: r(t, quote_id=m.pk, version=2, **e),
        ])  # fmt: skip
        assert (first.status, second) == ("ACCEPTED", "StaleVersion")
        assert (reread(shop, made).status, reread(shop, made).version) == ("ACCEPTED", 3)


def test_accepting_and_a_move_of_the_opportunity_at_once_never_deadlock(
    shop: SimpleNamespace, paying: Any
) -> None:
    seller = sender(shop, **OPPS)
    quoted = tables.stage(shop.a, "cotizado").pk
    opp = tables.make(shop.a)
    made = sold(shop, seller, opp)
    first, second = race(shop.a, seller, [  # el pipeline y la oportunidad, ya bloqueados
        lambda t: move_opportunity(t, opportunity_id=opp.pk, version=1, stage_id=quoted),
        lambda t: answers.accept_quote(t, quote_id=made.pk, version=2),
    ])  # fmt: skip
    assert (second.status, where(shop, opp)) == ("ACCEPTED", (paying.pk, 3))
    opp = tables.make(shop.a)
    made = sold(shop, seller, opp)
    first, second = race(shop.a, seller, [  # al revés: la cotización primero
        lambda t: answers.accept_quote(t, quote_id=made.pk, version=2),
        lambda t: move_opportunity(t, opportunity_id=opp.pk, version=1, stage_id=quoted),
    ])  # fmt: skip
    assert (first.status, second, where(shop, opp)) == ("ACCEPTED", "StaleVersion", (paying.pk, 2))


# --- Vencer ---------------------------------------------------------------------------------------


def test_a_lapsed_quote_expires_once_as_the_system(
    shop: SimpleNamespace, migrator: DB, monkeypatch: pytest.MonkeyPatch
) -> None:
    seller = sender(shop)
    made, alive = sold(shop, seller), sold(shop, seller)
    lapse(migrator, made)
    assert tasks.expire_quotes(str(shop.a)) == {"expired": 1, "skipped": 0, "failed": 0}
    assert (reread(shop, made).status, reread(shop, alive).status) == ("EXPIRED", "SENT")
    assert last(migrator)[:2] == ("quote.expired", "SYSTEM")
    assert events(migrator, made)[-1] == "quote.expired"
    assert tasks.expire_quotes(str(shop.a))["expired"] == 0  # una sola vez
    with tenant_scope(ctx(shop.a)) as tenant:
        assert answers.expire_quote(tenant, made.pk) is False
    until = reread(shop, alive).valid_until
    clock(monkeypatch, until)  # la aplicación ya la ve vencida y la base todavía no: no es aún
    with tenant_scope(ctx(shop.a)) as tenant:
        assert answers.expire_quote(tenant, alive.pk) is False
    lapse(migrator, alive)
    clock(monkeypatch, reread(shop, alive).valid_until - timedelta(microseconds=1))
    with tenant_scope(ctx(shop.a)) as tenant:
        assert answers.expire_quote(tenant, alive.pk) is False  # la aplicación todavía no
    clock(monkeypatch, reread(shop, alive).valid_until)  # justo en el límite: `<=`
    with tenant_scope(ctx(shop.a)) as tenant:
        assert answers.expire_quote(tenant, alive.pk) is True


def test_the_expiry_task_trusts_only_active_organizations_and_its_own(
    shop: SimpleNamespace, migrator: DB, monkeypatch: pytest.MonkeyPatch
) -> None:
    made = sold(shop, sender(shop))
    lapse(migrator, made)
    assert tasks.expire_quotes(str(shop.b)) == {"expired": 0, "skipped": 0, "failed": 0}
    migrator.execute("UPDATE organizations SET status = 'SUSPENDED' WHERE id = %s", [shop.a])
    assert tasks.expire_quotes(str(shop.a))["expired"] == 0  # el mensaje no la activa
    queued: list[dict[str, Any]] = []
    monkeypatch.setattr(tasks.expire_quotes, "apply_async", lambda **kw: queued.append(kw))
    assert tasks.expire_sweep()["enqueued"] == 1  # solo B, que sigue activa
    assert queued == [{"kwargs": {"organization_id": str(shop.b)}, "expires": 300}]
    migrator.execute("UPDATE organizations SET status = 'ACTIVE' WHERE id = %s", [shop.a])
    monkeypatch.setattr(answers, "expire_quote", lambda *a: 1 / 0)  # un fallo no para el resto
    assert tasks.expire_quotes(str(shop.a), max_per_run=10**6)["failed"] == 1
    assert reread(shop, made).status == "SENT"


def test_an_answer_and_the_expiry_at_once_leave_one(
    shop: SimpleNamespace, migrator: DB, monkeypatch: pytest.MonkeyPatch
) -> None:
    seller = sender(shop)
    made = sold(shop, seller)
    lapse(migrator, made)
    first, second = race(shop.a, [None, seller], [  # el sistema vence; una persona acepta
        lambda t: answers.expire_quote(t, made.pk),
        lambda t: answers.accept_quote(t, quote_id=made.pk, version=2),
    ])  # fmt: skip
    assert (first, second, reread(shop, made).status) == (True, "StaleVersion", "EXPIRED")
    with tenant_scope(ctx(shop.a, seller)) as tenant, pytest.raises(NotAllowed):
        answers.expire_quote(tenant, made.pk)  # una persona no vence
    made, late, real = sold(shop, seller), threading.local(), timezone.now
    shifted = SimpleNamespace(now=lambda: real() + timedelta(days=60 * getattr(late, "on", 0)))
    monkeypatch.setattr(answers, "timezone", shifted)
    first, second = race(shop.a, [seller, None], [  # el que vence ve la validez pasada, y espera
        lambda t: answers.accept_quote(t, quote_id=made.pk, version=2),
        lambda t: expire_late(late, t, made.pk),
    ])  # fmt: skip
    assert (first.status, second, reread(shop, made).status) == ("ACCEPTED", False, "ACCEPTED")


def expire_late(late: threading.local, tenant: Any, pk: Any) -> bool:
    late.on = True  # solo este hilo ve la validez pasada
    return answers.expire_quote(tenant, pk)


# --- La base (`crm_app`, SQL directo) -------------------------------------------------------------


def test_an_answer_is_born_empty_and_written_only_when_answering(world: SimpleNamespace) -> None:
    born: list[tuple[dict[str, Any], str]] = [
        ({"accepted_at": timezone.now()}, "quotes_accepted_ck"),
        ({"rejection_reason": "x"}, "quotes_rejected_ck"),
    ]
    for over, name in born:
        with pytest.raises(IntegrityError) as raised:
            quote(world, number="COT-940001", **over)
        assert constraint(raised) == name
    made = quote(world)
    go(world, made, "SENT")
    change = "UPDATE quotes SET {} WHERE id = %s"
    for assignment, name in (
        ("accepted_at = now()", "quotes_answer_frozen"),
        ("rejection_reason = 'x'", "quotes_answer_frozen"),
        ("status = 'ACCEPTED'", "quotes_accepted_ck"),  # sin fecha
        ("status = 'ACCEPTED', accepted_at = now(), rejected_at = now()", "quotes_rejected_ck"),
        ("status = 'REJECTED', rejected_at = now(), rejection_reason = ' '", "quotes_rejected_ck"),
        ("status = 'REJECTED', rejected_at = now()", "quotes_rejected_ck"),  # sin motivo
        ("status = 'ACCEPTED', accepted_at = valid_until", "quotes_accepted_ck"),
        ("status = 'EXPIRED'", "quotes_not_expired"),  # aún vigente
    ):  # fmt: skip
        with pytest.raises(IntegrityError) as raised:
            sql(world.a, change.format(assignment), made.pk, user=world.ana)
        assert constraint(raised) == name
    with pytest.raises(IntegrityError) as raised:  # el sistema (o la IA) no responde
        with tenant_scope(ctx(world.a)), connection.cursor() as cursor:
            cursor.execute(change.format("status = 'ACCEPTED', accepted_at = now()"), [made.pk])
    assert constraint(raised) == "quotes_answer_by_person"
    sql(world.a, change.format("status = 'ACCEPTED', accepted_at = now()"), made.pk,
        user=world.ana)  # fmt: skip
    for assignment in ("accepted_at = now() - interval '1 second'", "accepted_at = NULL",
                       "rejection_reason = 'x'"):  # fmt: skip
        refused(world.a, "quotes_answer_frozen", change.format(assignment), made.pk)


def test_the_database_refuses_an_answer_after_the_validity_by_its_own_clock(
    world: SimpleNamespace, migrator: DB
) -> None:
    made = quote(world)
    go(world, made, "SENT")
    lapse(migrator, made, 0.001)
    with pytest.raises(IntegrityError) as raised:
        sql(world.a, "UPDATE quotes SET status = 'REJECTED', rejected_at = sent_at,"
            " rejection_reason = 'No' WHERE id = %s", made.pk, user=world.ana)  # fmt: skip
    assert constraint(raised) == "quotes_answer_expired"
    sql(world.a, "UPDATE quotes SET status = 'EXPIRED' WHERE id = %s", made.pk)
    approved = quote(world, number="COT-940002", valid_until=later())
    go(world, approved, "APPROVED")  # #716: el texto al cliente no cambia al emitir
    refused(world.a, "quotes_terms_frozen", "UPDATE quotes SET status = 'SENT', notes_to_customer"
            " = 'otra', company_snapshot = '{}', customer_snapshot = '{}', sent_at = now()"
            " WHERE id = %s", approved.pk)  # fmt: skip


def test_the_migration_only_adds_rules_and_goes_back_only_without_answers(
    world: SimpleNamespace, migrator: DB, monkeypatch: pytest.MonkeyPatch
) -> None:
    new = installed(migrator)
    assert new == MIGRATION.body(MIGRATION.GUARD)
    legacy = quote(world)  # antes de volver: el modelo ya lleva las columnas nuevas
    go(world, legacy, "SENT")
    with as_migrator():
        call_command("migrate", "quotes", "0005", verbosity=0)
    old = installed(migrator)
    assert old == MIGRATION.body(MIGRATION.PREVIOUS.GUARD)  # exactamente la de 0005
    removed = [x for x in difflib.ndiff(old.splitlines(), new.splitlines()) if x.startswith("- ")]
    assert removed == [] and old != new  # ninguna regla anterior se perdió
    migrator.execute("UPDATE quotes SET status = 'ACCEPTED' WHERE id = %s", [legacy.pk])
    with as_migrator(), pytest.raises(RuntimeError, match="1 accepted or rejected"):
        call_command("migrate", "quotes", verbosity=0)
    migrator.execute("UPDATE quotes SET status = 'CANCELLED', cancelled_at = now()")
    with as_migrator():
        call_command("migrate", "quotes", verbosity=0)
    assert installed(migrator) == new
    answered = quote(world, number="COT-950001")
    go(world, answered, "SENT")
    sql(world.a, "UPDATE quotes SET status = 'ACCEPTED', accepted_at = now() WHERE id = %s",
        answered.pk, user=world.ana)  # fmt: skip
    with as_migrator(), pytest.raises(RuntimeError, match="refusing to go back"):
        call_command("migrate", "quotes", "0005", verbosity=0)
    assert installed(migrator) == new
    with connection.schema_editor() as editor:
        with pytest.raises(RuntimeError, match="not the expected one"):
            MIGRATION._replace(None, editor)  # instalada otra que la de 0005: no reemplaza nada
        for check in (MIGRATION._refuse_legacy, MIGRATION._refuse_answered):  # ida y vuelta
            with pytest.raises(RuntimeError, match="BYPASSRLS"):  # `crm_app` no ve todas
                check(None, editor)
    monkeypatch.setattr(MIGRATION.PREVIOUS, "GUARD", "otra función")
    with pytest.raises(RuntimeError, match="anchor"):  # si 0005 cambiara, no añade nada
        MIGRATION._guard()
