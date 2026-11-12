"""F5-09b2: descuentos con el límite por rol, precio manual con el suelo de costo y la solicitud de
aprobación de una cotización (`apps.quotes.discounts`), con PostgreSQL real y `crm_app`."""

import json
from datetime import timedelta
from decimal import Decimal as D
from functools import partial
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from apps.access.models import Role
from apps.accounts.models import User
from apps.organizations.models import OrganizationMembership as Membership
from apps.pricing.models import VariantCost
from apps.quotes import commands, discounts
from apps.quotes.commands import QuoteNotDraft, StaleVersion
from apps.quotes.discounts import NoOpenRequest, NotAllowed, PriceRefused
from apps.quotes.models import DiscountPolicy, Quote, QuoteApproval, QuoteItem
from core.tenancy.scope import tenant_scope
from tests import test_opportunities_tables as tables
from tests.factories import make_user
from tests.test_authorization import give
from tests.test_inventory_stock import race
from tests.test_memberships import ctx, join
from tests.test_prices import price_list
from tests.test_promotion_tables import promo
from tests.test_quote_commands import DB, LONG_AGO, NOW, audit, lines, shop, stored
from tests.test_quote_commands import world as world  # noqa: PLC0414

_ = shop
pytestmark = pytest.mark.usefixtures("tenant_db")
ALLOW: dict[str, str | None] = {"prices.view": None, "quotes.discount": None}


def add_role(shop: SimpleNamespace, user: User, percent: str | None = None, owner: bool = False,
             list_id: Any = None, cap: str | None = None, **grants: Any) -> None:  # fmt: skip
    """Otro rol de `user` en A, con esos permisos y, si se dice, una política con ese límite."""
    with tenant_scope(ctx(shop.a)):
        membership = Membership.objects.get(user=user)
    role = give(shop.a, membership.pk, {**ALLOW, **grants})
    with tenant_scope(ctx(shop.a)):
        if owner:
            Role.objects.filter(pk=role.pk).update(is_owner_role=True)
        if percent is not None:
            DiscountPolicy.objects.create(
                role_id=role.pk,
                max_discount_percent=D(percent),
                applies_to_price_list_id=list_id,
                max_discount_amount=None if cap is None else D(cap),
                currency=None if cap is None and list_id is None else "PEN",
            )


def member(shop: SimpleNamespace, percent: str | None = None, **kwargs: Any) -> User:
    user = make_user()
    join(shop.a, user)
    add_role(shop, user, percent, **kwargs)
    return user


def act(shop: SimpleNamespace, user: User, command: Any, **fields: Any) -> Any:
    with tenant_scope(ctx(shop.a, user)) as tenant:
        return command(tenant, **fields)


def draft(shop: SimpleNamespace, user: User) -> tuple[Quote, QuoteItem, QuoteItem]:
    """Una cotización de `user` con el iPhone (4399,00) y dos cajas (100,50)."""
    made = act(shop, user, commands.create_quote, contact_id=tables.contact(shop.a).pk,
               items=lines((shop.iphone, 1), (shop.case, 2)))  # fmt: skip
    phone, case = stored(shop.a, made)
    return made, phone, case


def discount(shop: SimpleNamespace, user: User, made: Quote, line: QuoteItem, kind: str,
             value: Any = None, version: Any = 1) -> Quote:  # fmt: skip
    done: Quote = act(shop, user, discounts.set_line_discount, quote_id=made.pk, version=version,
               line_id=line.pk, discount_type=kind, discount_value=value)  # fmt: skip
    return done


def approvals(shop: SimpleNamespace) -> list[QuoteApproval]:
    with tenant_scope(ctx(shop.a)):
        return list(QuoteApproval.objects.order_by("created_at"))


def reread(shop: SimpleNamespace, made: Quote) -> Quote:
    with tenant_scope(ctx(shop.a)):
        found: Quote = Quote.objects.get(pk=made.pk)
    return found


def cost(shop: SimpleNamespace, variant: Any, amount: str, **fields: Any) -> None:
    base = {"currency": "PEN", "source": "MANUAL", "change_reason": "Semilla", "tax_included": True}
    with tenant_scope(ctx(shop.a)):
        VariantCost.objects.create(variant_id=variant, amount=D(amount), starts_at=LONG_AGO,
                                   **{**base, **fields})  # fmt: skip


