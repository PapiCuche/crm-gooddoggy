"""F5-09c: cancelar, emitir, aceptar y rechazar una cotización (`apps.quotes.commands` y
`apps.quotes.lifecycle`) y lo que la base impone a la emitida, con PostgreSQL real (RLS, `crm_app`,
disparadores) y transacciones a la vez."""

import difflib
import json
import threading
import time
from datetime import timedelta
from functools import partial
from importlib import import_module
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from django.apps import apps as django_apps
from django.conf import settings
from django.db import IntegrityError, connection
from django.utils import timezone

from apps.accounts.models import User
from apps.contacts.commands import delete_contact
from apps.organizations.models import OrganizationMembership as Membership
from apps.pricing import costs
from apps.quotes import commands, decisions, discounts, lifecycle
from apps.quotes.commands import BadReference, NotAllowed, QuoteTerminal, StaleVersion
from apps.quotes.lifecycle import NeedsApproval, WrongStatus
from apps.quotes.models import Quote
from core.tenancy.scope import tenant_scope
from tests.factories import make_user
from tests.test_authorization import give
from tests.test_inventory_stock import race
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
from tests.test_quote_tables import facts, quote

_ = shop
pytestmark = pytest.mark.usefixtures("tenant_db")
SEND: dict[str, Any] = {
    "quotes.send": "ORGANIZATION", "quotes.view": "ORGANIZATION", "contacts.view": "ORGANIZATION",
    "quotes.cancel": "ORGANIZATION",
}  # fmt: skip
SECRET = "Ana Perez, DNI 12345678"  # noqa: S105 — texto de prueba
LATER = timezone.now() + timedelta(days=30)


def sender(shop: SimpleNamespace, percent: str | None = "10", **grants: Any) -> User:
    return member(shop, percent, **{**SEND, **grants})


def approved(shop: SimpleNamespace, who: User | None = None) -> tuple[Quote, User]:
    """Una cotización aprobada (versión 2) de `who`: sin descuento, dentro de su límite."""
    who = who or sender(shop)
    made, _, _ = draft(shop, who)
    assert submit(shop, who, made).status == "APPROVED"
    return made, who


def issue(shop: SimpleNamespace, who: User, made: Quote, version: int = 2, **fields: Any) -> Any:
    fields = {"valid_until": LATER, **fields}
    return act(shop, who, lifecycle.issue_quote, quote_id=made.pk, version=version, **fields)


def sent(shop: SimpleNamespace) -> tuple[Quote, User]:
    made, who = approved(shop)
    assert issue(shop, who, made).status == "SENT"
    return made, who


def events(migrator: DB, made: Quote) -> list[tuple[str, dict[str, Any]]]:
    found = migrator.execute(
        "SELECT event_type, payload::text FROM outbox_events WHERE aggregate_id = %s"
        " ORDER BY occurred_at, id", [made.pk]).fetchall()  # fmt: skip
    return [(kind, json.loads(payload)) for kind, payload in found]


def actions(migrator: DB) -> list[str]:
    return [a for a, _ in audit(migrator)]


def plain(shop: SimpleNamespace, grants: dict[str, Any], org: Any = None) -> User:
    """Un miembro con exactamente esos permisos (`member` añade los de redactar)."""
    user, org = make_user(), org or shop.a
    give(org, join(org, user).pk, grants)
    return user


def one_error(shop: SimpleNamespace, calls: list[Any]) -> set[Any]:
    """Los errores de varias llamadas que han de ser el mismo: tipo, texto y argumentos."""
    seen = set()
    for org, who, command, target, extra in calls:
        with tenant_scope(ctx(org, who)) as tenant, pytest.raises(Quote.DoesNotExist) as caught:
            command(tenant, quote_id=target, version=9, **extra)
        seen.add((type(caught.value), str(caught.value), caught.value.args))
    return seen


def ai_draft(shop: SimpleNamespace, migrator: DB, who: User) -> Quote:
    """Un borrador de la IA: sin autor (lo escribe el dueño de la base, como un sistema)."""
    made, _, _ = draft(shop, who)
    migrator.execute("UPDATE quotes SET created_by_user_id = NULL WHERE id = %s", [made.pk])
    return made


