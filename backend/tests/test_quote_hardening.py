"""F5-09c (corte 1): la autoridad de `create_quote`, `update_quote` y `cancel_quote` (permiso,
bloqueo, alcance, versión y estado, en ese orden), cancelar lo que espera aprobación, lo que la IA
escribe y el suelo de costo con los costos programados, con PostgreSQL real (RLS, `crm_app`,
disparadores) y transacciones a la vez."""

import json
import logging
import threading
import time
from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from django.db import IntegrityError, connection
from django.utils import timezone

from apps.accounts.models import User
from apps.organizations.models import OrganizationMembership as Membership
from apps.pricing import costs
from apps.quotes import commands, decisions, discounts
from apps.quotes.commands import NotAllowed, QuoteTerminal, StaleVersion
from apps.quotes.models import Quote, QuoteApproval
from core.tenancy.scope import tenant_scope
from tests.factories import make_user
from tests.test_authorization import give
from tests.test_memberships import ctx, join
from tests.test_quote_commands import DB, audit, shop
from tests.test_quote_commands import world as world  # noqa: PLC0414
from tests.test_quote_decisions import asked, boss
from tests.test_quote_discounts import (
    act,
    approvals,
    cost,
    discount,
    draft,
    manual,
    member,
    raw,
    reread,
    submit,
)

_ = shop
pytestmark = pytest.mark.usefixtures("tenant_db")
CANCEL: dict[str, Any] = {"quotes.view": "ORGANIZATION", "quotes.cancel": "ORGANIZATION"}
SECRET = "Ana Perez, DNI 12345678"  # noqa: S105 — texto de prueba
ONE = [{"variant_id": uuid4(), "quantity": 1}]


def worker(shop: SimpleNamespace, percent: str | None = "10", **grants: Any) -> User:
    return member(shop, percent, **{**CANCEL, **grants})


def plain(shop: SimpleNamespace, grants: dict[str, Any]) -> User:
    """Un miembro con exactamente esos permisos (`member` añade los de redactar)."""
    user = make_user()
    give(shop.a, join(shop.a, user).pk, grants)
    return user


def events(migrator: DB, made: Quote) -> list[str]:
    found = migrator.execute(
        "SELECT event_type, payload::text FROM outbox_events WHERE aggregate_id = %s", [made.pk]
    ).fetchall()
    assert all(set(json.loads(p)) == {"quote_id", "contact_id", "opportunity_id", "status",
                                      "revision"} for _, p in found)  # fmt: skip
    return [kind for kind, _ in found]


def ai_draft(shop: SimpleNamespace, migrator: DB, who: User) -> Quote:
    """Un borrador de la IA: sin autor (lo escribe el dueño de la base, como un sistema)."""
    made, _, _ = draft(shop, who)
    migrator.execute("UPDATE quotes SET created_by_user_id = NULL WHERE id = %s", [made.pk])
    return made


def cancelling(shop: SimpleNamespace, who: User, made: Quote, version: int = 2, **kw: Any) -> Any:
    return act(shop, who, commands.cancel_quote, quote_id=made.pk, version=version, **kw)


def test_cancelling_a_quote_that_waits_for_approval_closes_its_request_in_the_same_transaction(
    shop: SimpleNamespace, migrator: DB
) -> None:
    made, seller = asked(shop, worker(shop, "3"))
    done = cancelling(shop, seller, made, reason=SECRET)
    assert (done.status, done.version, done.cancel_reason) == ("CANCELLED", 3, SECRET)
    [req] = approvals(shop)
    assert (req.status, req.decided_by_user_id, req.version) == ("CANCELLED", seller.pk, 2)
    assert req.decided_at is not None
    *_, (action, changes) = audit(migrator)
    assert action == "quote.cancelled" and changes["approval_status"] == ["PENDING", "CANCELLED"]
    assert changes["approval_id"] == [str(req.pk)] * 2 and SECRET not in json.dumps(changes)
    assert events(migrator, made) == ["quote.cancelled"]
    with pytest.raises(QuoteTerminal):  # y no se cancela dos veces
        cancelling(shop, seller, made, version=3)