def test_a_discount_within_the_limit_is_applied_and_asks_nothing(
    shop: SimpleNamespace, migrator: DB
) -> None:
    seller = member(shop, "10")
    made, phone, _ = draft(shop, seller)
    done = discount(shop, seller, made, phone, "PERCENT", "5")
    assert (done.status, done.version, done.total) == ("DRAFT", 2, D("4380.05"))  # 4179,05 + 201
    line = stored(shop.a, made)[0]
    assert (line.discount_type, line.discount_value, line.discount_amount) == (
        "PERCENT",
        D("5.00"),
        D("219.95"),
    )  # el importe de la línea lo da `final_unit_price`
    assert (line.final_unit_price, line.line_total, line.unit_price) == (
        D("4179.05"), D("4179.05"), D("4399.00"),
    )  # fmt: skip
    assert approvals(shop) == []
    assert [a for a, _ in audit(migrator)] == ["quote.created", "quote.line_discounted"]
    again = discount(shop, seller, made, phone, "NONE", version=2)  # quitarlo
    assert (again.total, stored(shop.a, made)[0].final_unit_price) == (D("4600.00"), D("4399.00"))


def test_a_discount_over_the_limit_is_not_applied_in_silence_the_quote_asks_for_approval(
    shop: SimpleNamespace, migrator: DB
) -> None:
    seller = member(shop, "10")
    made, phone, _ = draft(shop, seller)
    done = discount(shop, seller, made, phone, "PERCENT", "12")
    assert (done.status, done.version, done.total) == ("PENDING_APPROVAL", 2, D("4072.12"))
    [asked] = approvals(shop)
    assert (asked.status, asked.reason, asked.requested_by_user_id) == (
        "PENDING", "DISCOUNT_OVER_LIMIT", seller.pk,
    )  # fmt: skip
    assert (asked.requested_discount_percent, asked.limit_percent_at_request) == (
        D("12.00"),
        D("10.00"),
    )
    audited = audit(migrator)
    assert [a for a, _ in audited] == ["quote.created", "quote.line_discounted",
                                       "quote.approval_requested"]  # fmt: skip
    assert audited[2][1]["requested_percent"] == [None, "12.00"]
    with pytest.raises(QuoteNotDraft):  # sus líneas quedan congeladas hasta que se decida
        discount(shop, seller, made, phone, "NONE", version=2)


@pytest.mark.parametrize(
    ("kind", "value", "pending"),
    [("AMOUNT", "3.01", False), ("AMOUNT", "3.02", True), ("PERCENT", "3", False),
     ("PERCENT", "3.01", True)],
)  # fmt: skip
def test_the_limit_is_never_exceeded_by_rounding(
    shop: SimpleNamespace, kind: str, value: str, pending: bool
) -> None:
    """Con un tope del 3 %: 3,01 sobre 100,50 es 2,995 %, que sube a 3,00 y cabe; 3,02 es 3,0049 %,
    que sube a 3,01 y pide aprobación."""
    seller = member(shop, "3")
    made, phone, case = draft(shop, seller)
    target = case if kind == "AMOUNT" else phone
    done = discount(shop, seller, made, target, kind, value)
    assert (done.status == "PENDING_APPROVAL") is pending


def test_the_highest_policy_of_the_roles_of_a_person_decides_and_no_policy_means_zero(
    shop: SimpleNamespace,
) -> None:
    both = member(shop, "3")
    add_role(shop, both, "10")  # un segundo rol, más alto, para la misma persona
    made, phone, _ = draft(shop, both)
    assert discount(shop, both, made, phone, "PERCENT", "8").status == "DRAFT"
    bare = member(shop)  # sin política: 0 %
    other, line, _ = draft(shop, bare)
    assert discount(shop, bare, other, line, "PERCENT", "1").status == "PENDING_APPROVAL"