# ---- cancelar, crear y cambiar: permiso, alcance, versión y la solicitud abierta ----


def test_cancelling_a_quote_that_waits_for_approval_closes_its_request_in_the_same_transaction(
    shop: SimpleNamespace, migrator: DB
) -> None:
    made, seller = asked(shop, sender(shop, "3"))
    done = act(shop, seller, commands.cancel_quote, quote_id=made.pk, version=2, reason=SECRET)
    assert (done.status, done.version, done.cancel_reason) == ("CANCELLED", 3, SECRET)
    [req] = approvals(shop)
    assert (req.status, req.decided_by_user_id, req.version) == ("CANCELLED", seller.pk, 2)
    assert req.decided_at is not None
    *_, (action, changes) = audit(migrator)
    assert action == "quote.cancelled" and changes["approval_status"] == ["PENDING", "CANCELLED"]
    assert changes["approval_id"] == [str(req.pk)] * 2 and SECRET not in json.dumps(changes)
    assert [k for k, _ in events(migrator, made)] == ["quote.cancelled"]
    with pytest.raises(QuoteTerminal):  # y no se cancela dos veces
        act(shop, seller, commands.cancel_quote, quote_id=made.pk, version=3)


def test_only_who_asked_or_who_can_approve_cancels_what_waits_for_approval(
    shop: SimpleNamespace, migrator: DB
) -> None:
    made, seller = asked(shop, sender(shop, "3"))
    bystander = sender(shop, "10")  # puede cancelar y ver todo, pero no pidió ni aprueba
    with pytest.raises(NotAllowed):
        act(shop, bystander, commands.cancel_quote, quote_id=made.pk, version=2)
    assert (reread(shop, made).status, [a.status for a in approvals(shop)]) == (
        "PENDING_APPROVAL", ["PENDING"],
    )  # fmt: skip
    mine = ai_draft(shop, migrator, seller)  # la solicitud de la IA no tiene a quien la pidió
    submit(shop, seller, mine)
    with pytest.raises(NotAllowed):
        act(shop, seller, commands.cancel_quote, quote_id=mine.pk, version=2)
    approver = boss(shop, "10", **{"quotes.cancel": "ORGANIZATION"})
    for target in (made, mine):
        done = act(shop, approver, commands.cancel_quote, quote_id=target.pk, version=2)
        assert done.status == "CANCELLED"
    assert {a.status for a in approvals(shop)} == {"CANCELLED"}


def test_the_database_refuses_to_cancel_a_quote_with_an_open_request(shop: SimpleNamespace) -> None:
    made, seller = asked(shop, sender(shop, "3"))
    with pytest.raises(IntegrityError, match="decide the open request first"):
        raw(shop, seller, made, "CANCELLED")


@pytest.mark.parametrize("failing", ["record", "_event"])
def test_a_failure_while_cancelling_leaves_the_request_and_the_quote_as_they_were(
    shop: SimpleNamespace, migrator: DB, monkeypatch: pytest.MonkeyPatch, failing: str
) -> None:
    made, seller = asked(shop, sender(shop, "3"))
    before = len(audit(migrator))

    def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(commands, failing, boom)
    with pytest.raises(RuntimeError, match="boom"):
        act(shop, seller, commands.cancel_quote, quote_id=made.pk, version=2)
    monkeypatch.undo()
    assert [a.status for a in approvals(shop)] == ["PENDING"] and len(audit(migrator)) == before
    assert (reread(shop, made).status, reread(shop, made).version) == ("PENDING_APPROVAL", 2)


@pytest.mark.parametrize(
    ("command", "code", "extra"),
    [(commands.create_quote, "quotes.create", {"items": [{"variant_id": uuid4(), "quantity": 1}],
                                              "contact_id": uuid4()}),
     (commands.update_quote, "quotes.update", {"internal_notes": "x"}),
     (commands.cancel_quote, "quotes.cancel", {}),
     (lifecycle.issue_quote, "quotes.send", {})],
)  # fmt: skip
def test_without_the_permission_of_the_action_nothing_is_read_and_a_random_id_answers_the_same(
    shop: SimpleNamespace, command: Any, code: str, extra: dict[str, Any]
) -> None:
    made, _ = approved(shop)
    nobody = plain(shop, {"quotes.view": "ORGANIZATION"})  # ve todas, sin ese permiso
    seen = set()
    for target in (made.pk, uuid4()):
        ids = {} if command is commands.create_quote else {"quote_id": target, "version": 9}
        with pytest.raises(NotAllowed) as caught:
            act(shop, nobody, command, **ids, **extra)
        seen.add((str(caught.value), caught.value.args))
    assert seen == {(code, (code,))}
    assert reread(shop, made).version == 2


