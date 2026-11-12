"""F5-09b3: aprobar, rechazar y retirar la solicitud de una cotización (`apps.quotes.decisions`),
con PostgreSQL real (RLS, `crm_app` y los disparadores de la base)."""

import json
from decimal import Decimal as D
from functools import partial
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from django.db import IntegrityError

from apps.access.selectors import AccessDenied
from apps.accounts.models import User
from apps.organizations.models import OrganizationMembership as Membership
from apps.pricing import costs
from apps.quotes import commands, decisions, discounts
from apps.quotes.commands import NotAllowed, QuoteNotDraft, StaleVersion
from apps.quotes.decisions import BeyondLimit, NotPending, SelfApproval
from apps.quotes.discounts import PriceRefused
from apps.quotes.models import DiscountPolicy, Quote, QuoteApproval, QuoteItem
from core.tenancy.context import ActorType, TenantContext
from core.tenancy.scope import tenant_scope
from tests.factories import make_user
from tests.test_authorization import give
from tests.test_inventory_stock import race
from tests.test_memberships import ctx, join
from tests.test_quote_commands import DB, shop, stored
from tests.test_quote_commands import world as world  # noqa: PLC0414
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
APPROVER: dict[str, Any] = {"quotes.approve": None, "quotes.view": "ORGANIZATION"}


def boss(shop: SimpleNamespace, percent: str | None = "10", **grants: Any) -> User:
    """Quien aprueba: `quotes.approve` y verlas todas, con un límite propio."""
    return member(shop, percent, **{**APPROVER, **grants})


def asked(shop: SimpleNamespace, seller: User | None = None, percent: str = "5") -> tuple[Any, Any]:
    """Una cotización con un descuento sobre el 3 % de quien la pide: espera aprobación (v. 2)."""
    seller = seller or member(shop, "3")
    made, phone, _ = draft(shop, seller)
    assert discount(shop, seller, made, phone, "PERCENT", percent).status == "PENDING_APPROVAL"
    return made, seller


def approve(shop: SimpleNamespace, who: User, made: Quote, version: Any = 2, **fields: Any) -> Any:
    return act(shop, who, decisions.approve_quote, quote_id=made.pk, version=version, **fields)


def reject(
    shop: SimpleNamespace, who: User, made: Quote, version: int = 2, reason: Any = "Baja"
) -> Any:
    return act(shop, who, decisions.reject_quote, quote_id=made.pk, version=version, reason=reason)


def withdraw(shop: SimpleNamespace, who: User, made: Quote, version: int = 2) -> Any:
    return act(shop, who, decisions.withdraw_approval_request, quote_id=made.pk, version=version)


def waiting(shop: SimpleNamespace, reason: str) -> Quote:
    """Una solicitud abierta escrita sin pasar por los comandos: la de la IA (sin quien la pida) o
    la de un precio manual del iPhone (3500 de 4399) sin costo, que el comando no deja crear."""
    pricer = member(shop, "100", **{"quotes.override_price": None})
    made, phone, _ = draft(shop, pricer)
    manual_price = reason == "MANUAL_PRICE"
    with tenant_scope(ctx(shop.a, pricer)):
        if manual_price:
            QuoteItem.objects.filter(pk=phone.pk).update(
                manual_price_override=True, override_reason="Mayorista", discount_amount=D("899"),
                final_unit_price=D("3500"), line_total=D("3500"),
            )  # fmt: skip
        QuoteApproval.objects.create(
            quote_id=made.pk, reason=reason, limit_percent_at_request=D(100),
            requested_by_user_id=pricer.pk if manual_price else None,
            submitted_by_user_id=pricer.pk,
        )  # fmt: skip
        Quote.objects.filter(pk=made.pk).update(status="PENDING_APPROVAL", version=2)
    return made


def log(migrator: DB) -> list[tuple[str, str, str, dict[str, Any], dict[str, Any]]]:
    found = migrator.execute(
        "SELECT action, actor_type, actor_id::text, changes::text, metadata::text FROM audit_logs"
        " WHERE entity_type = 'quote' ORDER BY occurred_at, id"
    ).fetchall()
    return [(a, t, i, json.loads(c), json.loads(m)) for a, t, i, c, m in found]


