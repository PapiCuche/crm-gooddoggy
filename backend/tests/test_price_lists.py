"""F3-40: los permisos de precios, la tabla `price_lists`, sus comandos y quién ve cada lista,
contra PostgreSQL real con el rol `crm_app`, también con dos transacciones a la vez."""

from collections.abc import Callable
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from django.db import IntegrityError

from apps.access.catalog import BY_CODE, ROLE_TEMPLATES
from apps.access.models import MembershipRole
from apps.access.selectors import execution_context
from apps.access.services import clone_role_templates
from apps.accounts.models import User
from apps.pricing import price_lists as commands
from apps.pricing.models import PriceList
from apps.pricing.selectors import visible_price_lists
from core.tenancy.scope import tenant_scope
from tests import test_authorization
from tests.factories import make_user
from tests.test_authorization import give
from tests.test_inventory_stock import race
from tests.test_memberships import ctx, join

world = test_authorization.world
pytestmark = pytest.mark.usefixtures("tenant_db")
READ = ("prices.view", "prices.view_wholesale", "prices.view_distributor", "prices.view_internal")
STARRED = ("prices.manage", "prices.bulk_update", "product_cost.view", "product_cost.manage")
PRICING = (*READ, *STARRED, "promotions.manage")


def run(world: Any, command: Callable[..., Any], **fields: Any) -> Any:
    with tenant_scope(ctx(world.a, world.ana)) as tenant:
        return command(tenant, **fields)


def create(world: Any, code: str, kind: str = "RETAIL", **fields: Any) -> PriceList:
    wanted = {"code": code, "name": code, "kind": kind, **fields}
    made: PriceList = run(world, commands.create_price_list, **wanted)
    return made


def update(world: Any, made: PriceList, **fields: Any) -> PriceList:
    changed: PriceList = run(world, commands.update_price_list, price_list_id=made.pk, **fields)
    return changed


def visible(org: UUID, user: User) -> list[str]:
    with tenant_scope(ctx(org, user)) as tenant:
        lists = visible_price_lists(execution_context(tenant)).order_by("id")
        return list(lists.values_list("code", flat=True))


def defaults(org: UUID) -> list[str]:
    with tenant_scope(ctx(org)):
        return list(PriceList.objects.filter(is_default=True).values_list("code", flat=True))


def audit(migrator: psycopg.Connection[Any]) -> list[tuple[str, str, Any]]:
    query = (
        "SELECT action, entity_label, changes FROM audit_logs WHERE entity_type = 'price_list' "
        "ORDER BY occurred_at, id"
    )
    return list(migrator.execute(query).fetchall())


def test_the_catalog_and_the_templates_carry_the_pricing_permissions_of_03_h() -> None:
    for code in PRICING:
        permission = BY_CODE[code]
        assert (permission.module, permission.supports_scope) == ("prices", False)
        assert permission.is_sensitive is (code in STARRED), code
    grants = {template.code: set(template.grants) & set(PRICING) for template in ROLE_TEMPLATES}
    assert grants["owner"] == set(PRICING)
    assert grants["admin"] == set(PRICING) - {"product_cost.view", "product_cost.manage"}
    assert grants["supervisor"] == {"prices.view", "prices.view_wholesale"}
    assert grants["seller"] == {"prices.view"}


def test_regular_and_wholesale_and_who_sees_each(
    world: Any, migrator: psycopg.Connection[Any]
) -> None:
    """La aceptación de F3-40 en el servicio: la ruta llega en F3-40b."""
    people = {code: make_user(email=f"{code}@example.com") for code in ("seller", "supervisor")}
    with tenant_scope(ctx(world.a)) as scope:
        roles = {role.code: role for role in clone_role_templates(scope)}
        MembershipRole.objects.create(membership_id=world.membership, role=roles["owner"])
    for code, user in people.items():
        membership = join(world.a, user).pk
        with tenant_scope(ctx(world.a)):
            MembershipRole.objects.create(membership_id=membership, role=roles[code])
    regular = create(world, " regular ", currency="PEN")
    assert (regular.code, regular.is_default, regular.required_permission) == (
        "REGULAR",
        True,  # la primera activa de su moneda
        "prices.view",
    )
    assert (regular.ai_exposable, regular.is_active, regular.payment_condition) == (
        False,
        True,
        None,
    )
    wholesale = create(world, "MAYORISTA", "WHOLESALE")
    assert (wholesale.is_default, wholesale.required_permission) == (False, "prices.view_wholesale")
    assert visible(world.a, world.ana) == visible(world.a, people["supervisor"])
    assert visible(world.a, world.ana) == ["REGULAR", "MAYORISTA"]
    assert visible(world.a, people["seller"]) == ["REGULAR"]  # la Vendedora no ve la mayorista
    assert [(action, label) for action, label, _ in audit(migrator)] == [
        ("price_list.created", "REGULAR"),
        ("price_list.created", "MAYORISTA"),
    ]
    assert audit(migrator)[0][2]["required_permission"] == [None, "prices.view"]