@pytest.mark.parametrize(
    ("command", "grants", "extra"),
    [(commands.update_quote, {"quotes.update": "OWN"}, {"internal_notes": "x"}),
     (commands.cancel_quote, {"quotes.cancel": "OWN"}, {}),
     (lifecycle.issue_quote, {"quotes.send": "OWN"}, {})],
)  # fmt: skip
def test_the_permission_does_not_give_the_scope_an_absent_a_foreign_and_a_hidden_quote_are_one(
    shop: SimpleNamespace, command: Any, grants: dict[str, Any], extra: dict[str, Any]
) -> None:
    made, _ = approved(shop)  # de otra persona
    mine = sender(shop, "10", **{**grants, "quotes.view": "OWN"})
    with tenant_scope(ctx(shop.b)):
        give(shop.b, Membership.objects.get(user=shop.luis).pk, {**SEND, **ALLOW_B})
    seen = one_error(shop, [
        (shop.a, mine, command, made.pk, extra),  # existe, pero no es suya
        (shop.a, mine, command, uuid4(), extra),  # no existe
        (shop.b, shop.luis, command, made.pk, extra),  # de otra organización
    ])  # fmt: skip
    assert len(seen) == 1 and reread(shop, made).version == 2  # y la versión 9 no lo cambia


ALLOW_B: dict[str, Any] = {"quotes.update": "ORGANIZATION", "quotes.cancel": "ORGANIZATION"}


def test_a_stale_version_is_seen_only_by_who_reaches_the_quote(shop: SimpleNamespace) -> None:
    made, who = approved(shop)
    with pytest.raises(StaleVersion):
        act(shop, who, commands.cancel_quote, quote_id=made.pk, version=1)
    with pytest.raises(StaleVersion):
        issue(shop, who, made, version=1)
    own = sender(shop, "10", **{"quotes.view": "OWN", "quotes.send": "OWN", "quotes.cancel": "OWN"})
    for command in (commands.cancel_quote, lifecycle.issue_quote):
        with pytest.raises(Quote.DoesNotExist):  # fuera de su alcance: ni siquiera la versión
            act(shop, own, command, quote_id=made.pk, version=1)
    assert reread(shop, made).status == "APPROVED"


# ---- emitir ----


def test_issuing_freezes_the_snapshots_the_validity_and_leaks_nothing_to_the_log(
    shop: SimpleNamespace, migrator: DB
) -> None:
    made, who = approved(shop)
    done = issue(shop, who, made, terms=f"  {SECRET} ", warranty="12 meses")
    assert (done.status, done.version, done.valid_until) == ("SENT", 3, LATER)
    assert done.sent_at is not None and set(done.company_snapshot) == {"name"}
    assert (done.terms_snapshot, done.warranty_snapshot) == (SECRET, "12 meses")
    assert done.customer_snapshot["contact_id"] == str(made.contact_id)
    assert set(done.customer_snapshot) == {
        "contact_id",
        "display_name",
        "document_type",
        "document_number",
    }  # lo mínimo: sin teléfono ni correo
    *_, (action, changes) = audit(migrator)
    assert action == "quote.issued" and changes["status"] == ["APPROVED", "SENT"]
    assert changes["snapshots"] == [None, "[congeladas]"]
    shown = json.dumps(changes) + json.dumps(events(migrator, made))
    assert SECRET not in shown and "12345678" not in shown
    assert done.customer_snapshot["display_name"] not in shown
    [(kind, payload)] = events(migrator, made)
    assert kind == "quote.issued" and set(payload) == {"quote_id", "contact_id", "opportunity_id",
                                                       "status", "revision"}  # fmt: skip