def test_a_policy_of_another_price_list_does_not_apply_and_one_of_this_list_does(
    shop: SimpleNamespace,
) -> None:
    seller = member(shop, "10", list_id=price_list(shop.a, "OTRA"))
    made, phone, _ = draft(shop, seller)
    assert discount(shop, seller, made, phone, "PERCENT", "5").status == "PENDING_APPROVAL"
    add_role(shop, seller, "10", list_id=shop.listed)  # la de esta lista
    again, line, _ = draft(shop, seller)
    assert discount(shop, seller, again, line, "PERCENT", "5").status == "DRAFT"


def test_the_amount_cap_of_a_policy_counts_even_within_the_percent(shop: SimpleNamespace) -> None:
    seller = member(shop, "10", cap="100")
    made, phone, _ = draft(shop, seller)  # 5 % del iPhone son 219,95: pasa de 100
    assert discount(shop, seller, made, phone, "PERCENT", "5").status == "PENDING_APPROVAL"
    again, _, case = draft(shop, seller)  # 2 x 3,00 = 6,00 de las cajas: cabe
    assert discount(shop, seller, again, case, "AMOUNT", "3").status == "DRAFT"
    exact = member(shop, "10", cap="219.95")  # justo el tope: cabe (y un céntimo menos, no)
    third, line, _ = draft(shop, exact)
    assert discount(shop, exact, third, line, "PERCENT", "5").status == "DRAFT"


def test_the_owner_has_no_limit(shop: SimpleNamespace) -> None:
    owner = member(shop, owner=True)
    made, phone, _ = draft(shop, owner)
    assert discount(shop, owner, made, phone, "PERCENT", "60").status == "DRAFT"


def test_a_wrong_value_a_line_of_another_quote_or_a_stale_version_writes_nothing(
    shop: SimpleNamespace, migrator: DB
) -> None:
    seller = member(shop, "100")
    made, phone, _ = draft(shop, seller)
    bad = [("X", "5"), ("PERCENT", 1.5), ("PERCENT", "abc"), ("PERCENT", "-1"),
           ("PERCENT", "100.01"), ("PERCENT", "100"), ("PERCENT", "5.555"), ("PERCENT", None),
           ("AMOUNT", "4399.00"), ("AMOUNT", "5000"), ("AMOUNT", True), ("NONE", "5")]  # fmt: skip
    for kind, value in bad:
        with pytest.raises(ValueError):  # noqa: PT011
            discount(shop, seller, made, phone, kind, value)
    for version in (0, True, "1", None):
        with pytest.raises(ValueError):  # noqa: PT011
            discount(shop, seller, made, phone, "NONE", version=version)
    with pytest.raises(StaleVersion):
        discount(shop, seller, made, phone, "NONE", version=9)
    other, _, _ = draft(shop, seller)
    with pytest.raises(QuoteItem.DoesNotExist):  # una línea de otra cotización
        discount(shop, seller, made, stored(shop.a, other)[0], "NONE")
    with pytest.raises(Quote.DoesNotExist):
        act(shop, seller, discounts.set_line_discount, quote_id=uuid4(), version=1,
            line_id=phone.pk, discount_type="NONE")  # fmt: skip
    assert [a for a, _ in audit(migrator)] == ["quote.created", "quote.created"]
    assert stored(shop.a, made)[0].final_unit_price == D("4399.00")


def test_without_the_permission_nothing_is_discounted_or_overridden(shop: SimpleNamespace) -> None:
    plain = member(shop, "10")
    made, phone, _ = draft(shop, plain)
    stripped = make_user()  # con `prices.view`, pero sin `quotes.discount`
    with tenant_scope(ctx(shop.a)):
        membership = Membership.objects.create(user=stripped)
    give(shop.a, membership.pk, {"prices.view": None})
    with pytest.raises(NotAllowed):
        discount(shop, stripped, made, phone, "PERCENT", "1")
    with pytest.raises(NotAllowed):  # tener `quotes.discount` no basta para un precio manual
        act(shop, plain, discounts.set_manual_price, quote_id=made.pk, version=1,
            line_id=phone.pk, price="4000", reason="x")  # fmt: skip
    assert reread(shop, made).version == 1 and approvals(shop) == []