def test_approving_opens_the_quote_for_the_next_step_and_audits_who_decided(
    shop: SimpleNamespace, migrator: DB
) -> None:
    approver = boss(shop)
    made, seller = asked(shop)
    done = approve(shop, approver, made, comment="  Cliente DNI 12345678 ")
    assert (done.status, done.version) == ("APPROVED", 3)
    [req] = approvals(shop)
    assert (req.status, req.decided_by_user_id, req.requested_by_user_id) == (
        "APPROVED", approver.pk, seller.pk,
    )  # fmt: skip
    assert req.comment == "Cliente DNI 12345678" and req.decided_at and req.version == 2
    *_, (action, actor, who, changes, metadata) = log(migrator)
    assert (action, actor, who) == ("quote.approved", "USER", str(approver.pk))  # el actor real
    assert changes["status"] == ["PENDING_APPROVAL", "APPROVED"]
    assert changes["approval_id"] == [str(req.pk)] * 2 and changes["comment"] == [None, "[texto]"]
    assert "12345678" not in json.dumps(log(migrator)) and metadata == {}
    with pytest.raises(QuoteNotDraft):  # lo aprobado no se edita, ni por los comandos...
        discount(shop, seller, made, stored(shop.a, made)[0], "NONE", version=3)
    editor = member(shop, "3", **{"quotes.view": "ORGANIZATION"})
    with pytest.raises(QuoteNotDraft):
        act(shop, editor, commands.update_quote, quote_id=made.pk, version=3, internal_notes="x")
    with tenant_scope(ctx(shop.a, seller)), pytest.raises(IntegrityError, match="frozen"):
        Quote.objects.filter(pk=made.pk).update(total=D(1), subtotal=D(1), tax_total=D(0))


def test_rejecting_needs_a_reason_returns_the_quote_to_draft_and_it_can_be_asked_again(
    shop: SimpleNamespace, migrator: DB
) -> None:
    approver = boss(shop)
    made, seller = asked(shop)
    for reason in (None, "", "   ", 5):
        with pytest.raises(ValueError):  # noqa: PT011
            reject(shop, approver, made, reason=reason)
    assert (
        reread(shop, made).status == "PENDING_APPROVAL" and approvals(shop)[0].status == "PENDING"
    )
    done = reject(shop, approver, made, reason="  Demasiado, DNI 12345678 ")
    assert (done.status, done.version) == ("DRAFT", 3)
    [req] = approvals(shop)
    assert (req.status, req.decided_by_user_id) == ("REJECTED", approver.pk)
    assert req.comment == "Demasiado, DNI 12345678"
    *_, (action, actor, who, changes, _) = log(migrator)
    assert (action, actor, who) == ("quote.rejected", "USER", str(approver.pk))
    assert changes["approval_status"] == ["PENDING", "REJECTED"] and "12345678" not in str(changes)
    discount(
        shop, seller, made, stored(shop.a, made)[0], "PERCENT", "2", version=3
    )  # edita de nuevo
    assert submit(shop, seller, made, version=4).status == "APPROVED"  # dentro de su límite
    again, _ = asked(shop, seller)
    assert reject(shop, approver, again).status == "DRAFT"


def test_whoever_asked_cannot_approve_it_but_can_reject_with_a_reason_if_allowed(
    shop: SimpleNamespace,
) -> None:
    both = boss(shop, "3")  # pide con su límite del 3 % y puede aprobar
    made, _ = asked(shop, both)
    with pytest.raises(SelfApproval):
        approve(shop, both, made)
    assert (reread(shop, made).version, approvals(shop)[0].status) == (2, "PENDING")
    assert approve(shop, boss(shop), made).status == "APPROVED"  # otra persona
    other, _ = asked(shop, both)
    assert reject(shop, both, other).status == "DRAFT"  # rechazar lo propio equivale a retirarlo
    plain = member(shop, "3")  # sin `quotes.approve`: no rechaza ni lo suyo, lo retira
    mine, _ = asked(shop, plain)
    with pytest.raises(NotAllowed):
        reject(shop, plain, mine)
    assert withdraw(shop, plain, mine).status == "DRAFT"