def test_a_draft_goes_out_only_inside_the_limit_of_who_issues_and_a_returned_one_does_not(
    shop: SimpleNamespace, migrator: DB
) -> None:
    careful, senior = sender(shop, "3"), sender(shop, "10")
    made, _, _ = draft(shop, senior)
    assert issue(shop, senior, made, version=1).status == "SENT"  # sin descuento, de una persona
    over, phone, _ = draft(shop, senior)
    discount(shop, senior, over, phone, "PERCENT", "5")  # cabe en el 10 % de senior
    with pytest.raises(NeedsApproval):  # no en el 3 % de careful: la base lo dejaría pasar
        issue(shop, careful, over, version=2)
    assert reread(shop, over).status == "DRAFT" and approvals(shop) == []
    assert issue(shop, senior, over, version=2).status == "SENT"
    back, phone, _ = draft(shop, careful)  # el descuento de una solicitud retirada sigue ahí
    discount(shop, careful, back, phone, "PERCENT", "5")  # 5 > 3: pidió aprobación
    act(shop, careful, decisions.withdraw_approval_request, quote_id=back.pk, version=2)
    with pytest.raises(NeedsApproval):
        issue(shop, careful, back, version=3)
    assert reread(shop, back).status == "DRAFT"
    assert actions(migrator).count("quote.issued") == 2


def rise(shop: SimpleNamespace, migrator: DB, later: bool) -> None:
    """El costo del iPhone sube a 4000: ya, o programado dentro de tres días."""
    if later:  # un costo programado a futuro cuenta: manda el mayor
        when = timezone.now() + timedelta(days=3)
        migrator.execute("UPDATE variant_costs SET ends_at = %s WHERE variant_id = %s",
                         [when, shop.iphone])  # fmt: skip
        cost(shop, shop.iphone, "4000", starts_at=when, source="IMPORT")
    else:
        act(shop, shop.ana, costs.set_cost, variant_id=shop.iphone, amount="4000", reason="Subió")


def manual_and_approved(shop: SimpleNamespace) -> tuple[Quote, User]:
    """Un precio manual de 3500 (el costo es 1000) aprobado por otra persona: versión 3."""
    pricer = sender(shop, "100", **{"quotes.override_price": None})
    cost(shop, shop.iphone, "1000")
    made, phone, _ = draft(shop, pricer)
    manual(shop, pricer, made, phone, "3500")
    act(shop, boss(shop, "100"), decisions.approve_quote, quote_id=made.pk, version=2)
    return made, pricer


@pytest.mark.parametrize("scheduled", [False, True])
def test_a_manual_price_goes_through_the_floor_again_before_it_goes_out(
    shop: SimpleNamespace, migrator: DB, scheduled: bool
) -> None:
    made, pricer = manual_and_approved(shop)
    rise(shop, migrator, scheduled)
    before = len(audit(migrator))
    with pytest.raises(discounts.PriceRefused) as refused:
        issue(shop, pricer, made, version=3)
    assert refused.value.args == () and "4000" not in str(refused.value)
    assert (reread(shop, made).status, len(audit(migrator))) == ("APPROVED", before)
    assert events(migrator, made) == []


def test_without_contacts_view_the_floor_is_not_an_oracle_for_the_issuer(
    shop: SimpleNamespace, migrator: DB
) -> None:
    made, pricer = manual_and_approved(shop)
    rise(shop, migrator, False)
    blind = plain(shop, {"quotes.send": "ORGANIZATION", "quotes.view": "ORGANIZATION"})
    with pytest.raises(BadReference) as refused:  # ni el costo ni el contacto se cuentan
        issue(shop, blind, made, version=3)
    assert refused.value.field == "contact_id"


def test_a_withdrawn_manual_price_does_not_go_out_as_a_draft(shop: SimpleNamespace) -> None:
    pricer = sender(shop, "100", **{"quotes.override_price": None})
    cost(shop, shop.iphone, "1000")
    made, phone, _ = draft(shop, pricer)
    manual(shop, pricer, made, phone, "3500")
    act(shop, pricer, decisions.withdraw_approval_request, quote_id=made.pk, version=2)
    with pytest.raises(NeedsApproval):  # cubre el costo, pero un precio manual siempre se aprueba
        issue(shop, pricer, made, version=3)
    assert reread(shop, made).status == "DRAFT"