def manual(shop: SimpleNamespace, user: User, made: Quote, line: QuoteItem, price: Any,
           reason: Any = "Cliente mayorista", version: int = 1) -> Quote:  # fmt: skip
    done: Quote = act(shop, user, discounts.set_manual_price, quote_id=made.pk, version=version,
               line_id=line.pk, price=price, reason=reason)  # fmt: skip
    return done


def test_a_manual_price_covering_the_cost_always_asks_for_approval_and_never_shows_the_cost(
    shop: SimpleNamespace, migrator: DB
) -> None:
    boss = member(shop, "100", **{"quotes.override_price": None})
    cost(shop, shop.iphone, "3000")
    made, phone, _ = draft(shop, boss)
    done = manual(shop, boss, made, phone, "3500", reason="  Cliente DNI 12345678 ")
    assert (done.status, done.total) == ("PENDING_APPROVAL", D("3701.00"))  # 3500 + 2 x 100,50
    line = stored(shop.a, made)[0]
    assert (line.manual_price_override, line.override_reason) == (True, "Cliente DNI 12345678")
    assert (line.final_unit_price, line.unit_price, line.discount_amount) == (
        D("3500.00"), D("4399.00"), D("899.00"),
    )  # fmt: skip
    [asked] = approvals(shop)
    assert asked.reason == "MANUAL_PRICE" and asked.requested_by_user_id == boss.pk
    body = json.dumps(audit(migrator))
    assert "12345678" not in body and "[texto]" in body and "3000" not in body  # ni texto ni costo


@pytest.mark.parametrize(
    ("amount", "included", "currency", "price", "accepted"),
    [
        ("3000", True, "PEN", "3000.00", True),  # el costo mismo cubre
        ("3000", True, "PEN", "2999.99", False),
        ("2500", False, "PEN", "2950.00", True),  # sin IGV: 2500 x 1,18
        ("2500", False, "PEN", "2949.99", False),
        ("1000", True, "USD", "3500", False),  # otra moneda: no se puede comparar
    ],
)  # fmt: skip
def test_the_cost_floor_decides_a_manual_price_and_the_refusal_says_nothing(
    shop: SimpleNamespace, amount: str, included: bool, currency: str, price: str, accepted: bool
) -> None:
    boss = member(shop, "100", **{"quotes.override_price": None})
    cost(shop, shop.iphone, amount, tax_included=included, currency=currency)
    made, phone, _ = draft(shop, boss)
    if accepted:
        assert manual(shop, boss, made, phone, price).status == "PENDING_APPROVAL"
        return
    with pytest.raises(PriceRefused) as refused:
        manual(shop, boss, made, phone, price)
    assert str(refused.value) == "" and refused.value.args == ()  # sin cifras ni motivo
    assert reread(shop, made).version == 1 and approvals(shop) == []


def test_a_variant_without_a_cost_cannot_be_priced_by_hand_and_the_values_are_validated(
    shop: SimpleNamespace,
) -> None:
    boss = member(shop, "100", **{"quotes.override_price": None})
    made, phone, case = draft(shop, boss)
    with pytest.raises(PriceRefused):  # sin costo no se puede comprobar: falla cerrado
        manual(shop, boss, made, phone, "4000")
    cost(shop, shop.iphone, "1000")
    for price, reason in [("0", "x"), ("4399.00", "x"), ("5000", "x"), (1.5, "x"), ("abc", "x"),
                          ("-3", "x"), ("4000", ""), ("4000", "   "), ("4000", 5),
                          ("4000", "x" * 201)]:  # fmt: skip
        with pytest.raises(ValueError):  # noqa: PT011
            manual(shop, boss, made, phone, price, reason)
    assert case and reread(shop, made).version == 1


def test_the_discount_cannot_be_used_to_clear_a_manual_price_without_asking_again(
    shop: SimpleNamespace,
) -> None:
    boss = member(shop, "100", **{"quotes.override_price": None})
    cost(shop, shop.iphone, "1000")
    made, phone, _ = draft(shop, boss)
    manual(shop, boss, made, phone, "3500")
    with pytest.raises(QuoteNotDraft):  # esperando aprobación no se cambia nada
        discount(shop, boss, made, phone, "NONE", version=2)
    act(shop, boss, discounts.withdraw_approval_request, quote_id=made.pk, version=2)
    cleared = discount(shop, boss, made, phone, "NONE", version=3)  # y de vuelta a la lista
    assert (cleared.status, stored(shop.a, made)[0].manual_price_override) == ("DRAFT", False)


