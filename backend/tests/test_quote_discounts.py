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
from django.db import DataError, IntegrityError, connection
from django.utils import timezone

from apps.access.models import Role
from apps.accounts.models import User
from apps.organizations.models import OrganizationMembership as Membership
from apps.pricing import costs
from apps.pricing.models import VariantCost
from apps.quotes import commands, discounts
from apps.quotes.commands import NotAllowed, QuoteNotDraft, StaleVersion
from apps.quotes.discounts import PriceRefused
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
ALLOW: dict[str, str | None] = {"prices.view": None, "quotes.discount": None,
                                "quotes.create": None, "quotes.update": "ORGANIZATION"}  # fmt: skip


def add_role(shop: SimpleNamespace, user: User, percent: str | None = None, owner: bool = False,
             list_id: Any = None, cap: str | None = None, **grants: Any) -> None:  # fmt: skip
    """Otro rol de `user` en A, con esos permisos y, si se dice, una política con ese límite."""
    with tenant_scope(ctx(shop.a)):
        role = give(shop.a, Membership.objects.get(user=user).pk, {**ALLOW, **grants})
        if owner:
            Role.objects.filter(pk=role.pk).update(is_owner_role=True)
        if percent is not None:
            DiscountPolicy.objects.create(
                role_id=role.pk, max_discount_percent=D(percent), applies_to_price_list_id=list_id,
                max_discount_amount=None if cap is None else D(cap),
                currency=None if cap is None and list_id is None else "PEN",
            )  # fmt: skip


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
             value: Any = None, version: Any = 1) -> Any:  # fmt: skip
    return act(shop, user, discounts.set_line_discount, quote_id=made.pk, version=version,
               line_id=line.pk, discount_type=kind, discount_value=value)  # fmt: skip


def approvals(shop: SimpleNamespace) -> list[QuoteApproval]:
    with tenant_scope(ctx(shop.a)):
        return list(QuoteApproval.objects.order_by("created_at"))


def reread(shop: SimpleNamespace, made: Quote) -> Any:
    with tenant_scope(ctx(shop.a)):
        return Quote.objects.get(pk=made.pk)


def back_to_draft(shop: SimpleNamespace, made: Quote) -> None:  # como hará F5-09b3 al retirar
    with tenant_scope(ctx(shop.a)):
        QuoteApproval.objects.filter(quote_id=made.pk, status="PENDING").update(
            status="CANCELLED", decided_at=timezone.now()
        )
        Quote.objects.filter(pk=made.pk).update(status="DRAFT", version=3)


def cost(shop: SimpleNamespace, variant: Any, amount: str, **fields: Any) -> None:
    base = {"currency": "PEN", "source": "MANUAL", "change_reason": "Semilla", "tax_included": True,
            "starts_at": LONG_AGO}  # fmt: skip
    with tenant_scope(ctx(shop.a)):
        VariantCost.objects.create(variant_id=variant, amount=D(amount), **{**base, **fields})


def test_a_discount_within_the_limit_is_applied_and_asks_nothing(
    shop: SimpleNamespace, migrator: DB
) -> None:
    seller = member(shop, "10")
    made, phone, _ = draft(shop, seller)
    done = discount(shop, seller, made, phone, "PERCENT", "5")
    assert (done.status, done.version, done.total) == ("DRAFT", 2, D("4380.05"))  # 4179,05 + 201
    line = stored(shop.a, made)[0]
    shown = (line.discount_type, line.discount_value, line.discount_amount, line.final_unit_price)
    assert shown == ("PERCENT", D("5"), D("219.95"), D("4179.05")) and approvals(shop) == []
    assert [a for a, _ in audit(migrator)] == ["quote.created", "quote.line_discounted"]


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
    assert (asked.requested_discount_percent, asked.limit_percent_at_request) == (D("12"), D("10"))
    audited = audit(migrator)
    assert [a for a, _ in audited][1:] == ["quote.approval_requested", "quote.line_discounted"]
    assert audited[1][1]["requested_percent"] == [None, "12.00"]
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
    assert discount(shop, bare, *draft(shop, bare)[:2], "PERCENT", "1").status == "PENDING_APPROVAL"
    assert approvals(shop)[0].limit_percent_at_request == D("0.00")  # el límite de ese momento
    owner = member(shop, owner=True)  # el Owner no tiene límite
    assert discount(shop, owner, *draft(shop, owner)[:2], "PERCENT", "60").status == "DRAFT"