def test_what_the_ai_wrote_asks_for_approval_and_goes_out_only_after_a_person_approved_it(
    shop: SimpleNamespace, migrator: DB
) -> None:
    who = sender(shop)
    made = ai_draft(shop, migrator, who)
    with pytest.raises(NeedsApproval):  # de borrador, no
        issue(shop, who, made, version=1)
    assert submit(shop, who, made).status == "PENDING_APPROVAL"  # `submit_quote` la pide
    [asked_by_ai] = approvals(shop)
    assert (asked_by_ai.reason, asked_by_ai.requested_by_user_id) == ("AI_CREATED", None)
    with pytest.raises(WrongStatus):
        issue(shop, who, made, version=2)
    done = act(shop, boss(shop), decisions.approve_quote, quote_id=made.pk, version=2)
    assert done.status == "APPROVED" and issue(shop, who, made, version=3).status == "SENT"


def test_the_database_closes_the_ai_path_without_an_approved_request(
    shop: SimpleNamespace, migrator: DB
) -> None:
    who = sender(shop)
    made = ai_draft(shop, migrator, who)
    for state in ("APPROVED", "SENT"):
        with pytest.raises(IntegrityError, match="needs an approved request"):
            raw(shop, who, made, state)  # de borrador, ni con un descuento dentro del límite
    migrator.execute("UPDATE quotes SET status = 'APPROVED' WHERE id = %s", [made.pk])
    with pytest.raises(IntegrityError, match="needs an approved request"):  # APPROVED sin solicitud
        raw(shop, who, made, "SENT")
    with pytest.raises(NeedsApproval):  # y el comando tampoco, aunque la base fuera permisiva
        issue(shop, who, made, version=1)


def test_what_the_ai_wrote_goes_out_through_the_database_once_a_request_was_approved(
    shop: SimpleNamespace, migrator: DB
) -> None:
    who = sender(shop)
    made = ai_draft(shop, migrator, who)
    submit(shop, who, made)
    act(shop, boss(shop), decisions.approve_quote, quote_id=made.pk, version=2)
    raw(shop, who, made, "SENT")  # la última solicitud está aprobada
    assert reread(shop, made).status == "SENT"


@pytest.mark.parametrize("until", [None, "past"])
def test_a_quote_needs_a_validity_in_the_future_and_nothing_is_written_without_it(
    shop: SimpleNamespace, migrator: DB, until: Any
) -> None:
    made, who = approved(shop)
    given = {"valid_until": None if until is None else timezone.now() - timedelta(seconds=1)}
    with pytest.raises(ValueError, match="validez"):
        act(shop, who, lifecycle.issue_quote, quote_id=made.pk, version=2, **given)
    stored = reread(shop, made)
    assert (stored.status, stored.version, stored.sent_at, stored.company_snapshot) == (
        "APPROVED", 2, None, None,
    )  # fmt: skip
    assert events(migrator, made) == []


def test_issuing_copies_the_customer_only_for_who_may_see_it_and_says_nothing_else(
    shop: SimpleNamespace,
) -> None:
    made, _ = approved(shop)
    blind = plain(shop, {"quotes.send": "ORGANIZATION", "quotes.view": "ORGANIZATION"})
    with pytest.raises(BadReference) as hidden:
        issue(shop, blind, made)
    seller = sender(shop)
    with tenant_scope(ctx(shop.a, seller)) as tenant:
        delete_contact(tenant, contact_id=made.contact_id)
    with pytest.raises(BadReference) as gone:  # borrado: el mismo error que no verlo
        issue(shop, seller, made)
    assert (hidden.value.field, hidden.value.args) == (gone.value.field, gone.value.args)
    assert reread(shop, made).status == "APPROVED"


def test_a_quote_out_of_scope_does_not_wait_for_whoever_holds_its_lock(
    shop: SimpleNamespace,
) -> None:
    made, who = approved(shop)
    mine = sender(shop, "10", **{"quotes.view": "OWN", "quotes.send": "OWN"})
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
        act(shop, mine, lifecycle.issue_quote, quote_id=made.pk, version=2)
    waited = time.monotonic() - started
    timer.cancel()
    release.set()
    holder.join(timeout=10)
    assert waited < 3  # no esperó: el tiempo no dice si la cotización existe