def submit(shop: SimpleNamespace, user: User, made: Quote, version: int = 1) -> Quote:
    done: Quote = act(shop, user, discounts.submit_quote, quote_id=made.pk, version=version)
    return done


def test_a_quote_is_approved_only_by_submit_and_only_inside_the_limit_of_who_submits(
    shop: SimpleNamespace, migrator: DB
) -> None:
    careful = member(shop, "3")
    senior = member(shop, "10")
    made, phone, _ = draft(shop, senior)
    discount(shop, senior, made, phone, "PERCENT", "5")  # cabe en el 10 % de quien lo pone
    # quien lo envía tiene un 3 %: el descuento ya no cabe y no pasa directo a APPROVED
    asked = submit(shop, careful, made, version=2)
    assert asked.status == "PENDING_APPROVAL"
    [req] = approvals(shop)
    assert (req.reason, req.requested_by_user_id, req.limit_percent_at_request) == (
        "DISCOUNT_OVER_LIMIT", careful.pk, D("3.00"),
    )  # fmt: skip
    with pytest.raises(QuoteNotDraft):
        submit(shop, careful, made, version=3)
    other, line, _ = draft(shop, senior)
    discount(shop, senior, other, line, "PERCENT", "5")
    assert submit(shop, senior, other, version=2).status == "APPROVED"  # dentro de su límite
    plain, _, _ = draft(shop, careful)
    assert submit(shop, careful, plain).status == "APPROVED"  # sin descuento
    assert [a for a, _ in audit(migrator)].count("quote.submitted") == 3
    with pytest.raises(StaleVersion):
        submit(shop, senior, draft(shop, senior)[0], version=4)


def test_submitting_a_manual_price_asks_for_approval_even_for_the_owner(
    shop: SimpleNamespace,
) -> None:
    owner = member(shop, owner=True, **{"quotes.override_price": None})
    cost(shop, shop.iphone, "1000")
    made, phone, _ = draft(shop, owner)
    manual(shop, owner, made, phone, "3500")  # ya esperaba: se retira y se envía de nuevo
    act(shop, owner, discounts.withdraw_approval_request, quote_id=made.pk, version=2)
    assert submit(shop, owner, made, version=3).status == "PENDING_APPROVAL"
    assert [a.status for a in approvals(shop)] == ["CANCELLED", "PENDING"]


def test_the_request_is_withdrawn_and_the_quote_goes_back_to_draft(shop: SimpleNamespace) -> None:
    seller = member(shop, "10")
    made, phone, _ = draft(shop, seller)
    discount(shop, seller, made, phone, "PERCENT", "12")
    with pytest.raises(StaleVersion):
        act(shop, seller, discounts.withdraw_approval_request, quote_id=made.pk, version=1)
    back = act(shop, seller, discounts.withdraw_approval_request, quote_id=made.pk, version=2)
    assert (back.status, back.version) == ("DRAFT", 3)
    [asked] = approvals(shop)
    assert asked.status == "CANCELLED" and asked.decided_at is not None
    with pytest.raises(NoOpenRequest):  # ya no hay nada que retirar
        act(shop, seller, discounts.withdraw_approval_request, quote_id=made.pk, version=3)
    assert discount(shop, seller, made, phone, "PERCENT", "5", version=3).status == "DRAFT"


def test_two_changes_at_once_leave_one_and_a_stale_version(shop: SimpleNamespace) -> None:
    seller = member(shop, "10")
    made, phone, _ = draft(shop, seller)
    one = partial(discounts.set_line_discount, quote_id=made.pk, version=1, line_id=phone.pk,
                  discount_type="PERCENT", discount_value="5")  # fmt: skip
    first, second = race(shop.a, seller, [one, one])
    assert (first.version, second) == (2, "StaleVersion")
    again = partial(discounts.submit_quote, quote_id=made.pk, version=2)
    ok, stale = race(shop.a, seller, [again, again])
    assert (ok.status, stale) == ("APPROVED", "StaleVersion")