def test_a_policy_of_another_price_list_does_not_apply_and_one_of_this_list_does(
    shop: SimpleNamespace,
) -> None:
    seller = member(shop, "10", list_id=price_list(shop.a, "OTRA"))
    made, phone, _ = draft(shop, seller)
    assert discount(shop, seller, made, phone, "PERCENT", "5").status == "PENDING_APPROVAL"
    add_role(shop, seller, "10", list_id=shop.listed)  # la de esta lista
    assert discount(shop, seller, *draft(shop, seller)[:2], "PERCENT", "5").status == "DRAFT"


def test_the_amount_cap_of_a_policy_counts_even_within_the_percent(shop: SimpleNamespace) -> None:
    seller = member(shop, "10", cap="100")
    made, phone, _ = draft(shop, seller)  # 5 % del iPhone son 219,95: pasa de 100
    assert discount(shop, seller, made, phone, "PERCENT", "5").status == "PENDING_APPROVAL"
    exact = member(shop, "10", cap="219.95")  # justo el tope: cabe
    assert discount(shop, exact, *draft(shop, exact)[:2], "PERCENT", "5").status == "DRAFT"


def test_a_wrong_value_a_line_of_another_quote_or_a_stale_version_writes_nothing(
    shop: SimpleNamespace, migrator: DB
) -> None:
    seller = member(shop, "100")
    made, phone, _ = draft(shop, seller)
    bad = [("X", "5"), ("PERCENT", 1.5), ("PERCENT", "abc"), ("PERCENT", "-1"), ("PERCENT", "100"),
           ("PERCENT", "5.555"), ("PERCENT", None), ("AMOUNT", "4399.00"), ("AMOUNT", True),
           ("NONE", "5")]  # fmt: skip
    for kind, value in bad:
        with pytest.raises(ValueError):  # noqa: PT011
            discount(shop, seller, made, phone, kind, value)
    with pytest.raises(StaleVersion):
        discount(shop, seller, made, phone, "NONE", version=9)
    with pytest.raises(QuoteItem.DoesNotExist):  # una línea de otra cotización
        discount(shop, seller, made, stored(shop.a, draft(shop, seller)[0])[0], "NONE")
    with pytest.raises(Quote.DoesNotExist):
        act(shop, seller, discounts.set_line_discount, quote_id=uuid4(), version=1,
            line_id=phone.pk, discount_type="NONE")  # fmt: skip
    assert [a for a, _ in audit(migrator)] == ["quote.created"] * 2  # nada se escribió


def test_without_the_permission_nothing_is_discounted_or_overridden(shop: SimpleNamespace) -> None:
    made, phone, _ = draft(shop, plain := member(shop, "10"))
    stripped = make_user()  # con `prices.view` y `quotes.update`, pero sin `quotes.discount`
    give(shop.a, join(shop.a, stripped).pk, {"prices.view": None, "quotes.update": "ORGANIZATION"})
    with pytest.raises(NotAllowed):
        discount(shop, stripped, made, phone, "PERCENT", "1")
    with pytest.raises(NotAllowed):  # tener `quotes.discount` no basta para un precio manual
        act(shop, plain, discounts.set_manual_price, quote_id=made.pk, version=1,
            line_id=phone.pk, price="4000", reason="x")  # fmt: skip
    assert reread(shop, made).version == 1 and approvals(shop) == []


def manual(shop: SimpleNamespace, user: User, made: Quote, line: QuoteItem, price: Any,
           reason: Any = "Cliente mayorista", version: int = 1) -> Any:  # fmt: skip
    return act(shop, user, discounts.set_manual_price, quote_id=made.pk, version=version,
               line_id=line.pk, price=price, reason=reason)  # fmt: skip


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
    assert (line.final_unit_price, line.discount_amount) == (D("3500.00"), D("899.00"))
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
    bad = [("0", "x"), ("4399.00", "x"), (1.5, "x"), ("abc", "x"), ("-3", "x"), ("4000", ""),
           ("4000", " "), ("4000", 5), ("4000", "x" * 201)]  # fmt: skip
    for price, reason in bad:
        with pytest.raises(ValueError):  # noqa: PT011
            manual(shop, boss, made, phone, price, reason)
    assert case and reread(shop, made).version == 1


