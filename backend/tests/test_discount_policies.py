"""F5-09b4: `set_discount_policy` (`apps.quotes.policies`) con PostgreSQL real y `crm_app`: la
autoridad de quien actúa (permiso y «nadie da más de lo que tiene»), los datos que no sirven, las
referencias de otra organización, la versión y las carreras, la auditoría y el efecto inmediato
sobre el disparador de `quotes`."""

import json
from decimal import Decimal as D
from functools import partial
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from django.db import IntegrityError

from apps.access.models import Role
from apps.access.selectors import AccessDenied, Denied
from apps.access.services import RoleHasDependents, clone_role_templates, delete_role
from apps.quotes import policies
from apps.quotes.models import DiscountPolicy, Quote
from apps.quotes.policies import DuplicatePolicy, Invalid, StalePolicy
from apps.quotes.seed import ensure_template_policies
from core.tenancy.scope import tenant_scope
from tests.test_inventory_stock import race
from tests.test_memberships import ctx
from tests.test_prices import price_list
from tests.test_quote_commands import shop, world
from tests.test_quote_discounts import BLOCKED, act, add_role, discount, draft, member, raw
from tests.test_quote_guards import role

_ = shop, world  # las fixtures
pytestmark = pytest.mark.usefixtures("tenant_db")
type DB = psycopg.Connection[Any]
MANAGE = {"discount_policies.manage": None}
SET = policies.set_discount_policy


def put(shop: SimpleNamespace, who: Any, role_id: Any, percent: Any = "5", **fields: Any) -> Any:
    return act(shop, who, SET, role_id=role_id, max_discount_percent=percent, **fields)


def stored(shop: SimpleNamespace) -> list[DiscountPolicy]:
    with tenant_scope(ctx(shop.a)):
        return list(DiscountPolicy.objects.order_by("id"))


def audits(migrator: DB) -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
    found = migrator.execute(
        "SELECT action, changes::text, metadata::text FROM audit_logs"
        " WHERE entity_type = 'discount_policy' ORDER BY occurred_at, id"
    ).fetchall()
    return [(a, json.loads(c), json.loads(m)) for a, c, m in found]


def role_of(shop: SimpleNamespace, user: Any) -> Any:
    with tenant_scope(ctx(shop.a)):
        return Role.objects.get(assignments__membership__user=user).pk


@pytest.fixture
def staff(shop: SimpleNamespace) -> SimpleNamespace:
    """Un administrador con un 100 %, un seller y un rol propio para asignar."""
    boss = member(shop, "100", **MANAGE)
    return SimpleNamespace(boss=boss, seller=member(shop), target=role(shop.a, "gerencia"))


def test_without_the_permission_nothing_is_written(
    shop: SimpleNamespace, staff: SimpleNamespace, migrator: DB
) -> None:
    """Sin el permiso, también quien tiene límite de sobra; y quien no es miembro, tampoco."""
    plain, before = member(shop, "100"), len(stored(shop))
    for who in (staff.seller, plain):
        with pytest.raises(AccessDenied) as denied:
            put(shop, who, staff.target)
        assert denied.value.reason is Denied.PERMISSION
    with pytest.raises(AccessDenied) as stranger, tenant_scope(ctx(shop.a, shop.luis)) as tenant:
        SET(tenant, role_id=staff.target, max_discount_percent="1")
    assert stranger.value.reason is Denied.MEMBERSHIP
    assert len(stored(shop)) == before and audits(migrator) == []


def test_nobody_gives_a_limit_above_the_one_they_hold(
    shop: SimpleNamespace, staff: SimpleNamespace, migrator: DB
) -> None:
    capped = member(shop, "10", cap="100", **MANAGE)  # 10 %, con tope de 100 PEN
    mine, usd = role_of(shop, capped), price_list(shop.a, "USD1", currency="USD")
    cap = {"max_discount_amount": "100.01", "currency": "PEN"}
    wanted: list[tuple[Any, str, dict[str, Any]]] = [
        (mine, "10.01", {}),  # su propio rol, un céntimo más
        (staff.target, "11", {}),  # otro rol
        (staff.target, "10", {}),  # sin tope: el suyo lo tiene
        (staff.target, "10", cap),  # un tope mayor
        (staff.target, "10", {"max_discount_amount": "50", "price_list_id": usd}),  # otra moneda
        (mine, "9.99", {}),  # bajar el porcentaje pero quitar el tope también da poder
    ]
    for target, percent, extra in wanted:
        with pytest.raises(AccessDenied) as denied:
            put(shop, capped, target, percent, **extra)
        assert denied.value.reason is Denied.ESCALATION
    assert len(stored(shop)) == 2 and audits(migrator) == []  # nada cambió
    ok = put(shop, capped, staff.target, "10", max_discount_amount="100", currency="PEN")
    assert (ok.max_discount_percent, ok.max_discount_amount) == (D("10.00"), D("100.00"))
    low = put(shop, capped, mine, "9.99", max_discount_amount="99", currency="PEN", version=1)
    assert low.version == 2  # bajar no da poder
    nobody = member(
        shop, **MANAGE
    )  # administra, pero sin política propia: ni un 0 % (falla cerrado)
    with pytest.raises(AccessDenied):
        put(shop, nobody, staff.target, "0")