def test_the_approver_cannot_exceed_their_own_limit(shop: SimpleNamespace, migrator: DB) -> None:
    made, _ = asked(shop)  # 5 % pedido
    low, bare, capped = boss(shop, "3"), boss(shop, None), boss(shop, "10", cap="100")
    usd = boss(shop, "10", cap="1000")
    with tenant_scope(ctx(shop.a)):
        DiscountPolicy.objects.filter(max_discount_amount=D("1000")).update(currency="USD")
    for who in (low, bare, capped, usd):  # 3 %, 0 %, tope de 100 contra 219,95, tope en dólares
        with pytest.raises(BeyondLimit):
            approve(shop, who, made)
    assert (reread(shop, made).status, approvals(shop)[0].status) == ("PENDING_APPROVAL", "PENDING")
    assert (
        len(log(migrator)) == 3 and reread(shop, made).version == 2
    )  # nada se escribió ni se auditó
    assert approve(shop, boss(shop, "5"), made).status == "APPROVED"  # justo en su límite
    other, _ = asked(shop)
    assert approve(shop, boss(shop, None, owner=True), other).status == "APPROVED"  # el Owner no


def test_a_manual_price_is_checked_against_the_cost_of_today_when_it_is_approved(
    shop: SimpleNamespace, migrator: DB
) -> None:
    pricer = member(shop, "100", **{"quotes.override_price": None})
    cost(shop, shop.iphone, "3000")
    owner = boss(shop, None, owner=True)
    made, phone, _ = draft(shop, pricer)
    manual(shop, pricer, made, phone, "3500")
    cost(shop, shop.case, "50")  # y una cotización con precio manual en una caja, de costo 50
    other, _, line = draft(shop, pricer)
    manual(shop, pricer, other, line, "90")
    act(shop, owner, costs.set_cost, variant_id=shop.iphone, amount="3600", reason="Subió")
    with pytest.raises(PriceRefused) as refused:  # el costo subió desde que se pidió
        approve(shop, owner, made)
    assert str(refused.value) == "" and refused.value.args == ()  # sin cifras
    assert not any(a == "quote.approved" for a, *_ in log(migrator))  # el rechazo no deja rastro
    assert (reread(shop, made).status, approvals(shop)[0].status) == ("PENDING_APPROVAL", "PENDING")
    act(shop, owner, costs.set_cost, variant_id=shop.iphone, amount="3400", reason="Bajó")
    assert approve(shop, owner, made).status == "APPROVED"  # el costo (3400) por debajo de 3500
    act(shop, owner, costs.set_cost, variant_id=shop.case, amount="95", reason="Subió")
    with pytest.raises(PriceRefused):  # también con otra línea: cada precio manual vuelve a pasar
        approve(shop, owner, other)
    assert reject(shop, owner, other).status == "DRAFT"  # rechazar siempre se puede


def test_a_decision_is_final_and_a_repeat_changes_nothing(shop: SimpleNamespace) -> None:
    approver = boss(shop)
    made, _ = asked(shop)
    assert approve(shop, approver, made).version == 3
    with pytest.raises(StaleVersion):  # el mismo envío otra vez
        approve(shop, approver, made)
    with pytest.raises(NotPending):  # con la versión nueva: ya no espera nada
        approve(shop, approver, made, version=3)
    with pytest.raises(NotPending):
        reject(shop, approver, made, version=3)
    with pytest.raises(NotPending):
        withdraw(shop, approver, made, version=3)
    other, _ = asked(shop)
    reject(shop, approver, other)
    with pytest.raises(NotPending):  # un borrador no espera nada
        approve(shop, approver, other, version=3)
    assert [x.status for x in approvals(shop)] == ["APPROVED", "REJECTED"]
    assert (reread(shop, made).version, reread(shop, other).version) == (3, 3)


def test_two_decisions_at_once_leave_one(shop: SimpleNamespace) -> None:
    approver = boss(shop)
    made, seller = asked(shop)
    one = partial(decisions.approve_quote, quote_id=made.pk, version=2)
    first, second = race(shop.a, approver, [one, one])
    assert (first.status, second) == ("APPROVED", "StaleVersion")
    other, seller = asked(shop, seller)
    gone = partial(decisions.withdraw_approval_request, quote_id=other.pk, version=2)
    yes = partial(decisions.approve_quote, quote_id=other.pk, version=2)
    first, second = race(shop.a, approver, [gone, yes])  # retirar y aprobar a la vez
    assert (first.status, second) == ("DRAFT", "StaleVersion")
    assert [x.status for x in approvals(shop)] == ["APPROVED", "CANCELLED"]


