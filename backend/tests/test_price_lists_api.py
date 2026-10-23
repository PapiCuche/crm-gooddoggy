"""F3-40b: las rutas de las listas de precios (`…/pricing/price-lists/`). Middleware, sesión,
motor de autorización y PostgreSQL con el rol `crm_app`. Las reglas de la predeterminada y la
concurrencia se prueban en el servicio (`test_price_lists`)."""

from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from django.test import Client

from apps.access.models import MembershipRole
from apps.access.services import clone_role_templates
from apps.pricing.models import PriceList
from core.tenancy.scope import tenant_scope
from tests import test_authorization, test_self_context
from tests.factories import make_user
from tests.test_authorization import give
from tests.test_member_branch import DENIED
from tests.test_memberships import ctx, join
from tests.test_products import send
from tests.test_self_context import NOT_FOUND, reply, signed

world, real_stack = test_authorization.world, test_self_context.real_stack
pytestmark = pytest.mark.usefixtures("tenant_db")
LISTS = "/api/v1/o/org-a/pricing/price-lists/"
FIELDS = "id code name kind currency is_default required_permission ai_exposable payment_condition"
FIELDS += " is_active sort_order"


def create(client: Client, code: str, kind: str = "RETAIL", **body: Any) -> Any:
    return send(
        client, "post", LISTS, {"code": code, "name": f"Lista {code}", "kind": kind, **body}
    )


def patch(client: Client, list_id: Any, body: Any) -> Any:
    return send(client, "patch", f"{LISTS}{list_id}/", body)


def codes(client: Client) -> list[str]:
    response = client.get(LISTS)
    assert response.status_code == 200, response.content
    return [row["code"] for row in response.json()["results"]]


@pytest.fixture
def shop(orgs: dict[str, UUID]) -> SimpleNamespace:
    """Org A con sus plantillas: a Owner, v Vendedora, s Supervisor, x Administrador."""
    a, people = orgs["A"], {name: make_user(email=f"{name}@example.com") for name in "avsx"}
    with tenant_scope(ctx(a)) as scope:
        roles = {role.code: role for role in clone_role_templates(scope)}
    for name, code in (("a", "owner"), ("v", "seller"), ("s", "supervisor"), ("x", "admin")):
        membership = join(a, people[name]).pk
        with tenant_scope(ctx(a)):
            MembershipRole.objects.create(membership_id=membership, role=roles[code])
    return SimpleNamespace(b=orgs["B"], **{n: signed(user) for n, user in people.items()})