def test_each_list_is_seen_with_its_permission_or_prices_manage(world: Any) -> None:
    kinds = ("RETAIL", "WHOLESALE", "DISTRIBUTOR", "INTERNAL")
    made = [create(world, kind, kind).required_permission for kind in kinds]
    assert made == list(READ)  # por el tipo
    vip = create(world, "VIP", "CUSTOM", required_permission="prices.view_internal")
    assert vip.required_permission == "prices.view_internal"
    for held, seen in (
        ({}, []),
        ({"prices.view": None, "prices.view_distributor": None}, ["RETAIL", "DISTRIBUTOR"]),
        ({"prices.view_internal": None}, ["INTERNAL", "VIP"]),
        ({"prices.manage": None}, [*kinds, "VIP"]),  # las ve todas
        ({"prices.bulk_update": None, "product_cost.view": None}, []),
    ):
        user = make_user(email=f"{uuid4().hex}@example.com")
        give(world.a, join(world.a, user).pk, dict(held))
        assert visible(world.a, user) == seen, held


def test_one_default_per_currency_that_is_never_deactivated(
    world: Any, migrator: psycopg.Connection[Any]
) -> None:
    draft = create(world, "BORRADOR", is_active=False)  # inactiva: no es la predeterminada
    assert draft.is_default is False and defaults(world.a) == []
    regular = create(world, "REGULAR")
    assert regular.is_default is True
    cash = create(world, "EFECTIVO", "CASH", is_default=True, payment_condition="CASH")
    assert cash.is_default is True and defaults(world.a) == ["EFECTIVO"]  # en la misma tx
    assert audit(migrator)[-2][1:] == ("REGULAR", {"is_default": [True, False]})
    with tenant_scope(ctx(world.a)):  # quien le quitó la marca
        assert PriceList.objects.get(code="REGULAR").updated_by_user_id == world.ana.pk
    assert update(world, regular, is_default=True).is_default is True
    assert defaults(world.a) == ["REGULAR"]
    for wrong in ({"is_active": False}, {"is_default": False}):
        with pytest.raises(commands.PriceListIsDefault):
            update(world, regular, **wrong)
    assert update(world, cash, is_active=False).is_active is False
    with pytest.raises(commands.InactiveDefault):  # inactiva: no puede serlo
        update(world, cash, is_default=True)
    with pytest.raises(commands.InactiveDefault):
        create(world, "OTRA", is_default=True, is_active=False)
    both = update(world, cash, is_default=True, is_active=True)
    assert both.is_default is True and defaults(world.a) == ["EFECTIVO"]
    assert update(world, regular, is_active=False).is_active is False
    with tenant_scope(ctx(world.a)):  # sin predeterminada (solo en la base): activar la marca
        PriceList.objects.filter(code="EFECTIVO").update(is_default=False, is_active=False)
        PriceList.objects.filter(code="BORRADOR").update(updated_by_user_id=None)
    assert update(world, regular, name="Regular viejo").is_default is False  # renombrar no marca
    kept = update(world, regular, is_active=True, is_default=False)  # el `false` explícito vale
    assert (kept.is_active, kept.is_default, defaults(world.a)) == (True, False, [])
    assert update(world, regular, name="Regular").is_default is False  # activa: tampoco
    activated = update(world, draft, is_active=True)
    assert (activated.is_default, activated.updated_by_user_id) == (True, world.ana.pk)
    assert audit(migrator)[-1][1:] == (
        "BORRADOR",
        {"is_active": [False, True], "is_default": [False, True]},
    )
    before = len(audit(migrator))
    assert update(world, draft, is_active=True, name=" BORRADOR ").name == "BORRADOR"
    assert len(audit(migrator)) == before  # sin cambios no escribe ni audita