def submit(shop: SimpleNamespace, user: User, made: Quote, version: int = 1) -> Any:
    return act(shop, user, discounts.submit_quote, quote_id=made.pk, version=version)


def test_a_quote_is_approved_only_by_submit_and_only_inside_the_limit_of_who_submits(
    shop: SimpleNamespace,
) -> None:
    careful, senior = member(shop, "3"), member(shop, "10")
    made, phone, _ = draft(shop, senior)
    discount(shop, senior, made, phone, "PERCENT", "5")  # cabe en el 10 % de quien lo pone
    assert submit(shop, careful, made, version=2).status == "PENDING_APPROVAL"
    [req] = approvals(shop)
    assert (req.reason, req.requested_by_user_id, req.limit_percent_at_request) == (
        "DISCOUNT_OVER_LIMIT", careful.pk, D("3.00"),
    )  # fmt: skip
    with pytest.raises(QuoteNotDraft):
        submit(shop, careful, made, version=3)
    other, line, _ = draft(shop, senior)
    discount(shop, senior, other, line, "PERCENT", "5")
    assert submit(shop, senior, other, version=2).status == "APPROVED"  # dentro de su límite
    assert submit(shop, careful, draft(shop, careful)[0]).status == "APPROVED"  # sin descuento


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
        give(shop.b, Membership.objects.get(user=shop.luis).pk, ALLOW)
    with tenant_scope(ctx(shop.b, shop.luis)) as tenant, pytest.raises(Quote.DoesNotExist):
        discounts.set_line_discount(tenant, quote_id=made.pk, version=1, line_id=phone.pk,
                                    discount_type="NONE")  # fmt: skip
    assert reread(shop, made).version == 1


def test_a_promotion_is_not_the_sellers_discount_and_the_percent_is_measured_over_what_is_charged(
    shop: SimpleNamespace,
) -> None:
    promo(shop.a, [shop.listed], [{"variant_id": shop.iphone}],
          starts=NOW - timedelta(days=1), ends=NOW + timedelta(days=1))  # fmt: skip
    promoted, _, _ = draft(shop, bare := member(shop))  # 0 %; el iPhone ya cuesta 3959,10
    assert submit(shop, bare, promoted).status == "APPROVED"  # nada quitado a mano: no pide
    made, phone, case = draft(shop, seller := member(shop, "8.2"))  # 9 %: 8,10 % de la lista
    assert discount(shop, seller, made, phone, "PERCENT", "9").status == "PENDING_APPROVAL"
    assert approvals(shop)[-1].requested_discount_percent == D("9.01")
    other, _, line = draft(shop, seller)  # 60 % de las cajas: 2,9 % del total, 60 % de la línea
    assert discount(shop, seller, other, line, "PERCENT", "60").status == "PENDING_APPROVAL"
    assert approvals(shop)[-1].requested_discount_percent == D("60.00") and case


def test_a_foreign_currency_cap_never_fits_and_percents_round_half_up(
    shop: SimpleNamespace,
) -> None:
    seller = member(shop, "10")
    made, phone, _ = draft(shop, seller)
    discount(shop, seller, made, phone, "PERCENT", "5.55")  # 244,1445: se redondea a 244,14
    discount(shop, seller, made, stored(shop.a, made)[1], "PERCENT", "0.05", version=2)  # 0,05025
    assert [x.final_unit_price for x in stored(shop.a, made)] == [D("4154.86"), D("100.45")]
    role_only = member(shop, "10", cap="1000")
    with tenant_scope(ctx(shop.a)):
        DiscountPolicy.objects.filter(max_discount_amount=D("1000")).update(currency="USD")
    pending = discount(shop, role_only, *draft(shop, role_only)[:2], "PERCENT", "5")
    assert pending.status == "PENDING_APPROVAL"  # 219,95 PEN contra un tope de 1.000 USD