@pytest.mark.parametrize("state", ["PENDING_APPROVAL", "SENT", "CANCELLED"])
def test_only_an_approved_or_a_draft_quote_is_issued(
    shop: SimpleNamespace, migrator: DB, state: str
) -> None:
    if state == "PENDING_APPROVAL":
        made, who = asked(shop, sender(shop, "3"))
        version = 2
    else:
        made, who = approved(shop)
        version = 2
        if state == "SENT":
            issue(shop, who, made)
        else:
            act(shop, who, commands.cancel_quote, quote_id=made.pk, version=2)
        version = 3
    before = reread(shop, made)
    with pytest.raises(WrongStatus):
        issue(shop, who, made, version=version)
    after = reread(shop, made)
    assert (after.status, after.version) == (before.status, before.version)
    ends = {"PENDING_APPROVAL": [], "SENT": ["quote.issued"], "CANCELLED": ["quote.cancelled"]}
    assert [k for k, _ in events(migrator, made)] == ends[state]  # y nada más


# ---- dos transacciones a la vez ----


def test_issuing_and_cancelling_at_once_leave_one_outcome_and_one_event(
    shop: SimpleNamespace, migrator: DB
) -> None:
    made, who = approved(shop)
    issuing = partial(lifecycle.issue_quote, quote_id=made.pk, version=2, valid_until=LATER)
    cancelling = partial(commands.cancel_quote, quote_id=made.pk, version=2)
    first, second = race(shop.a, who, [issuing, cancelling])
    assert (first.status, second) == ("SENT", "StaleVersion")
    assert [k for k, _ in events(migrator, made)] == ["quote.issued"]


def test_issuing_twice_at_once_freezes_one_snapshot_and_writes_one_event(
    shop: SimpleNamespace, migrator: DB
) -> None:
    made, who = approved(shop)
    twice = partial(lifecycle.issue_quote, quote_id=made.pk, version=2, valid_until=LATER)
    first, second = race(shop.a, who, [twice, twice])
    assert (first.status, second) == ("SENT", "StaleVersion")
    assert [k for k, _ in events(migrator, made)] == ["quote.issued"]
    assert actions(migrator).count("quote.issued") == 1


@pytest.mark.parametrize("failing", ["record", "_event"])
def test_a_failure_while_issuing_leaves_the_quote_approved_and_unfrozen(
    shop: SimpleNamespace, migrator: DB, monkeypatch: pytest.MonkeyPatch, failing: str
) -> None:
    made, who = approved(shop)

    def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(lifecycle, failing, boom)
    with pytest.raises(RuntimeError, match="boom"):
        issue(shop, who, made)
    monkeypatch.undo()
    stored = reread(shop, made)
    assert (stored.status, stored.version, stored.company_snapshot, stored.sent_at) == (
        "APPROVED", 2, None, None,
    )  # fmt: skip
    assert events(migrator, made) == [] and issue(shop, who, made).status == "SENT"


# ---- lo que la base impone a lo emitido (SQL directo de `crm_app`) ----


def update(shop: SimpleNamespace, made: Quote, **fields: Any) -> None:
    with tenant_scope(ctx(shop.a)):
        assert Quote.objects.filter(pk=made.pk).update(**fields) == 1


@pytest.mark.parametrize(
    "field",
    ["company_snapshot", "customer_snapshot", "terms_snapshot", "warranty_snapshot", "sent_at",
     "valid_until", "notes_to_customer"],
)  # fmt: skip
def test_what_was_sent_is_not_edited_not_even_by_sql(shop: SimpleNamespace, field: str) -> None:
    made, _ = sent(shop)
    new = {
        "company_snapshot": {"name": "Otra"}, "customer_snapshot": {"display_name": "Otro"},
        "terms_snapshot": "Nuevas", "warranty_snapshot": "Nueva", "sent_at": timezone.now(),
        "valid_until": LATER + timedelta(days=1), "notes_to_customer": "Nueva",
    }[field]  # fmt: skip
    with pytest.raises(IntegrityError, match="set only when issuing|not edited"):
        update(shop, made, **{field: new})
    update(shop, made, internal_notes="lo interno sí cambia")