def test_withdrawing_is_for_who_asked_or_who_can_approve_never_the_request_of_the_ai_by_others(
    shop: SimpleNamespace, migrator: DB
) -> None:
    approver, stranger = boss(shop), member(shop, "3")
    made, seller = asked(shop)
    viewer = member(shop, "3", **{"quotes.view": "ORGANIZATION"})  # ve y edita, no aprueba
    for person in (stranger, viewer):
        with pytest.raises(NotAllowed):  # otra persona con `quotes.update` en alcance: no
            withdraw(shop, person, made)
        with pytest.raises(NotAllowed):  # y antes que la versión: no se le cuenta
            withdraw(shop, person, made, version=9)
    bare = make_user()  # ni `quotes.approve` ni `quotes.update`: el permiso va antes que todo
    give(shop.a, join(shop.a, bare).pk, {"prices.view": None})
    with pytest.raises(NotAllowed):
        withdraw(shop, bare, made, version=9)
    assert reread(shop, made).status == "PENDING_APPROVAL"
    done = withdraw(shop, seller, made)  # quien la pidió
    assert (done.status, done.version) == ("DRAFT", 3)
    [req] = approvals(shop)
    assert (req.status, req.decided_by_user_id, req.comment) == ("CANCELLED", seller.pk, "")
    *_, (action, actor, who, changes, _) = log(migrator)
    assert (action, actor, who) == ("quote.approval_withdrawn", "USER", str(seller.pk))
    assert changes["approval_status"] == ["PENDING", "CANCELLED"] and "comment" not in changes
    discount(shop, seller, made, stored(shop.a, made)[0], "NONE", version=3)  # vuelve a editarse
    again, _ = asked(shop, seller)
    assert withdraw(shop, approver, again).status == "DRAFT"  # quien aprueba, aunque no la pidió
    ai = waiting(shop, "AI_CREATED")  # la solicitud de la IA no la retira quien no puede aprobar
    for person in (stranger, seller):
        with pytest.raises(NotAllowed):
            withdraw(shop, person, ai)
    assert withdraw(shop, approver, ai).status == "DRAFT"
    assert [x.status for x in approvals(shop)] == ["CANCELLED"] * 3


def test_the_request_of_the_ai_is_never_approved_alone_a_person_decides(
    shop: SimpleNamespace,
) -> None:
    ai = waiting(shop, "AI_CREATED")
    approver = boss(shop, None)  # no pidió nadie: puede aprobar cualquier persona con el permiso
    for actor in (ActorType.SYSTEM, ActorType.AI_AGENT):
        for command, fields in (
            (decisions.approve_quote, {}), (decisions.reject_quote, {"reason": "x"}),
            (decisions.withdraw_approval_request, {}),
        ):  # fmt: skip
            with tenant_scope(TenantContext(shop.a, "ai", None, actor, None)) as tenant:
                with pytest.raises(AccessDenied):
                    command(tenant, quote_id=ai.pk, version=2, **fields)
    assert (reread(shop, ai).status, approvals(shop)[0].status) == ("PENDING_APPROVAL", "PENDING")
    assert approve(shop, approver, ai).status == "APPROVED"
    assert approvals(shop)[0].decided_by_user_id == approver.pk


def test_permission_and_scope_come_before_the_version_or_the_state(shop: SimpleNamespace) -> None:
    made, seller = asked(shop)
    bad = {"version": 9}  # una versión que no es: a quien no puede, no se le cuenta
    plain = member(shop, "10")  # sin `quotes.approve`
    for command, extra in (
        (decisions.approve_quote, {}),
        (decisions.reject_quote, {"reason": "x"}),
    ):
        with pytest.raises(NotAllowed):
            act(shop, plain, command, quote_id=made.pk, **{**extra, **bad})
        with pytest.raises(NotAllowed):  # tampoco de una que no existe
            act(shop, plain, command, quote_id=uuid4(), **{**extra, "version": 1})
    assert reread(shop, made).version == 2
    own = boss(shop, "10", **{"quotes.view": "OWN"})  # aprueba, pero solo ve las suyas
    blind = make_user()  # aprueba, no puede ver ninguna
    give(shop.a, join(shop.a, blind).pk, {"quotes.approve": None})
    for who in (own, blind):
        for command, extra in (
            (decisions.approve_quote, {}), (decisions.reject_quote, {"reason": "x"}),
        ):  # fmt: skip
            for target in (made.pk, uuid4()):  # existe o no: el mismo error, antes de la versión
                with pytest.raises(Quote.DoesNotExist):
                    act(shop, who, command, quote_id=target, **{**extra, **bad})
    assert approvals(shop)[0].status == "PENDING" and seller