def test_the_default_is_seen_by_whoever_sees_prices(world: Any) -> None:
    """03 §H: `prices.view` es la lista por defecto; 04 §K.2: Regular, RETAIL, `prices.view`."""
    wholesale = create(world, "MAYORISTA", "WHOLESALE")  # la primera, pero restringida
    assert wholesale.is_default is False and defaults(world.a) == []
    with pytest.raises(commands.RestrictedDefault):
        update(world, wholesale, is_default=True)
    with pytest.raises(commands.RestrictedDefault):
        create(world, "VIP", "CUSTOM", required_permission="prices.view_internal", is_default=True)
    assert create(world, "SIN", is_default=False).is_default is False  # el `false` explícito
    regular = create(world, "REGULAR")
    assert regular.is_default is True and regular.updated_by_user_id == world.ana.pk
    with pytest.raises(commands.PriceListIsDefault):  # cambiar quién ve la predeterminada
        update(world, regular, required_permission="prices.view_wholesale")
    assert defaults(world.a) == ["REGULAR"]


def test_code_and_currency_do_not_change_and_the_input_is_checked(world: Any) -> None:
    made = create(world, "regular")
    with pytest.raises(commands.PriceListCodeTaken):
        create(world, "Regular")
    for fixed in ({"code": "OTRO"}, {"currency": "PEN"}):
        with pytest.raises(ValueError, match="no admitidos"):
            update(world, made, **fixed)
    for bad in ({"code": "ſ1"}, {"name": "  "}, {"currency": "USD"}):  # D-PRC-2: solo PEN
        with pytest.raises(ValueError):
            create(world, **{"code": "OTRA", **bad})
    with pytest.raises(PriceList.DoesNotExist):
        run(world, commands.update_price_list, price_list_id=uuid4(), name="x")
    assert visible(world.a, world.ana) == []  # sin permisos de precios no ve ninguna
    with tenant_scope(ctx(world.a)):
        assert list(PriceList.objects.values_list("code", "name")) == [("REGULAR", "regular")]


def test_isolation_and_what_the_database_refuses(world: Any) -> None:
    with tenant_scope(ctx(world.b)):
        foreign = PriceList.objects.create(
            code="AJENA", name="Ajena", kind="RETAIL", currency="PEN", required_permission=READ[0]
        )
    give(world.a, world.membership, {"prices.manage": None})
    assert visible(world.a, world.ana) == []
    with pytest.raises(PriceList.DoesNotExist):
        update(world, foreign, name="x")
    assert create(world, "AJENA").is_default is True  # el código y la predeterminada, por org
    base = {"name": "x", "kind": "RETAIL", "currency": "PEN", "required_permission": READ[0]}
    for broken in (
        {"code": "SEGUNDA", "is_default": True},  # dos predeterminadas en PEN
        {"code": "MINUS", "currency": "pen"},
        {"code": "X", "required_permission": "prices.manage"},
        {"code": "Y", "deleted_by_user_id": uuid4()},  # no es miembro
        {"code": "Z", "created_by_user_id": world.ana.pk, "kind": "PROMO"},
        {"code": "W", "payment_condition": "CHEQUE"},
        {"code": "V", "currency": "USD", "is_default": True, "required_permission": READ[1]},
    ):
        with pytest.raises(IntegrityError), tenant_scope(ctx(world.a)):
            PriceList.objects.create(**{**base, **broken})
    with pytest.raises(IntegrityError), tenant_scope(ctx(world.a)):  # predeterminada inactiva
        PriceList.objects.filter(code="AJENA").update(is_active=False)


def test_two_first_lists_at_once_leave_one_default(world: Any) -> None:
    def first(code: str) -> Any:
        return lambda tenant: commands.create_price_list(
            tenant, code=code, name=code, kind="RETAIL"
        )

    outcome = race(world.a, world.ana, [first("UNA"), first("OTRA")])
    assert [made.is_default for made in outcome] == [True, False]
    assert defaults(world.a) == ["UNA"]


def test_two_lists_marked_default_at_once_leave_one(world: Any) -> None:
    made = [create(world, code) for code in ("UNA", "OTRA", "TERCERA")]

    def mark(price_list: PriceList) -> Any:
        return lambda tenant: commands.update_price_list(
            tenant, price_list_id=price_list.pk, is_default=True
        )

    outcome = race(world.a, world.ana, [mark(made[1]), mark(made[2])])
    assert [changed.is_default for changed in outcome] == [True, True]
    assert defaults(world.a) == ["TERCERA"]  # la última en confirmar