def test_an_own_scope_seller_cannot_touch_the_quote_of_someone_else(shop: SimpleNamespace) -> None:
    """`quotes.discount` no tiene alcance: manda el de `quotes.update` sobre la cotización."""
    senior = member(shop, "10", **{"quotes.override_price": None})
    made, phone, _ = draft(shop, senior)
    own = member(shop, "10", **{"quotes.override_price": None, "quotes.update": "OWN"})
    with tenant_scope(ctx(shop.b)):  # y alguien de otra organización con todos los permisos
        give(
            shop.b,
            Membership.objects.get(user=shop.luis).pk,
            {**ALLOW, "quotes.override_price": None},
        )
    seen = set()
    cases: list[tuple[Any, dict[str, Any]]] = [
        (discounts.set_line_discount, {"line_id": phone.pk, "discount_type": "NONE"}),
        (discounts.set_manual_price, {"line_id": phone.pk, "price": "4000", "reason": "x"}),
        (discounts.submit_quote, {}),
    ]
    for command, extra in cases:
        for org, who, target in ((shop.a, own, made.pk), (shop.a, own, uuid4()),
                                 (shop.b, shop.luis, made.pk)):  # fmt: skip
            with tenant_scope(ctx(org, who)) as tenant, pytest.raises(Quote.DoesNotExist) as gone:
                command(tenant, quote_id=target, version=1, **extra)
            seen.add((type(gone.value), str(gone.value), gone.value.args))
    assert len(seen) == 1  # fuera de alcance, inexistente y ajena: el mismo error y texto
    assert discount(shop, own, *draft(shop, own)[:2], "PERCENT", "5").status == "DRAFT"  # la suya
    with pytest.raises(Quote.DoesNotExist):  # y el alcance va antes que la versión: no se le cuenta
        discount(shop, own, made, phone, "NONE", version=9)
    assert reread(shop, made).version == 1


def test_submit_rechecks_the_cost_floor_and_a_manual_price_asks_approval_even_of_the_owner(
    shop: SimpleNamespace,
) -> None:
    owner = member(shop, owner=True, **{"quotes.override_price": None})
    cost(shop, shop.iphone, "1000")
    made, phone, _ = draft(shop, owner)
    manual(shop, owner, made, phone, "3500")
    with pytest.raises(QuoteNotDraft):  # mientras espera, no se escribe otro precio
        manual(shop, owner, made, phone, "3400", version=2)
    back_to_draft(shop, made)  # se retira y se envía de nuevo: también el Owner pide aprobación
    assert submit(shop, owner, made, version=3).status == "PENDING_APPROVAL"
    assert [a.status for a in approvals(shop)] == ["CANCELLED", "PENDING"]
    again, line, _ = draft(shop, owner)
    manual(shop, owner, again, line, "3500")
    back_to_draft(shop, again)
    act(shop, owner, costs.set_cost, variant_id=shop.iphone, amount="3600", reason="Subió")
    with pytest.raises(PriceRefused):  # el costo subió desde que se escribió el precio
        submit(shop, owner, again, version=3)
    assert (reread(shop, again).status, len(approvals(shop))) == ("DRAFT", 3)


def raw(shop: SimpleNamespace, user: Any, made: Quote, state: str,
        gucs: dict[str, str] | None = None) -> None:  # fmt: skip
    """Un `UPDATE` directo de `crm_app` como `user`; `gucs` cambia variables de la transacción."""
    with tenant_scope(ctx(shop.a, user)), connection.cursor() as cursor:
        for name, value in (gucs or {}).items():
            cursor.execute("SELECT set_config(%s, %s, true)", [name, value])
        Quote.objects.filter(pk=made.pk).update(status=state)


BLOCKED = "go through the request"