def test_the_owner_creates_regular_and_wholesale_and_a_seller_sees_only_regular(
    shop: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    regular = create(shop.a, "regular", name="Regular", currency="PEN")
    assert regular.status_code == 201, regular.content
    body = regular.json()
    assert set(body) == set(FIELDS.split())
    assert [body[k] for k in ("code", "is_default", "required_permission")] == [
        "REGULAR",
        True,
        "prices.view",
    ]
    wholesale = create(shop.a, "MAYORISTA", "WHOLESALE", name="Mayorista").json()
    assert wholesale["required_permission"] == "prices.view_wholesale"
    assert codes(shop.a) == codes(shop.x) == codes(shop.s) == ["REGULAR", "MAYORISTA"]
    assert codes(shop.v) == ["REGULAR"]  # la Vendedora no ve la mayorista
    assert shop.v.get(f"{LISTS}{body['id']}/").json() == body
    assert reply(shop.v.get(f"{LISTS}{wholesale['id']}/")) == NOT_FOUND  # como si no existiera
    assert reply(shop.v.get(f"{LISTS}{uuid4()}/")) == NOT_FOUND
    for client in (shop.v, shop.s):  # sin `prices.manage`: ni crea ni cambia
        assert reply(create(client, "OTRA")) == DENIED
        assert reply(patch(client, body["id"], {"name": "x"})) == DENIED
    changed = patch(shop.x, body["id"], {"name": " Precio  regular ", "sort_order": 2})
    assert changed.status_code == 200 and changed.json()["name"] == "Precio regular"
    query = "SELECT action FROM audit_logs WHERE entity_type = 'price_list' ORDER BY occurred_at"
    assert [row[0] for row in migrator.execute(query).fetchall()] == [
        "price_list.created",
        "price_list.created",
        "price_list.updated",
    ]


def test_reading_needs_prices_view_and_each_list_its_permission(world: Any) -> None:
    give(world.a, world.membership, {"prices.view": None, "prices.manage": None})
    ana = signed(world.ana)
    made = {kind: create(ana, kind, kind).json()["id"] for kind in ("RETAIL", "INTERNAL")}
    assert codes(ana) == ["RETAIL", "INTERNAL"]  # `prices.manage` las ve todas
    other = make_user(email="otro@example.com")
    give(world.a, join(world.a, other).pk, {"prices.view": None})
    assert reply(signed(other).get(f"{LISTS}{made['INTERNAL']}/")) == NOT_FOUND
    manager = make_user(email="gestor@example.com")  # sin `prices.view` la ruta no se abre
    give(world.a, join(world.a, manager).pk, {"prices.manage": None})
    assert reply(signed(manager).get(LISTS)) == DENIED


def test_the_errors_of_the_contract(world: Any) -> None:
    give(world.a, world.membership, {"prices.view": None, "prices.manage": None})
    ana = signed(world.ana)
    made = create(ana, "regular").json()
    taken = create(ana, "Regular")
    assert taken.status_code == 409 and taken.json()["code"] == "PRICE_LIST_CODE_TAKEN"
    fixed = patch(ana, made["id"], {"code": "OTRO", "currency": "PEN", "name": "x"})
    assert fixed.status_code == 400 and set(fixed.json()["fields"]) == {"code", "currency"}
    for wrong in ("5", "null", '"code"', '["code"]'):  # no es un objeto: 400, nunca un 500
        assert patch(ana, made["id"], wrong).json()["code"] == "VALIDATION_ERROR", wrong
    for bad in (
        {"name": "  "},
        {"kind": "PROMO"},
        {"currency": "USD"},  # D-PRC-2: solo PEN
        {"required_permission": "prices.manage"},
        {"payment_condition": "CHEQUE"},
        {"sort_order": -1},
        {"is_default": True, "is_active": False},
    ):
        refused = create(ana, "OTRA", **bad)
        assert refused.status_code == 400, bad
        assert set(refused.json()["fields"]) == (set(bad) - {"is_active"}), bad
    refused = create(ana, "VIP", "WHOLESALE", is_default=True)  # restringida: no puede serlo
    assert refused.status_code == 400 and set(refused.json()["fields"]) == {"is_default"}
    refused = create(ana, "ſ1")
    assert refused.status_code == 400 and set(refused.json()["fields"]) == {"code"}
    for unmark in (
        {"is_active": False},
        {"is_default": False},
        {"required_permission": "prices.view_internal"},
    ):
        refused = patch(ana, made["id"], unmark)
        assert refused.status_code == 409 and refused.json()["code"] == "PRICE_LIST_IS_DEFAULT"
        assert "elige otra" in refused.json()["message"]
    draft = create(ana, "BORRADOR", is_active=False).json()
    refused = patch(ana, draft["id"], {"is_default": True})
    assert refused.status_code == 400 and set(refused.json()["fields"]) == {"is_default"}
    wholesale = create(ana, "MAYORISTA", "WHOLESALE").json()
    refused = patch(ana, wholesale["id"], {"is_default": True})  # restringida
    assert refused.status_code == 400 and set(refused.json()["fields"]) == {"is_default"}
    with tenant_scope(ctx(world.b)):
        foreign = PriceList.objects.create(
            code="AJENA",
            name="Ajena",
            kind="RETAIL",
            currency="PEN",
            required_permission="prices.view",
        )
    assert reply(ana.get(f"{LISTS}{foreign.pk}/")) == NOT_FOUND
    assert reply(patch(ana, foreign.pk, {"name": "x"})) == NOT_FOUND
    assert reply(patch(ana, uuid4(), {"name": "x"})) == NOT_FOUND
    assert codes(ana) == ["REGULAR", "BORRADOR", "MAYORISTA"]
