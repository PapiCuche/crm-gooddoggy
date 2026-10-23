"""F3-43 (E03-06, E03-10): `PricingService`, el motor de precios, contra PostgreSQL real con el
rol `crm_app`. Vigencias en sus bordes, zonas horarias, programados, variantes que no se venden,
quién ve cada lista, IA frente a persona, aislamiento y `resolve_many` frente a `resolve`."""

from dataclasses import fields, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.access.models import MembershipRole
from apps.access.selectors import execution_context
from apps.access.services import clone_role_templates
from apps.accounts.models import User
from apps.catalog.models import Product, ProductVariant
from apps.pricing import prices, services
from apps.pricing.models import PriceList, ProductPrice
from apps.pricing.services import Audience, ListNotVisible, PriceQuery, PriceResolution, Reason
from core.tenancy.scope import tenant_scope
from tests import test_authorization
from tests.factories import make_user
from tests.test_authorization import give
from tests.test_memberships import ctx, join
from tests.test_prices import price_list

world = test_authorization.world
pytestmark = pytest.mark.usefixtures("tenant_db")
LIMA, US = ZoneInfo("America/Lima"), timedelta(microseconds=1)
T0 = datetime(2026, 3, 1, tzinfo=LIMA)  # 00:00 en Lima: 05:00 en UTC
T1 = datetime(2026, 3, 15, 12, 30, tzinfo=LIMA)
_emails = iter(range(10_000))


def item(org: UUID, sku: str = "IP16", product: str = "ACTIVE", **variant: Any) -> UUID:
    """Una variante de un producto, los dos `ACTIVE` y visibles para la IA salvo que se diga."""
    deleted, product_ai = variant.pop("deleted", ""), variant.pop("product_ai", True)
    with tenant_scope(ctx(org)):
        made = Product.objects.create(
            name=sku,
            slug=sku.lower(),
            product_type="PHYSICAL",
            status=product,
            ai_visible=product_ai,
        )
        one = ProductVariant.objects.create(product=made, sku=sku, name=sku, **variant)
        gone = {"deleted_at": timezone.now()}
        if deleted == "variant":
            ProductVariant.objects.filter(pk=one.pk).update(**gone)
        if deleted == "product":
            Product.objects.filter(pk=made.pk).update(**gone)
    return UUID(str(one.pk))


def row(
    org: UUID, where: tuple[UUID, UUID], amount: str, starts: datetime, ends: Any = None
) -> UUID:
    """Un precio con la vigencia que se quiera, también pasada: `set_price` no las escribe."""
    with tenant_scope(ctx(org)):
        made = ProductPrice.objects.create(
            variant_id=where[0],
            price_list_id=where[1],
            amount=Decimal(amount),
            currency="PEN",
            starts_at=starts,
            ends_at=ends,
            change_reason="Semilla del test",
        )
    return UUID(str(made.pk))


def person(world: Any, grants: dict[str, str | None], org: UUID | None = None) -> User:
    user = make_user(email=f"p{next(_emails)}@example.com")
    give(org or world.a, join(org or world.a, user).pk, grants)
    return user


def human(org: UUID, user: User, query: PriceQuery) -> PriceResolution:
    with tenant_scope(ctx(org, user)) as tenant:
        return services.resolve(execution_context(tenant), query)


def public_ai(org: UUID, query: PriceQuery) -> PriceResolution:
    with tenant_scope(ctx(org)) as tenant:  # un agente no tiene membresía: basta el tenant
        return services.resolve(tenant, replace(query, audience=Audience.PUBLIC_AI))


def amount(found: PriceResolution) -> Decimal | Reason | None:
    return found.final_amount if found.found else found.reason