def test_only_who_asked_or_who_can_approve_cancels_what_waits_for_approval(
    shop: SimpleNamespace, migrator: DB
) -> None:
    made, seller = asked(shop, worker(shop, "3"))
    with pytest.raises(NotAllowed):  # puede cancelar y verlo todo, pero no pidió ni aprueba
        cancelling(shop, worker(shop), made, version=99)  # y eso va antes que la versión
    assert (reread(shop, made).status, [a.status for a in approvals(shop)]) == (
        "PENDING_APPROVAL", ["PENDING"],
    )  # fmt: skip
    mine = ai_draft(shop, migrator, seller)  # la solicitud de la IA no tiene a quien la pidió
    submit(shop, seller, mine)
    with pytest.raises(NotAllowed):
        cancelling(shop, seller, mine)
    approver = boss(shop, "10", **{"quotes.cancel": "ORGANIZATION"})
    assert [cancelling(shop, approver, q).status for q in (made, mine)] == ["CANCELLED"] * 2
    assert {a.status for a in approvals(shop)} == {"CANCELLED"}


def test_the_database_refuses_to_cancel_a_quote_with_an_open_request(shop: SimpleNamespace) -> None:
    made, seller = asked(shop, worker(shop, "3"))
    with pytest.raises(IntegrityError, match="decide the open request first"):
        raw(shop, seller, made, "CANCELLED")


@pytest.mark.parametrize("failing", ["record", "_event"])
def test_a_failure_while_cancelling_leaves_the_request_and_the_quote_as_they_were(
    shop: SimpleNamespace, migrator: DB, monkeypatch: pytest.MonkeyPatch, failing: str
) -> None:
    made, seller = asked(shop, worker(shop, "3"))
    before = len(audit(migrator))

    def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(commands, failing, boom)
    with pytest.raises(RuntimeError, match="boom"):
        cancelling(shop, seller, made)
    monkeypatch.undo()
    assert [a.status for a in approvals(shop)] == ["PENDING"] and len(audit(migrator)) == before
    assert (reread(shop, made).status, reread(shop, made).version) == ("PENDING_APPROVAL", 2)


@pytest.mark.parametrize(
    ("command", "code", "extra"),
    [(commands.create_quote, "quotes.create", {"items": ONE, "contact_id": uuid4()}),
     (commands.update_quote, "quotes.update", {"internal_notes": "x"}),
     (commands.cancel_quote, "quotes.cancel", {})],
)  # fmt: skip
def test_without_the_permission_of_the_action_nothing_is_read_and_a_random_id_answers_the_same(
    shop: SimpleNamespace, command: Any, code: str, extra: dict[str, Any]
) -> None:
    made, _, _ = draft(shop, worker(shop))
    nobody = plain(shop, {"quotes.view": "ORGANIZATION"})  # ve todas, sin ese permiso
    seen = set()
    for target in (made.pk, uuid4()):
        ids = {} if command is commands.create_quote else {"quote_id": target, "version": 9}
        with pytest.raises(NotAllowed) as caught:
            act(shop, nobody, command, **ids, **extra)
        seen.add((str(caught.value), caught.value.args))
    assert seen == {(code, (code,))} and reread(shop, made).version == 1


@pytest.mark.parametrize(("view", "act_on"), [("OWN", "OWN"), ("ORGANIZATION", "OWN"),
                                              ("OWN", "ORGANIZATION")])  # fmt: skip