def test_withdrawing_needs_the_scope_of_update_and_a_requester_with_own_scope_works(
    shop: SimpleNamespace,
) -> None:
    own = member(shop, "3", **{"quotes.update": "OWN"})
    made, _ = asked(shop, own)
    assert withdraw(shop, own, made).status == "DRAFT"  # la suya
    other, _ = asked(shop)
    with pytest.raises(Quote.DoesNotExist):  # la de otra persona: fuera de su alcance, como si no
        withdraw(shop, own, other, version=9)
    with pytest.raises(Quote.DoesNotExist):  # y una que no existe, igual
        act(shop, own, decisions.withdraw_approval_request, quote_id=uuid4(), version=2)
    assert reread(shop, other).status == "PENDING_APPROVAL"


def test_a_person_of_another_organization_cannot_decide(shop: SimpleNamespace) -> None:
    made, _ = asked(shop)
    with tenant_scope(ctx(shop.b)):
        give(
            shop.b,
            Membership.objects.get(user=shop.luis).pk,
            {**APPROVER, "quotes.update": "ORGANIZATION"},
        )
    for command, extra in (
        (decisions.approve_quote, {}), (decisions.reject_quote, {"reason": "x"}),
        (decisions.withdraw_approval_request, {}),
    ):  # fmt: skip
        with tenant_scope(ctx(shop.b, shop.luis)) as tenant, pytest.raises(Quote.DoesNotExist):
            command(tenant, quote_id=made.pk, version=2, **extra)
    assert (reread(shop, made).version, approvals(shop)[0].status) == (2, "PENDING")


def test_the_values_are_validated_and_nothing_is_written_for_a_wrong_one(
    shop: SimpleNamespace, migrator: DB
) -> None:
    approver = boss(shop)
    made, _ = asked(shop)
    for version in (0, True, "2", 2.0, None, 2**31):
        with pytest.raises(ValueError):  # noqa: PT011
            approve(shop, approver, made, version=version)
    for comment in (5, "x" * 2001, "a\x00b"):
        with pytest.raises(ValueError):  # noqa: PT011
            approve(shop, approver, made, comment=comment)
    with pytest.raises(ValueError):  # noqa: PT011
        act(shop, approver, decisions.approve_quote, quote_id="no", version=2)
    assert reread(shop, made).version == 2 and len(log(migrator)) == 3  # creada, descuento, pedida


def test_the_database_guards_the_decision_even_against_a_command_that_forgets(
    shop: SimpleNamespace,
) -> None:
    made, seller = asked(shop)
    with pytest.raises(IntegrityError, match="open request"):  # sin aprobación decidida
        raw(shop, seller, made, "APPROVED")
    with pytest.raises(IntegrityError, match="open request"):  # ni volver a borrador con la abierta
        raw(shop, seller, made, "DRAFT")
    with tenant_scope(ctx(shop.a, seller)), pytest.raises(IntegrityError, match="separation"):
        QuoteApproval.objects.filter(quote_id=made.pk).update(
            status="APPROVED", decided_by_user_id=seller.pk, decided_at=made.created_at
        )  # quien pidió no aprueba, tampoco por SQL
    with tenant_scope(ctx(shop.a, seller)), pytest.raises(IntegrityError, match="separation"):
        QuoteApproval.objects.filter(quote_id=made.pk).update(
            status="REJECTED", decided_by_user_id=seller.pk, decided_at=made.created_at, comment=" "
        )  # rechazar sin motivo
    approve(shop, boss(shop), made)
    with tenant_scope(ctx(shop.a, seller)), pytest.raises(IntegrityError, match="final"):
        QuoteApproval.objects.filter(quote_id=made.pk).update(
            status="PENDING"
        )  # una decisión no se reabre
    with pytest.raises(IntegrityError, match="not allowed"):
        raw(shop, seller, made, "DRAFT")  # una aprobada no vuelve atrás
    assert discounts and reread(shop, made).status == "APPROVED"