def test_a_person_of_another_organization_cannot_touch_the_quote(shop: SimpleNamespace) -> None:
    seller = member(shop, "10")
    made, phone, _ = draft(shop, seller)
    with tenant_scope(ctx(shop.b)):
        membership = Membership.objects.get(user=shop.luis)
    give(shop.b, membership.pk, ALLOW)
    with tenant_scope(ctx(shop.b, shop.luis)) as tenant, pytest.raises(Quote.DoesNotExist):
        discounts.set_line_discount(tenant, quote_id=made.pk, version=1, line_id=phone.pk,
                                    discount_type="NONE")  # fmt: skip
    assert reread(shop, made).version == 1


def test_a_promotion_is_not_the_sellers_discount_and_the_percent_is_measured_per_line_too(
    shop: SimpleNamespace,
) -> None:
    promo(shop.a, [shop.listed], [{"variant_id": shop.iphone}],
          starts=NOW - timedelta(days=1), ends=NOW + timedelta(days=1))  # fmt: skip
    bare = member(shop)  # 0 %
    promoted, _, _ = draft(shop, bare)  # el iPhone ya cuesta 3959,10 por la promoción
    assert submit(shop, bare, promoted).status == "APPROVED"  # nada quitado a mano: no pide
    seller = member(shop, "10")
    made, _, case = draft(shop, seller)  # 60 % de las cajas: 2,6 % de toda la cotización
    assert discount(shop, seller, made, case, "PERCENT", "60").status == "PENDING_APPROVAL"
    assert approvals(shop)[-1].requested_discount_percent == D("60.00")


def test_a_percent_is_rounded_like_every_amount_and_a_cap_in_another_currency_never_fits(
    shop: SimpleNamespace,
) -> None:
    seller = member(shop, "10")
    made, phone, _ = draft(shop, seller)
    discount(shop, seller, made, phone, "PERCENT", "5.55")  # 244,1445: se redondea a 244,14
    assert stored(shop.a, made)[0].final_unit_price == D("4154.86")
    role_only = member(shop)
    add_role(shop, role_only, "10", cap="1000")
    with tenant_scope(ctx(shop.a)):
        DiscountPolicy.objects.filter(max_discount_amount=D("1000")).update(currency="USD")
    other, line, _ = draft(shop, role_only)  # 219,95 PEN contra un tope de 1.000 USD: no se compara
    assert discount(shop, role_only, other, line, "PERCENT", "5").status == "PENDING_APPROVAL"


def test_the_request_records_the_limit_of_that_moment_and_zero_when_there_is_no_policy(
    shop: SimpleNamespace,
) -> None:
    bare = member(shop)  # sin política: 0 %
    made, phone, _ = draft(shop, bare)
    assert discount(shop, bare, made, phone, "PERCENT", "1").status == "PENDING_APPROVAL"
    [req] = approvals(shop)
    assert (req.limit_percent_at_request, req.requested_discount_percent) == (D("0.00"), D("1.00"))


def test_the_percent_of_a_line_is_applied_rounded_to_the_nearest_cent(
    shop: SimpleNamespace,
) -> None:
    seller = member(shop, "10")
    made, phone, _ = draft(shop, seller)
    discount(shop, seller, made, phone, "PERCENT", "0.05")  # 2,1995: sube a 2,20, no baja a 2,19
    assert stored(shop.a, made)[0].final_unit_price == D("4396.80")


def test_the_whole_quote_is_measured_over_the_list_price_not_over_the_promotion_price(
    shop: SimpleNamespace,
) -> None:
    promo(shop.a, [shop.listed], [{"variant_id": shop.iphone}],
          starts=NOW - timedelta(days=1), ends=NOW + timedelta(days=1))  # fmt: skip
    seller = member(shop, "8.2")
    made, phone, _ = draft(shop, seller)  # el iPhone: 4399,00 de lista, 3959,10 por la promoción
    # 9 % de 3959,10 son 356,32: 8,10 % del precio de lista de la línea y 7,75 % de la cotización
    # (sobre 4.160,10 serían 8,57 %: el límite se mide sobre lo de lista)
    assert discount(shop, seller, made, phone, "PERCENT", "9").status == "DRAFT"
    assert approvals(shop) == []