@pytest.mark.parametrize(
    ("command", "code", "extra"),
    [(commands.update_quote, "quotes.update", {"internal_notes": "x"}),
     (commands.cancel_quote, "quotes.cancel", {}),
     (discounts.submit_quote, "quotes.update", {}),
     (discounts.set_line_discount, "quotes.update", {"line_id": uuid4(), "discount_type": "NONE"}),
     (discounts.set_manual_price, "quotes.update",
      {"line_id": uuid4(), "price": "1", "reason": "x"})],
)  # fmt: skip
def test_the_permission_does_not_give_the_scope_and_a_stale_version_is_not_told_outside_it(
    shop: SimpleNamespace, command: Any, code: str, extra: dict[str, Any], view: str, act_on: str
) -> None:
    extra_codes: dict[Any, str] = {discounts.set_line_discount: "quotes.discount",
                                   discounts.set_manual_price: "quotes.override_price"}  # fmt: skip
    extra_code = extra_codes.get(command)
    plus = {extra_code: None} if extra_code else {}  # el permiso propio de la acción, sin alcance
    grants = {code: act_on, "quotes.view": view, **plus}  # ver sin poder cambiar, o al revés
    made = draft(shop, worker(shop))[0]  # de otra persona
    mine = plain(shop, grants)
    with tenant_scope(ctx(shop.b)):
        give(shop.b, Membership.objects.get(user=shop.luis).pk,
             {**CANCEL, "quotes.update": "ORGANIZATION", **plus})  # fmt: skip
    seen = set()
    for org, who, target in ((shop.a, mine, made.pk), (shop.a, mine, uuid4()),
                             (shop.b, shop.luis, made.pk)):  # fmt: skip
        with tenant_scope(ctx(org, who)) as tenant, pytest.raises(Quote.DoesNotExist) as caught:
            command(tenant, quote_id=target, version=9, **extra)  # la versión 9 no es la
        seen.add((type(caught.value), str(caught.value), caught.value.args))
    assert len(seen) == 1 and reread(shop, made).version == 1
    with pytest.raises(StaleVersion):  # quien sí la alcanza, sí
        act(shop, worker(shop, "10", **{"quotes.update": "ORGANIZATION", **plus}), command,
            quote_id=made.pk, version=9, **extra)  # fmt: skip


def test_a_quote_out_of_scope_does_not_wait_for_whoever_holds_its_lock(
    shop: SimpleNamespace,
) -> None:
    made, who = asked(shop, worker(shop, "3"))
    mine = worker(shop, "10", **{"quotes.view": "OWN", "quotes.cancel": "OWN"})
    held, release = threading.Event(), threading.Event()

    def hold() -> None:
        try:
            with tenant_scope(ctx(shop.a, who)):
                Quote.objects.select_for_update().get(pk=made.pk)
                held.set()
                release.wait(timeout=10)
        finally:
            held.set()
            connection.close()

    holder = threading.Thread(target=hold)
    holder.start()
    assert held.wait(timeout=10)
    timer = threading.Timer(8, release.set)
    timer.start()
    started = time.monotonic()
    with pytest.raises(Quote.DoesNotExist):
        cancelling(shop, mine, made)
    waited = time.monotonic() - started
    timer.cancel()
    release.set()
    holder.join(timeout=10)
    assert waited < 3  # no esperó: el tiempo no dice si la cotización existe


def test_what_the_ai_wrote_always_asks_for_a_person_to_approve_it(
    shop: SimpleNamespace, migrator: DB
) -> None:
    who = worker(shop)
    made = ai_draft(shop, migrator, who)
    assert submit(shop, who, made).status == "PENDING_APPROVAL"  # sin descuento ni precio manual
    [asked_by_ai] = approvals(shop)
    assert (asked_by_ai.reason, asked_by_ai.requested_by_user_id) == ("AI_CREATED", None)
    done = act(shop, boss(shop), decisions.approve_quote, quote_id=made.pk, version=2)
    assert done.status == "APPROVED"  # y la decide una persona