@pytest.mark.parametrize("state", ["DRAFT", "PENDING_APPROVAL"])
def test_the_snapshots_are_set_only_when_the_quote_goes_to_sent(
    shop: SimpleNamespace, state: str
) -> None:
    made, _ = (
        asked(shop, sender(shop, "3"))
        if state == "PENDING_APPROVAL"
        else draft(shop, sender(shop))[:2]
    )
    with pytest.raises(IntegrityError, match="set only when issuing"):
        update(shop, made, company_snapshot={"name": "X"})
    with pytest.raises(IntegrityError, match="set only when issuing"):
        update(shop, made, sent_at=timezone.now())


def test_an_approved_quote_freezes_its_validity_and_notes_until_it_is_sent(
    shop: SimpleNamespace,
) -> None:
    made, who = approved(shop)
    for field, value in (("valid_until", LATER), ("notes_to_customer", "Cambiada")):
        with pytest.raises(IntegrityError, match="not edited"):
            update(shop, made, **{field: value})
    assert issue(shop, who, made).valid_until is not None  # al emitir sí se fija


def test_the_database_asks_every_issued_state_for_its_documents_and_dates(
    shop: SimpleNamespace,
) -> None:
    made, _ = approved(shop)
    with pytest.raises(IntegrityError, match="quotes_issued_ck"):  # enviada sin fotografías
        update(shop, made, status="SENT")
    update(shop, made, status="SENT", **facts("SENT"))


@pytest.mark.parametrize(
    "field",
    ["company_snapshot", "customer_snapshot", "terms_snapshot", "warranty_snapshot", "sent_at",
     ],
)  # fmt: skip
def test_a_quote_is_born_a_draft_without_snapshots_or_decisions(
    shop: SimpleNamespace, field: str
) -> None:
    value = {"company_snapshot": {"n": 1}, "customer_snapshot": {"n": 1}, "terms_snapshot": "t",
             "warranty_snapshot": "w", "sent_at": timezone.now()}[field]  # fmt: skip
    with pytest.raises(IntegrityError, match="born without"):
        quote(shop, number="COT-900010", **{field: value})


def test_a_cancellation_date_or_reason_is_not_rewritten_not_even_by_sql(
    shop: SimpleNamespace,
) -> None:
    made, who = approved(shop)
    act(shop, who, commands.cancel_quote, quote_id=made.pk, version=2, reason="Antes")
    for field, value in (("cancelled_at", timezone.now()), ("cancel_reason", "Otro")):
        with pytest.raises(IntegrityError, match="not rewritten"):
            update(shop, made, **{field: value})


def test_what_waits_for_approval_is_not_edited_so_the_approver_approves_what_he_sees(
    shop: SimpleNamespace,
) -> None:
    made, _ = asked(shop, sender(shop, "3"))
    for field, value in (("valid_until", LATER), ("notes_to_customer", "Cambiada")):
        with pytest.raises(IntegrityError, match="not edited"):
            update(shop, made, **{field: value})
    update(shop, made, internal_notes="lo interno sí")


def test_the_guard_of_0004_only_adds_lines_to_the_function_of_0003(migrator: DB) -> None:
    (live,) = migrator.execute("SELECT pg_get_functiondef('quotes_guard'::regproc)").fetchone() or (
        "",
    )
    old = import_module("apps.quotes.migrations.0003_quotes_guard_limit").QUOTES_GUARD
    old = old.replace("__APP__", settings.DB_APP_ROLE)
    bodies = [
        text.split("$f$" if "$f$" in text else "$function$")[1].splitlines() for text in (old, live)
    ]
    lines = [[x.rstrip() for x in body] for body in bodies]
    opcodes = difflib.SequenceMatcher(None, *lines, autojunk=False).get_opcodes()
    assert {tag for tag, *_ in opcodes} == {"equal", "insert"}  # nada de lo anterior se quitó


def test_reverting_the_migration_refuses_to_drop_what_was_issued(shop: SimpleNamespace) -> None:
    revert = import_module("apps.quotes.migrations.0004_quotes_lifecycle")._refuse_if_data
    made, _ = approved(shop)
    with tenant_scope(ctx(shop.a)):
        revert(django_apps, None)  # nada emitido: se puede
    issue(shop, sender(shop), made)
    with tenant_scope(ctx(shop.a)), pytest.raises(RuntimeError, match="no se revierte"):
        revert(django_apps, None)