def test_4599_now_and_4399_tomorrow_then_no_price_and_a_seller_without_wholesale(
    world: Any,
) -> None:
    """La aceptación de F3-43 en el servicio: la ruta llega en F3-44."""
    with tenant_scope(ctx(world.a)) as scope:
        roles = {role.code: role for role in clone_role_templates(scope)}
    seller = make_user(email="vendedora@example.com")
    for user, code in ((world.ana, "owner"), (seller, "seller")):
        membership = world.membership if user == world.ana else join(world.a, seller).pk
        with tenant_scope(ctx(world.a)):
            MembershipRole.objects.create(membership_id=membership, role=roles[code])
    where = (item(world.a), price_list(world.a, is_default=True))
    tomorrow = timezone.now() + timedelta(days=1)
    with tenant_scope(ctx(world.a, world.ana)) as tenant:
        today = prices.set_price(
            tenant, variant_id=where[0], price_list_id=where[1], amount="4599", reason="Lanzamiento"
        )
        later = prices.set_price(
            tenant,
            variant_id=where[0],
            price_list_id=where[1],
            amount="4399",
            reason="Octubre",
            starts_at=tomorrow,
        )
    now = human(world.a, seller, PriceQuery(where[0]))  # sin lista: la predeterminada
    assert now == PriceResolution(
        found=True,
        reason=None,
        variant_id=where[0],
        price_list_id=where[1],
        currency="PEN",
        tax_included=True,
        base_amount=Decimal("4599.00"),
        final_amount=Decimal("4599.00"),
        savings=Decimal("0.00"),
        valid_until=tomorrow,
        price_id=today.pk,
        resolved_at=now.resolved_at,
    )
    then = human(world.a, seller, PriceQuery(where[0], where[1], tomorrow))
    assert (then.final_amount, then.valid_until, then.price_id) == (
        Decimal("4399.00"),
        None,
        later.pk,
    )
    bare = human(world.a, seller, PriceQuery(item(world.a, "IP15"), where[1]))
    assert bare == PriceResolution(
        found=False,
        reason=Reason.NO_PRICE,
        variant_id=bare.variant_id,
        price_list_id=where[1],
        currency="PEN",
        resolved_at=bare.resolved_at,
    )
    wholesale = price_list(
        world.a, "MAYORISTA", kind="WHOLESALE", required_permission="prices.view_wholesale"
    )
    row(world.a, (where[0], wholesale), "4100", T0)
    with pytest.raises(ListNotVisible):
        human(world.a, seller, PriceQuery(where[0], wholesale))
    assert amount(human(world.a, world.ana, PriceQuery(where[0], wholesale))) == Decimal("4100")


def test_the_resolution_has_no_cost_fields_ever() -> None:
    assert [field.name for field in fields(PriceResolution)] == [
        "found",
        "reason",
        "variant_id",
        "price_list_id",
        "currency",
        "tax_included",
        "base_amount",
        "promotion",
        "final_amount",
        "savings",
        "valid_until",
        "price_id",
        "promotion_id",
        "resolved_at",
        "evidence_kind",
    ]


def test_every_instant_has_at_most_one_price_start_included_end_excluded(world: Any) -> None:
    """Casos tabulados (Hypothesis no es dependencia): cada borde, un microsegundo antes y
    después, en Lima y en UTC. El oráculo es la tabla, no el motor."""
    where = (item(world.a), price_list(world.a))
    spans = [("90", T0 - timedelta(days=30), T0), ("100", T0, T1), ("120", T1, None)]
    ids = [row(world.a, where, value, start, end) for value, start, end in spans]
    viewer = person(world, {"prices.view": None})
    edges = [T0 - timedelta(days=30), T0, T1]
    instants = [edge + step for edge in edges for step in (-US, timedelta(0), US)]
    instants += [
        datetime(2026, 2, 28, 23, 59, 59, 999999, tzinfo=LIMA),  # 04:59:59 UTC del 1 de marzo
        datetime(2026, 3, 1, 4, 59, 59, tzinfo=UTC),  # el mismo día en UTC, aún el 28 en Lima
        datetime(2026, 3, 1, 5, tzinfo=UTC),  # el inicio, escrito en UTC
        datetime(2026, 3, 1, tzinfo=UTC),  # medianoche UTC: las 19:00 del 28 en Lima
    ]
    for at in instants:
        ruling = [
            n
            for n, (_, start, end) in enumerate(spans)
            if start <= at and (end is None or at < end)
        ]
        assert len(ruling) <= 1
        found = human(world.a, viewer, PriceQuery(where[0], where[1], at))
        if not ruling:
            assert (found.found, found.reason, found.price_id) == (False, Reason.NO_PRICE, None)
            continue
        value, _, end = spans[ruling[0]]
        assert (found.price_id, found.final_amount, found.valid_until) == (
            ids[ruling[0]],
            Decimal(value),
            end,
        ), at
    lima_eve = human(world.a, viewer, PriceQuery(where[0], where[1], T0 - US))
    assert lima_eve.final_amount == Decimal("90")  # aún el precio del día anterior en Lima
    with pytest.raises(ValueError, match="zona horaria"):
        human(world.a, viewer, PriceQuery(where[0], where[1], datetime(2026, 3, 1)))  # noqa: DTZ001