def test_the_database_itself_refuses_to_approve_or_send_a_draft_beyond_the_limit_of_who_acts(
    shop: SimpleNamespace,
) -> None:
    careful, senior, owner = member(shop, "3"), member(shop, "10"), member(shop, owner=True)
    boss = member(shop, "100", **{"quotes.override_price": None})
    cost(shop, shop.iphone, "1000")
    within, phone, _ = draft(shop, careful)
    discount(shop, careful, within, phone, "PERCENT", "2")
    raw(shop, careful, within, "APPROVED")  # dentro de su límite: la base lo deja
    for state in ("APPROVED", "SENT"):
        over, line, _ = draft(shop, senior)
        discount(shop, senior, over, line, "PERCENT", "5")  # cabe en el 10 % de senior...
        for who in (careful, None):  # ...no en el 3 % de careful, ni sin persona (la IA)
            with pytest.raises(IntegrityError, match=BLOCKED):
                raw(shop, who, over, state)
        raw(shop, owner, over, state)  # el Owner no tiene límite
        priced, line, _ = draft(shop, boss)
        manual(shop, boss, priced, line, "3500")
        back_to_draft(shop, priced)
        with pytest.raises(IntegrityError, match=BLOCKED):  # ni manual
            raw(shop, owner, priced, state)
    edge, _, case = draft(shop, owner)
    discount(shop, owner, edge, case, "AMOUNT", "3.02")  # 3,0049 % de la caja: sube a 3,01 %
    with pytest.raises(IntegrityError, match=BLOCKED):  # no cabe en el 3 %
        raw(shop, careful, edge, "APPROVED")
    promo(shop.a, [shop.listed], [{"variant_id": shop.iphone}],
          starts=NOW - timedelta(days=1), ends=NOW + timedelta(days=1))  # fmt: skip
    cheap, phone, _ = draft(shop, owner)  # el iPhone a 3959,10
    discount(shop, owner, cheap, phone, "PERCENT", "9")  # 8,10 % de la lista, 9,01 % de lo cobrado
    with pytest.raises(IntegrityError, match=BLOCKED):
        raw(shop, member(shop, "8.2"), cheap, "APPROVED")


def flip(shop: SimpleNamespace, user: User, what: str, on: bool) -> None:
    with tenant_scope(ctx(shop.a)):
        if what == "membership":
            Membership.objects.filter(user=user).update(status="ACTIVE" if on else "SUSPENDED")
        else:
            User.objects.filter(pk=user.pk).update(is_active=on)


def test_the_database_rule_counts_only_the_policies_the_command_counts(
    shop: SimpleNamespace,
) -> None:
    """El disparador cuenta las políticas que el comando: la lista de la cotización o todas, el
    porcentaje por línea, el tope en importe y su moneda, y una persona activa (no el permiso)."""
    owner, other = member(shop, owner=True), member(shop, "30", list_id=price_list(shop.a, "OTRA"))
    capped, usd = member(shop, "10", cap="100"), member(shop, "10", cap="1000")
    exact, here = member(shop, "10", cap="219.95"), member(shop, "5", list_id=shop.listed)
    with tenant_scope(ctx(shop.a)):
        DiscountPolicy.objects.filter(max_discount_amount=D("1000")).update(currency="USD")
    made, phone, _ = draft(shop, owner)
    discount(shop, owner, made, phone, "PERCENT", "5")  # 219,95 PEN: 5 % del iPhone
    for who in (other, capped, usd):  # otra lista; 219,95 pasa del tope de 100; tope en dólares
        with pytest.raises(IntegrityError, match=BLOCKED):
            raw(shop, who, made, "APPROVED")
    with pytest.raises(IntegrityError, match=BLOCKED):  # un agente de IA no es una persona
        raw(shop, exact, made, "APPROVED", {"app.actor_type": "AI_AGENT"})
    for actor in (exact, None):  # un identificador roto: el mismo error, con o sin miembro
        with pytest.raises(DataError):
            raw(shop, actor, made, "APPROVED", {"app.actor_type": "USER", "app.user_id": "x"})
    for what in ("membership", "user"):  # ni un miembro suspendido ni un usuario desactivado
        flip(shop, exact, what, False)
        with pytest.raises(IntegrityError, match=BLOCKED):
            raw(shop, exact, made, "APPROVED")
        flip(shop, exact, what, True)
    raw(shop, exact, made, "APPROVED")  # justo en el tope en importe: pasa
    again, line, _ = draft(shop, owner)
    discount(shop, owner, again, line, "PERCENT", "5")
    raw(shop, here, again, "APPROVED")  # justo el 5 % de la política de esta lista: pasa