def test_a_missing_foreign_and_out_of_scope_quote_give_one_identical_error(
    shop: SimpleNamespace,
) -> None:
    made, _ = asked(shop)
    own = boss(shop, "10", **{"quotes.view": "OWN", "quotes.update": "OWN"})
    with tenant_scope(ctx(shop.b)):
        give(
            shop.b,
            Membership.objects.get(user=shop.luis).pk,
            {**APPROVER, "quotes.update": "ORGANIZATION"},
        )
    seen = set()
    for command, extra in (
        (decisions.approve_quote, {}), (decisions.reject_quote, {"reason": "x"}),
        (decisions.withdraw_approval_request, {}), (discounts.submit_quote, {}),
    ):  # fmt: skip
        for org, who, target in ((shop.a, own, made.pk), (shop.a, own, uuid4()),
                                 (shop.b, shop.luis, made.pk)):  # fmt: skip
            with tenant_scope(ctx(org, who)) as tenant, pytest.raises(Quote.DoesNotExist) as caught:
                command(tenant, quote_id=target, version=9, **extra)
            seen.add((type(caught.value), str(caught.value), caught.value.args))
    assert len(seen) == 1  # fuera de alcance, ajena e inexistente: ni el tipo ni el texto difieren


def test_the_refusals_of_the_decision_carry_no_figures(shop: SimpleNamespace) -> None:
    both = boss(shop, "3")
    made, _ = asked(shop, both)
    other, _ = asked(shop)
    with pytest.raises(SelfApproval) as own:
        approve(shop, both, made)
    with pytest.raises(BeyondLimit) as beyond:
        approve(shop, both, other)
    approve(shop, boss(shop), other)
    with pytest.raises(NotPending) as closed:
        approve(shop, boss(shop), other, version=3)
    with pytest.raises(PriceRefused) as price:  # un precio manual sin costo vigente
        approve(shop, boss(shop, None, owner=True), waiting(shop, "MANUAL_PRICE"))
    assert all(str(e.value) == "" and e.value.args == () for e in (own, beyond, closed, price))


def test_a_manual_price_without_a_usable_cost_is_refused_and_a_covering_one_is_approved(
    shop: SimpleNamespace,
) -> None:
    owner = boss(shop, None, owner=True)
    made = waiting(shop, "MANUAL_PRICE")
    with pytest.raises(PriceRefused):  # sin ningún costo: falla cerrado
        approve(shop, owner, made)
    cost(shop, shop.iphone, "1000", currency="USD")
    with pytest.raises(PriceRefused):  # un costo en otra moneda no se compara
        approve(shop, owner, made)
    assert (reread(shop, made).status, approvals(shop)[0].status) == ("PENDING_APPROVAL", "PENDING")
    act(shop, owner, costs.set_cost, variant_id=shop.iphone, amount="3000", reason="En soles")
    assert approve(shop, owner, made).status == "APPROVED"


def test_the_markdown_of_a_manual_price_counts_against_the_limit_of_the_approver(
    shop: SimpleNamespace,
) -> None:
    pricer = member(shop, "100", **{"quotes.override_price": None})
    cost(shop, shop.iphone, "3000")
    made, phone, _ = draft(shop, pricer)
    manual(shop, pricer, made, phone, "3500")  # 899 de 4399: 20,44 %
    for limit in ("10", "20"):
        with pytest.raises(BeyondLimit):
            approve(shop, boss(shop, limit), made)
    assert approve(shop, boss(shop, "21"), made).status == "APPROVED"


def test_the_lines_of_an_approved_quote_cannot_be_written_by_the_database_either(
    shop: SimpleNamespace,
) -> None:
    made, seller = asked(shop)
    approve(shop, boss(shop), made)
    [line, _] = stored(shop.a, made)
    for attempt in (
        lambda: QuoteItem.objects.filter(pk=line.pk).update(final_unit_price=D("1.00")),
        lambda: QuoteItem.objects.filter(quote_id=made.pk).delete(),
    ):  # fmt: skip
        with (
            tenant_scope(ctx(shop.a, seller)),
            pytest.raises(IntegrityError, match="only in DRAFT"),
        ):
            attempt()
    line.pk, line.sort_order = uuid4(), 9
    line._state.adding = True
    with tenant_scope(ctx(shop.a, seller)), pytest.raises(IntegrityError, match="only in DRAFT"):
        line.save(force_insert=True)  # ni una línea nueva
    assert [x.final_unit_price for x in stored(shop.a, made)][0] != D("1.00")