@pytest.mark.parametrize("scheduled", [False, True])
def test_the_floor_takes_the_largest_cost_the_current_one_and_the_ones_scheduled(
    shop: SimpleNamespace, migrator: DB, scheduled: bool
) -> None:
    pricer = worker(shop, "100", **{"quotes.override_price": None})
    cost(shop, shop.iphone, "1000")
    made, phone, _ = draft(shop, pricer)
    manual(shop, pricer, made, phone, "3500")  # cubre 1000
    if scheduled:  # un costo de 4000 programado dentro de tres días
        when = timezone.now() + timedelta(days=3)
        migrator.execute("UPDATE variant_costs SET ends_at = %s WHERE variant_id = %s",
                         [when, shop.iphone])  # fmt: skip
        cost(shop, shop.iphone, "4000", starts_at=when, source="IMPORT")
    else:
        act(shop, shop.ana, costs.set_cost, variant_id=shop.iphone, amount="4000", reason="Subió")
    with pytest.raises(discounts.PriceRefused) as refused:
        act(shop, boss(shop, "100"), decisions.approve_quote, quote_id=made.pk, version=2)
    assert refused.value.args == () and (reread(shop, made).status, len(approvals(shop))) == (
        "PENDING_APPROVAL", 1,
    )  # fmt: skip


@pytest.mark.parametrize(
    ("command", "extra"),
    [(commands.create_quote, {"items": "no es una lista", "contact_id": "x"}),
     (commands.update_quote, {"quote_id": "x", "version": "y", "items": 3}),
     (commands.cancel_quote, {"quote_id": "x", "version": "y", "reason": 7}),
     (discounts.set_line_discount, {"quote_id": "x", "version": "y", "line_id": "z",
                                    "discount_type": "NOPE", "discount_value": 1.5}),
     (discounts.set_manual_price, {"quote_id": "x", "version": "y", "line_id": "z", "price": 1.5,
                                   "reason": 7}),
     (discounts.submit_quote, {"quote_id": "x", "version": "y"})],
)  # fmt: skip
def test_without_permission_even_malformed_input_is_a_permission_error(
    command: Any, extra: dict[str, Any], shop: SimpleNamespace
) -> None:
    nobody = plain(shop, {"quotes.view": "ORGANIZATION"})
    with pytest.raises(NotAllowed):  # no un `ValueError` que cuente cómo se valida
        act(shop, nobody, command, **extra)


def test_the_one_who_submitted_what_the_ai_wrote_cannot_approve_it_either(
    shop: SimpleNamespace, migrator: DB
) -> None:
    who = boss(shop, "10", **{"quotes.update": "ORGANIZATION"})  # envía y también puede aprobar
    made = ai_draft(shop, migrator, who)
    assert submit(shop, who, made).status == "PENDING_APPROVAL"
    [req] = approvals(shop)
    assert (req.requested_by_user_id, req.submitted_by_user_id) == (None, who.pk)
    with pytest.raises(decisions.SelfApproval):
        act(shop, who, decisions.approve_quote, quote_id=made.pk, version=2)
    with tenant_scope(ctx(shop.a, who)), pytest.raises(IntegrityError, match="separation"):
        QuoteApproval.objects.filter(pk=req.pk).update(  # y por SQL directo, tampoco
            status="APPROVED", decided_by_user_id=who.pk, decided_at=timezone.now()
        )
    other = boss(shop, "10")
    with tenant_scope(ctx(shop.a, who)), pytest.raises(IntegrityError, match="never changes"):
        QuoteApproval.objects.filter(pk=req.pk).update(submitted_by_user_id=other.pk)
    assert act(shop, other, decisions.approve_quote, quote_id=made.pk, version=2).status == (
        "APPROVED"
    )


def constraint(raised: pytest.ExceptionInfo[IntegrityError]) -> str:
    return str(raised.value.__cause__.diag.constraint_name)  # type: ignore[union-attr]