def test_touching_an_existing_policy_needs_to_cover_the_one_it_had_and_two_roles_do_not_mix(
    shop: SimpleNamespace, staff: SimpleNamespace
) -> None:
    low, mine = member(shop, "10", **MANAGE), role_of(shop, staff.seller)
    high, seller = put(shop, staff.boss, staff.target, "50"), put(shop, staff.boss, mine, "8")
    with pytest.raises(AccessDenied):  # rebajar a un rol de 50 %: 0 cabe, pero lo que tenía no
        put(shop, low, staff.target, "0", version=1)
    with pytest.raises(AccessDenied):  # ni quitarle la política, ni la del administrador
        act(shop, low, policies.delete_discount_policy, policy_id=high.pk, version=1)
    with pytest.raises(AccessDenied):
        act(shop, low, policies.delete_discount_policy, policy_id=stored(shop)[0].pk, version=1)
    down = put(shop, low, mine, "2", version=seller.version)  # un 8 % sí: cabe en su 10 %
    assert put(shop, low, mine, "10", version=down.version).version == 3  # y volver a subirlo
    both = member(shop, "10", cap="100", **MANAGE)
    add_role(shop, both, "5")  # un segundo rol: 5 % sin tope; no se mezclan (10 % sin tope)
    fresh = role(shop.a)
    with pytest.raises(AccessDenied):
        put(shop, both, fresh, "10")
    assert put(shop, both, fresh, "5").version == 1  # lo cubre el segundo
    capped = put(shop, both, role(shop.a), "10", max_discount_amount="100", currency="PEN")
    assert capped.max_discount_percent == D("10.00")  # y esto, el primero


def test_a_limit_for_one_list_is_covered_only_by_that_list_or_by_all(
    shop: SimpleNamespace, staff: SimpleNamespace
) -> None:
    here, other = price_list(shop.a, "AQUI"), price_list(shop.a, "OTRA")
    narrow = member(shop, "20", list_id=here, **MANAGE)
    for percent, extra in [("20", {}), ("5", {"price_list_id": other})]:  # todas, y otra lista
        with pytest.raises(AccessDenied):
            put(shop, narrow, staff.target, percent, **extra)
    assert (
        put(shop, narrow, staff.target, "20", price_list_id=here).applies_to_price_list_id == here
    )
    assert put(shop, staff.boss, staff.target, "20").applies_to_price_list_id is None  # el 100 %


def test_the_owner_role_has_no_policy_and_the_owner_is_not_capped(shop: SimpleNamespace) -> None:
    owner = member(shop, owner=True, **MANAGE)
    with pytest.raises(Invalid) as bad:  # el Owner es ilimitado: una fila engañaría
        put(shop, owner, role_of(shop, owner))
    assert bad.value.field == "role_id"
    assert (
        put(shop, owner, role(shop.a), "100", max_discount_amount="1", currency="PEN").version == 1
    )