def test_without_at_it_is_the_transaction_now(world: Any) -> None:
    where = (item(world.a), price_list(world.a))
    row(world.a, where, "100", T0)
    viewer = person(world, {"prices.view": None})
    with tenant_scope(ctx(world.a, viewer)) as tenant, connection.cursor() as cursor:
        cursor.execute("SELECT now()")
        began = cursor.fetchone()[0]
        found = services.resolve(execution_context(tenant), PriceQuery(where[0], where[1]))
    assert (found.resolved_at, found.final_amount) == (began, Decimal("100"))


@pytest.mark.parametrize(
    "state",
    [
        {"status": "INACTIVE"},
        {"status": "DISCONTINUED"},
        {"product": "DRAFT"},
        {"product": "DISCONTINUED"},
        {"deleted": "variant"},
        {"deleted": "product"},
    ],
)
def test_a_variant_that_is_not_for_sale_has_no_price(world: Any, state: dict[str, str]) -> None:
    where = (item(world.a, **state), price_list(world.a))
    row(world.a, where, "100", T0)
    found = human(
        world.a, person(world, {"prices.manage": None}), PriceQuery(where[0], where[1], T1)
    )
    assert (found.found, found.reason, found.base_amount, found.price_id) == (
        False,
        Reason.VARIANT_UNAVAILABLE,
        None,
        None,
    )


def test_who_sees_each_list_and_the_default_per_currency(world: Any) -> None:
    v = item(world.a)
    lists = {  # antes que la predeterminada de PEN: el motor no elige la primera que encuentra
        "USD": price_list(world.a, "USD", currency="USD", is_default=True),
        "MAYORISTA": price_list(world.a, "MAYORISTA", required_permission="prices.view_wholesale"),
        "INACTIVA": price_list(world.a, "INACTIVA", is_active=False),
        "BORRADA": price_list(world.a, "BORRADA", deleted_at=T0),
        "REGULAR": price_list(world.a, is_default=True),
    }
    for code, value in (
        ("REGULAR", "100"),
        ("MAYORISTA", "80"),
        ("INACTIVA", "70"),
        ("BORRADA", "60"),
    ):
        row(world.a, (v, lists[code]), value, T0)
    other = price_list(world.b, "REGULAR", is_default=True)
    viewer, manager = person(world, {"prices.view": None}), person(world, {"prices.manage": None})
    wholesaler = person(world, {"prices.view_wholesale": None})
    cases = [
        (viewer, "REGULAR", Decimal("100")),
        (viewer, "MAYORISTA", ListNotVisible),
        (wholesaler, "MAYORISTA", Decimal("80")),
        (wholesaler, "", ListNotVisible),  # la predeterminada pide prices.view
        (manager, "MAYORISTA", Decimal("80")),
        (manager, "INACTIVA", ListNotVisible),
        (manager, "BORRADA", ListNotVisible),
        (manager, "", Decimal("100")),  # la predeterminada de PEN, no la de USD
        (manager, "OTRA", ListNotVisible),  # la de otra organización no existe
        (manager, "NINGUNA", ListNotVisible),
    ]
    lists |= {"OTRA": other, "NINGUNA": uuid4()}
    for user, code, expected in cases:
        query = PriceQuery(v, lists[code] if code else None, T1)
        if expected is ListNotVisible:
            with pytest.raises(ListNotVisible):
                human(world.a, user, query)
        else:
            assert amount(human(world.a, user, query)) == expected, (code, expected)
    with tenant_scope(ctx(world.a)):
        PriceList.objects.filter(pk=lists["REGULAR"]).update(is_default=False)
    with pytest.raises(ListNotVisible):  # PEN sin predeterminada
        human(world.a, manager, PriceQuery(v, None, T1))
    with pytest.raises(TypeError), tenant_scope(ctx(world.a, manager)) as tenant:
        services.resolve(
            tenant, PriceQuery(v, lists["REGULAR"], T1)
        )  # una persona sin sus permisos