def test_the_database_binds_who_submits_to_who_acts_and_never_approves_without_it(
    shop: SimpleNamespace, migrator: DB
) -> None:
    seller, other = worker(shop, "3"), worker(shop, "3")
    made, _, _ = draft(shop, seller)
    for actor, submitter in ((seller, None), (seller, other.pk), (None, None)):  # ni sin persona
        with tenant_scope(ctx(shop.a, actor)), pytest.raises(IntegrityError) as raised:
            QuoteApproval.objects.create(
                quote_id=made.pk, reason="DISCOUNT_OVER_LIMIT", requested_by_user_id=seller.pk,
                submitted_by_user_id=submitter, requested_discount_percent=5,
                limit_percent_at_request=3,
            )  # fmt: skip
        assert constraint(raised) == "quote_approvals_submitter"
    old, _ = asked(shop, seller)
    migrator.execute("UPDATE quote_approvals SET submitted_by_user_id = NULL")  # anterior a 0004
    approver = boss(shop, "10", **{"quotes.cancel": "ORGANIZATION"})
    with pytest.raises(decisions.SelfApproval):
        act(shop, approver, decisions.approve_quote, quote_id=old.pk, version=2)
    [req] = approvals(shop)
    with tenant_scope(ctx(shop.a, approver)), pytest.raises(IntegrityError) as raised:
        QuoteApproval.objects.filter(pk=req.pk).update(
            status="APPROVED", decided_by_user_id=approver.pk, decided_at=timezone.now()
        )
    assert constraint(raised) == "quote_approvals_submitter"
    assert cancelling(shop, approver, old).status == "CANCELLED"  # se cancela y se vuelve a pedir


def test_a_database_rule_without_a_name_in_the_commands_is_refused_bare_and_logged(
    shop: SimpleNamespace, caplog: pytest.LogCaptureFixture
) -> None:
    senior = worker(shop, "10")
    made, phone, _ = draft(shop, senior)
    discount(shop, senior, made, phone, "PERCENT", "5")  # cabe en su 10 %, no en el 3 %
    with pytest.raises(IntegrityError) as raised:
        raw(shop, worker(shop, "3"), made, "APPROVED")
    with caplog.at_level(logging.WARNING, logger="apps.quotes.commands"):
        refused = commands._reference(raised.value)
    assert type(refused) is commands.QuoteRefused and refused.args == ()
    assert "quotes_needs_request_path" in caplog.text  # el nombre, solo en el servidor


def test_a_pending_quote_without_an_open_request_is_not_cancelled_by_anyone(
    shop: SimpleNamespace, migrator: DB
) -> None:
    made, seller = asked(shop, worker(shop, "3"))
    migrator.execute("UPDATE quote_approvals SET status = 'CANCELLED', decided_at = now()")
    with pytest.raises(NotAllowed):  # no queda a quién pedir permiso: se cierra, no se salta
        cancelling(shop, seller, made)
    assert reread(shop, made).status == "PENDING_APPROVAL"


@pytest.mark.parametrize("failing", ["_stamp"])
@pytest.mark.parametrize("name", ["cancel", "update"])
def test_a_rule_of_the_database_the_commands_did_not_foresee_is_a_domain_error(
    shop: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, name: str, failing: str
) -> None:
    made, who = draft(shop, worker(shop))[0], worker(shop)

    def boom(*args: Any, **kwargs: Any) -> None:
        raise IntegrityError("una regla que nadie previó")

    monkeypatch.setattr(commands, failing, boom)
    with pytest.raises(commands.QuoteRefused):
        if name == "cancel":
            cancelling(shop, who, made, version=1)
        else:
            act(shop, who, commands.update_quote, quote_id=made.pk, version=1, internal_notes="x")


@pytest.mark.parametrize("scheduled", [False, True])
def test_a_cost_in_another_currency_refuses_the_price_without_saying_why(
    shop: SimpleNamespace, migrator: DB, scheduled: bool
) -> None:
    pricer = worker(shop, "100", **{"quotes.override_price": None})
    cost(shop, shop.iphone, "1000")
    made, phone, _ = draft(shop, pricer)
    if scheduled:  # el vigente en soles, el programado en dólares
        when = timezone.now() + timedelta(days=3)
        migrator.execute("UPDATE variant_costs SET ends_at = %s WHERE variant_id = %s",
                         [when, shop.iphone])  # fmt: skip
        cost(shop, shop.iphone, "10", starts_at=when, source="IMPORT", currency="USD")
    else:
        migrator.execute("UPDATE variant_costs SET currency = 'USD' WHERE variant_id = %s",
                         [shop.iphone])  # fmt: skip
    with pytest.raises(discounts.PriceRefused) as refused:
        manual(shop, pricer, made, phone, "3500")
    assert refused.value.args == ()