@pytest.mark.parametrize(
    ("field", "fields"),
    [
        ("max_discount_percent", {"percent": "100.01"}),
        ("max_discount_percent", {"percent": "-1"}),
        ("max_discount_percent", {"percent": "10.005"}),  # no se redondea
        ("max_discount_percent", {"percent": 10.5}),  # un float ya perdió la exactitud
        ("max_discount_percent", {"percent": "NaN"}),
        ("max_discount_percent", {"percent": "Infinity"}),
        ("max_discount_percent", {"percent": "1e2"}),
        ("max_discount_percent", {"percent": True}),
        ("max_discount_percent", {"percent": None}),
        ("max_discount_amount", {"max_discount_amount": "-0.01", "currency": "PEN"}),
        ("max_discount_amount", {"max_discount_amount": 1.5, "currency": "PEN"}),
        ("currency", {"max_discount_amount": "10"}),  # el tope lleva su moneda...
        ("currency", {"currency": "PEN"}),  # ...y la moneda solo va con el tope
        ("currency", {"max_discount_amount": "10", "currency": "pen"}),
        ("currency", {"list": "usd", "currency": "PEN"}),  # la de la lista
        ("version", {"version": 0}),
        ("version", {"version": True}),
        ("role_id", {"role": "random"}),  # un rol que no existe...
        ("role_id", {"role": "foreign"}),  # ...y uno de otra organización: el mismo error
        ("price_list_id", {"list": "random"}),
        ("price_list_id", {"list": "foreign"}),
    ],
)
def test_data_that_does_not_serve_is_a_field_error_and_writes_nothing(
    shop: SimpleNamespace, staff: SimpleNamespace, migrator: DB, field: str, fields: dict[str, Any]
) -> None:
    extra = {k: v for k, v in fields.items() if k not in ("percent", "role", "list")}
    target = {"random": uuid4(), "foreign": role(shop.b)}.get(fields.get("role", ""), staff.target)
    lists = {"random": uuid4(), "foreign": price_list(shop.b, "DE-B"),
             "usd": price_list(shop.a, "USD1", currency="USD")}  # fmt: skip
    listed = lists.get(fields.get("list", ""))
    with pytest.raises(Invalid) as bad:
        put(shop, staff.boss, target, fields.get("percent", "5"), price_list_id=listed, **extra)
    assert bad.value.field == field
    assert len(stored(shop)) == 1 and audits(migrator) == []


def test_one_policy_per_role_and_list_and_a_version_to_change_it(
    shop: SimpleNamespace, staff: SimpleNamespace, migrator: DB
) -> None:
    usd = price_list(shop.a, "USD1", currency="USD")
    first = put(shop, staff.boss, staff.target, "5")
    with pytest.raises(DuplicatePolicy):  # sin versión: no se pisa en silencio
        put(shop, staff.boss, staff.target, "6")
    other = put(shop, staff.boss, staff.target, "6", price_list_id=usd)  # otra lista: otra fila
    assert (first.pk != other.pk, other.currency, len(stored(shop))) == (
        True,
        "USD",
        3,
    )  # la de la lista
    with pytest.raises(StalePolicy):  # una versión para lo que no existe
        put(shop, staff.boss, role(shop.a), "5", version=1)
    same = put(shop, staff.boss, staff.target, "5", version=1)  # lo mismo: ni versión ni auditoría
    assert (same.version, len(audits(migrator))) == (1, 2)
    changed = put(shop, staff.boss, staff.target, "7.50", version=1)
    assert (changed.version, changed.max_discount_percent) == (2, D("7.50"))
    with pytest.raises(StalePolicy):  # el segundo con la versión vieja
        put(shop, staff.boss, staff.target, "8", version=1)
    assert [r.max_discount_percent for r in stored(shop) if r.pk == first.pk] == [D("7.50")]


def test_the_audit_keeps_the_before_and_the_after_and_nothing_else(
    shop: SimpleNamespace, staff: SimpleNamespace, migrator: DB
) -> None:
    made = put(shop, staff.boss, staff.target, "5", max_discount_amount="10.5", currency="PEN")
    put(shop, staff.boss, staff.target, "5", version=1, max_discount_amount="12", currency="PEN")
    (_, created, meta), (_, updated, _) = audits(migrator)
    assert created == {"max_discount_percent": [None, "5.00"], "currency": [None, "PEN"],
                       "max_discount_amount": [None, "10.50"]}  # fmt: skip
    assert updated == {"max_discount_amount": ["10.50", "12.00"]}  # solo lo que cambió
    assert meta == {"operation": "created", "role": "gerencia", "price_list_id": None}
    who = migrator.execute(
        "SELECT actor_id, entity_id, entity_label FROM audit_logs"
        " WHERE entity_type = 'discount_policy' ORDER BY occurred_at, id LIMIT 1"
    )
    assert who.fetchone() == (staff.boss.pk, made.pk, "Rol")
    assert "cost" not in json.dumps(audits(migrator)).lower()  # la política no lleva costos


def test_two_creations_at_once_leave_one_policy_and_a_clean_conflict(
    shop: SimpleNamespace, staff: SimpleNamespace, migrator: DB
) -> None:
    one = partial(SET, role_id=staff.target, max_discount_percent="5")
    first, second = race(shop.a, staff.boss, [one, one])
    assert (first.version, second, len(stored(shop))) == (1, "DuplicatePolicy", 2)
    bump = partial(SET, role_id=staff.target, max_discount_percent="6", version=1)
    ok, stale = race(shop.a, staff.boss, [bump, bump])  # la misma versión dos veces
    assert (ok.version, stale, len(audits(migrator))) == (2, "StalePolicy", 2)