def test_public_ai_needs_an_exposable_list_and_a_visible_variant_and_product(world: Any) -> None:
    exposed = price_list(world.a, is_default=True, ai_exposable=True)
    hidden = price_list(world.a, "MAYORISTA", ai_exposable=False)
    gone = price_list(world.a, "BORRADA", ai_exposable=True, deleted_at=T0)
    internal = price_list(
        world.a,
        "INTERNA",
        kind="INTERNAL",
        required_permission="prices.view_internal",
        ai_exposable=True,
    )
    plain, no_variant, no_product = (
        item(world.a),
        item(world.a, "V2", ai_visible=False),
        item(world.a, "V3", product_ai=False),
    )
    for v in (plain, no_variant, no_product):
        for listed in (exposed, hidden, gone, internal):
            row(world.a, (v, listed), "100", T0)
    manager = person(world, {"prices.manage": None})
    assert amount(public_ai(world.a, PriceQuery(plain, exposed, T1))) == Decimal("100")
    assert amount(public_ai(world.a, PriceQuery(plain, None, T1))) == Decimal("100")
    for v in (no_variant, no_product):
        assert amount(public_ai(world.a, PriceQuery(v, exposed, T1))) == Reason.VARIANT_UNAVAILABLE
        assert amount(human(world.a, manager, PriceQuery(v, exposed, T1))) == Decimal("100")
    with pytest.raises(ListNotVisible):
        public_ai(world.a, PriceQuery(plain, hidden, T1))  # aunque una persona la vea
    with pytest.raises(ListNotVisible):
        public_ai(world.a, PriceQuery(plain, gone, T1))  # ni una borrada
    with pytest.raises(ListNotVisible):
        public_ai(world.a, PriceQuery(plain, internal, T1))  # ni la interna (04 §K.2)
    assert amount(human(world.a, manager, PriceQuery(plain, hidden, T1))) == Decimal("100")
    with tenant_scope(ctx(world.a)):
        PriceList.objects.filter(pk=exposed).update(ai_exposable=False)
    with pytest.raises(ListNotVisible):  # la predeterminada tampoco, si no se expone
        public_ai(world.a, PriceQuery(plain, None, T1))
    with pytest.raises(ListNotVisible):  # ni la de otra organización
        public_ai(world.b, PriceQuery(plain, exposed, T1))


def test_resolve_many_answers_like_resolve_in_constant_queries(world: Any) -> None:
    listed = price_list(world.a, is_default=True)
    page = [item(world.a, f"P{n}") for n in range(12)]
    for n, v in enumerate(page[:8]):
        row(world.a, (v, listed), f"{100 + n}", T0, T1 if n % 2 else None)
    page += [item(world.a, "OFF", status="INACTIVE"), item(world.b, "AJENA"), uuid4(), page[0]]
    viewer = person(world, {"prices.view": None})
    for at in (T0, T1 - US, T1):
        with tenant_scope(ctx(world.a, viewer)) as tenant:
            many = services.resolve_many(execution_context(tenant), page[::-1], None, at)
        one = {v: human(world.a, viewer, PriceQuery(v, None, at)) for v in page}
        assert many == one
        assert list(many) == list(dict.fromkeys(page[::-1]))  # en su orden, sin repetidas
    assert [amount(many[v]) for v in page[7:9]] == [Reason.NO_PRICE, Reason.NO_PRICE]
    assert {amount(many[v]) for v in page[12:15]} == {Reason.VARIANT_UNAVAILABLE}

    def count(size: int, at: datetime | None) -> int:
        with tenant_scope(ctx(world.a, viewer)) as tenant:
            ectx = execution_context(tenant)
            with CaptureQueriesContext(connection) as queries:
                services.resolve_many(ectx, page[:size], listed, at)
        return len(queries)

    assert count(1, T0) == count(15, T0) == 3  # la lista, las variantes, sus precios
    assert count(1, None) == count(15, None) == 4  # y `now()`