RIVALS: dict[str, Any] = {
    "set_line_discount": discounts.set_line_discount,
    "set_manual_price": discounts.set_manual_price,
    "submit_quote": discounts.submit_quote, "cancel_quote": commands.cancel_quote,
    "update_quote": commands.update_quote, "reject_quote": decisions.reject_quote,
    "withdraw": decisions.withdraw_approval_request, "approve_quote": decisions.approve_quote,
}  # fmt: skip
EXTRA: dict[str, dict[str, Any]] = {
    "set_line_discount": {"discount_type": "NONE"},
    "set_manual_price": {"price": "4000", "reason": "x"},
    "update_quote": {"internal_notes": "x"}, "reject_quote": {"reason": "x"},
}  # fmt: skip


def rival(name: str, made: Quote, line: QuoteItem) -> Any:
    lined = {"line_id": line.pk} if name.startswith("set_") else {}
    return partial(RIVALS[name], quote_id=made.pk, version=2, **lined, **EXTRA.get(name, {}))


@pytest.mark.parametrize("other", [n for n in RIVALS if n != "approve_quote"])
def test_a_decision_and_any_other_write_at_once_leave_one_coherent_outcome(
    shop: SimpleNamespace, other: str
) -> None:
    everything = boss(
        shop, "100", **{"quotes.override_price": None, "quotes.cancel": "ORGANIZATION"}
    )
    made, _ = asked(shop)
    line = stored(shop.a, made)[0]
    first, second = race(
        shop.a, everything, [rival("approve_quote", made, line), rival(other, made, line)]
    )
    assert (first.status, first.version, second) == ("APPROVED", 3, "StaleVersion")
    assert (reread(shop, made).status, [a.status for a in approvals(shop)]) == (
        "APPROVED",
        ["APPROVED"],
    )
    assert stored(shop.a, made)[0].final_unit_price == line.final_unit_price


@pytest.mark.parametrize(
    ("first", "second", "quote_end", "request_end"),
    [("reject_quote", "approve_quote", "DRAFT", "REJECTED"),
     ("withdraw", "withdraw", "DRAFT", "CANCELLED"),
     ("withdraw", "reject_quote", "DRAFT", "CANCELLED")],
)  # fmt: skip
def test_two_decisions_of_different_kind_at_once_leave_the_first_and_a_stale_version(
    shop: SimpleNamespace, first: str, second: str, quote_end: str, request_end: str
) -> None:
    everything = boss(
        shop, "100", **{"quotes.override_price": None, "quotes.cancel": "ORGANIZATION"}
    )
    made, _ = asked(shop)
    line = stored(shop.a, made)[0]
    won, lost = race(shop.a, everything, [rival(first, made, line), rival(second, made, line)])
    assert (won.status, won.version, lost) == (quote_end, 3, "StaleVersion")
    assert [a.status for a in approvals(shop)] == [request_end]


@pytest.mark.parametrize("failing", ["record", "_stamp"])
@pytest.mark.parametrize("name", ["approve", "reject", "withdraw"])
def test_a_failure_after_the_request_is_saved_rolls_everything_back(
    shop: SimpleNamespace, migrator: DB, monkeypatch: pytest.MonkeyPatch, name: str, failing: str
) -> None:
    approver = boss(shop)
    made, _ = asked(shop)
    before = len(log(migrator))

    def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(decisions, failing, boom)
    runs: dict[str, Any] = {"approve": approve, "reject": reject, "withdraw": withdraw}
    with pytest.raises(RuntimeError, match="boom"):
        runs[name](shop, approver, made)
    monkeypatch.undo()
    [req] = approvals(shop)  # la solicitud sigue abierta, la cotización sin tocar, sin auditoría
    assert (req.status, req.decided_by_user_id, req.version) == ("PENDING", None, 1)
    assert (reread(shop, made).status, reread(shop, made).version) == ("PENDING_APPROVAL", 2)
    assert len(log(migrator)) == before
    ends = {"approve": "APPROVED", "reject": "DRAFT", "withdraw": "DRAFT"}
    assert runs[name](shop, approver, made).status == ends[name]  # y se puede decidir