def test_a_failure_after_the_write_rolls_back_the_policy_and_its_audit(
    shop: SimpleNamespace, staff: SimpleNamespace, migrator: DB, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(policies, "record", lambda *a, **k: 1 / 0)
    with pytest.raises(ZeroDivisionError):
        put(shop, staff.boss, staff.target)
    assert len(stored(shop)) == 1 and audits(migrator) == []


def test_policies_of_another_organization_never_show_up_or_collide(
    shop: SimpleNamespace, staff: SimpleNamespace
) -> None:
    other = role(shop.b)
    with tenant_scope(ctx(shop.b)):
        DiscountPolicy.objects.create(role_id=other, max_discount_percent=D("50"))
    put(shop, staff.boss, staff.target, "5")
    with tenant_scope(ctx(shop.a)):
        assert {p.role_id for p in DiscountPolicy.objects.all()} == {
            staff.target,
            role_of(shop, staff.boss),
        }
        assert not DiscountPolicy.objects.filter(role_id=other).exists()  # RLS
    with pytest.raises(IntegrityError), tenant_scope(ctx(shop.a)):  # ni con SQL directo
        DiscountPolicy.objects.create(role_id=other, max_discount_percent=D("1"))


def test_a_policy_change_applies_at_once_to_the_database_rule_and_is_not_retroactive(
    shop: SimpleNamespace, staff: SimpleNamespace
) -> None:
    """El disparador de `quotes` lee la política en cada cambio de estado: subirla, bajarla o
    ponerle tope cambia al instante lo que `crm_app` puede hacer; lo ya aprobado no se mide."""
    seller, mine = staff.seller, role_of(shop, staff.seller)
    made, phone, _ = draft(shop, seller)
    discount(shop, staff.boss, made, phone, "PERCENT", "5")
    for percent, extra, version in [
        ("4.99", {}, None),
        ("5", {"max_discount_amount": "1", "currency": "PEN"}, 1),
    ]:
        row = put(shop, staff.boss, mine, percent, version=version, **extra)
        with pytest.raises(
            IntegrityError, match=BLOCKED
        ):  # sin política 0 %; 4,99 %; el tope manda
            raw(shop, seller, made, "APPROVED")
    put(shop, staff.boss, mine, "5", version=row.version)  # sin tope: ahora cabe
    raw(shop, seller, made, "APPROVED")
    put(shop, staff.boss, mine, "0", version=3)  # bajarlo no deshace lo aprobado
    raw(shop, seller, made, "SENT")
    other, line, _ = draft(shop, seller)
    discount(shop, staff.boss, other, line, "PERCENT", "5")
    with pytest.raises(IntegrityError, match=BLOCKED):  # pero un borrador nuevo ya no cabe
        raw(shop, seller, other, "APPROVED")
    with tenant_scope(ctx(shop.a)):
        assert Quote.objects.get(pk=made.pk).status == "SENT"


def test_a_role_with_a_policy_is_not_deleted_until_its_policy_is_removed_with_its_command(
    shop: SimpleNamespace, staff: SimpleNamespace, migrator: DB
) -> None:
    member(shop, owner=True)  # la organización tiene su Owner, pero no es quien borra
    owner = member(shop, **{"roles.manage": None})  # ni Owner ni `discount_policies.manage`
    row = put(shop, staff.boss, staff.target, "5")
    before = audits(migrator)
    with pytest.raises(RoleHasDependents):  # roles.manage no borra, en silencio, un límite
        act(shop, owner, delete_role, role_id=staff.target)
    assert (staff.target in {p.role_id for p in stored(shop)}, audits(migrator)) == (True, before)
    act(
        shop, staff.boss, policies.delete_discount_policy, policy_id=row.pk, version=1
    )  # con su permiso
    act(shop, owner, delete_role, role_id=staff.target)
    assert [a for a, _, _ in audits(migrator)] == ["discount_policy.changed"] * 2


def test_who_cannot_reach_the_percent_learns_nothing_about_roles_or_lists(
    shop: SimpleNamespace, staff: SimpleNamespace
) -> None:
    low, owner_role = member(shop, "10", **MANAGE), role_of(shop, member(shop, owner=True))
    for target, extra in [(owner_role, {}), (uuid4(), {}), (role(shop.b), {}),
                          (staff.target, {"price_list_id": uuid4()})]:  # fmt: skip
        with pytest.raises(AccessDenied):  # no `Invalid`: ni el Owner ni lo que no existe se nota
            put(shop, low, target, "11", **extra)
    with pytest.raises(Invalid):  # dentro de lo que tiene, sí se le dice
        put(shop, low, uuid4(), "10")
    here = price_list(shop.a, "AQUI")
    narrow = member(shop, "20", list_id=here, **MANAGE)  # el porcentaje se mide para esa lista
    asks = [(owner_role, uuid4()), (uuid4(), uuid4()), (staff.target, uuid4()),
            (staff.target, price_list(shop.a, "OTRA")), (owner_role, None)]  # fmt: skip
    for target, listed in asks:
        with pytest.raises(AccessDenied):
            put(shop, narrow, target, "5", price_list_id=listed)
    capped = member(shop, "50", cap="10", **MANAGE)  # y el tope, antes de las referencias
    with pytest.raises(AccessDenied):
        put(shop, capped, uuid4(), "5", max_discount_amount="11", currency="PEN")


def test_deleting_a_policy_id_tells_nothing_to_who_does_not_hold_everything(
    shop: SimpleNamespace, staff: SimpleNamespace
) -> None:
    """Quitar una política que no existe (o de otra organización) es, para quien no cubre el
    máximo, el mismo 403 que quitar una que no cubre; solo quien lo cubre todo oye que no existe."""
    high, low = put(shop, staff.boss, staff.target, "50"), member(shop, "10", **MANAGE)
    with tenant_scope(ctx(shop.b)):
        foreign = DiscountPolicy.objects.create(role_id=role(shop.b), max_discount_percent=D(1))
    for policy_id in (high.pk, uuid4(), foreign.pk):
        with pytest.raises(AccessDenied) as denied:
            act(shop, low, policies.delete_discount_policy, policy_id=policy_id, version=1)
        assert denied.value.reason is Denied.ESCALATION
    for policy_id in (uuid4(), foreign.pk):
        with pytest.raises(DiscountPolicy.DoesNotExist):
            act(shop, staff.boss, policies.delete_discount_policy, policy_id=policy_id, version=1)


def test_deleting_a_policy_needs_the_permission_and_the_version_and_the_limit_falls_at_once(
    shop: SimpleNamespace, staff: SimpleNamespace, migrator: DB
) -> None:
    row = put(shop, staff.boss, role_of(shop, staff.seller), "5")
    drafts = [draft(shop, staff.seller) for _ in "ab"]
    for made, phone, _ in drafts:
        discount(shop, staff.boss, made, phone, "PERCENT", "5")
    delete = partial(act, shop, staff.boss, policies.delete_discount_policy, policy_id=row.pk)
    with pytest.raises(AccessDenied):
        act(shop, staff.seller, policies.delete_discount_policy, policy_id=row.pk, version=1)
    with pytest.raises(StalePolicy):
        delete(version=2)
    raw(shop, staff.seller, drafts[0][0], "APPROVED")  # cabe en su 5 %...
    delete(version=1)
    with pytest.raises(IntegrityError, match=BLOCKED):  # ...y sin política, ya no
        raw(shop, staff.seller, drafts[1][0], "APPROVED")
    with pytest.raises(DiscountPolicy.DoesNotExist):  # repetirlo, o una de otra organización
        delete(version=1)
    assert audits(migrator)[-1][1] == {"max_discount_percent": ["5.00", None],
        "max_discount_amount": [None, None], "currency": [None, None]}  # fmt: skip


def test_new_organizations_get_the_template_limits_and_an_existing_choice_is_kept(
    shop: SimpleNamespace,
) -> None:
    with tenant_scope(ctx(shop.a)) as tenant:
        clone_role_templates(tenant)
        assert ensure_template_policies(tenant) == 3
        assert ensure_template_policies(tenant) == 0  # idempotente
        got = {
            p.role.code: p.max_discount_percent
            for p in DiscountPolicy.objects.select_related("role")
        }
    assert got == {"admin": D("100"), "supervisor": D("10"), "seller": D("3")}  # el Owner no lleva


def test_only_the_known_constraints_are_a_client_error(
    shop: SimpleNamespace, staff: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    lower = price_list(shop.a, "AQUI")
    monkeypatch.setattr(
        policies, "price_list_currency", lambda *a: "usd"
    )  # un dato que la base ve mal
    with pytest.raises(IntegrityError, match="discount_policies_amount_ck"):  # no es del cliente
        put(shop, staff.boss, staff.target, "5", price_list_id=lower, max_discount_amount="1")
