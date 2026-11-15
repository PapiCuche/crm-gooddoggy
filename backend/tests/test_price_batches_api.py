"""F3-49b: `…/pricing/batches/`, crear un lote de edición masiva y verlo, por HTTP con sesión, el
motor de autorización y PostgreSQL con el rol `crm_app`. Ningún precio cambia y ningún campo de
costo sale."""

from dataclasses import replace
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from django.test import Client

from apps.pricing import batches
from apps.pricing.api.batches import REFUSED
from core.tenancy.context import ActorType
from core.tenancy.resolution import resolve_tenant
from core.tenancy.scope import tenant_scope
from tests import test_price_batches, test_self_context
from tests.test_member_branch import DENIED
from tests.test_memberships import ctx
from tests.test_price_batches import EDITOR, create, lines
from tests.test_prices import logged, price_list
from tests.test_pricing_engine import person
from tests.test_products import send
from tests.test_promotion_tables import stock
from tests.test_self_context import NOT_FOUND, signed

world, shop = test_price_batches.world, test_price_batches.shop
real_stack = test_self_context.real_stack
pytestmark = pytest.mark.usefixtures("tenant_db")
BASE = "/api/v1/o/org-a/pricing/batches/"
GRANTS = {**EDITOR, "products.view": None}
BODY = {"id", "kind", "status", "price_list_id", "operation", "value", "rounding", "filters"}
BODY |= {"reason", "effective_at", "summary", "version", "created_at", "created_by_user_id"}
BODY |= {"applied_at", "applied_by_user_id"}
WRITES = "SELECT (SELECT count(*) FROM price_change_batches), (SELECT count(*) FROM audit_logs)"


def wanted(shop: Any, **fields: Any) -> dict[str, Any]:
    body = {"price_list_id": str(shop.regular), "operation": "PERCENT_DECREASE", "value": "5"}
    return {**body, "filters": {"category_id": str(shop.iphone)}, "reason": "Campaña", **fields}


def editor(world: Any, grants: dict[str, str | None] = GRANTS) -> Client:
    return signed(person(world, grants))


def post(client: Client, body: Any) -> Any:
    return send(client, "post", BASE, body)


def said(fields: Any) -> Any:
    """Los mensajes de `fields`, sin sus códigos: `{"value": ["Mayor que cero."]}`."""
    if isinstance(fields, list):
        return [said(entry) for entry in fields]
    if isinstance(fields, dict) and set(fields) == {"code", "message"}:
        return fields["message"]
    return {key: said(value) for key, value in fields.items()}


def test_minus_5_percent_to_iphone_in_regular_answers_201_with_the_counts(
    world: Any, shop: Any, migrator: psycopg.Connection[Any]
) -> None:
    """La aceptación de F3-49b: 201 con los recuentos, las líneas con antes y después, la que no
    tiene precio `SKIPPED`, ningún precio cambia y ningún campo de costo."""
    prices_before, client = logged(migrator, "product_prices", "*"), editor(world)
    answer = post(client, wanted(shop, rounding="INTEGER"))
    assert answer.status_code == 201, answer.content
    body = answer.json()
    assert set(body) == BODY  # ni costo ni margen ni suelo, a ninguna altura (ADR-006)
    assert not [word for word in ("cost", "margin", "floor") if word in answer.content.decode()]
    shown = {k: body[k] for k in ("kind", "status", "operation", "value", "rounding", "reason")}
    assert shown == {
        "kind": "BULK_ADJUST",
        "status": "PREVIEWED",
        "operation": "PERCENT_DECREASE",
        "value": "5.00",
        "rounding": "INTEGER",
        "reason": "Campaña",
    }
    none = dict.fromkeys(("brand_id", "product_id", "variant_ids"))
    assert body["filters"] == {"category_id": str(shop.iphone), **none}
    assert (body["price_list_id"], body["effective_at"], body["version"]) == (
        str(shop.regular),
        None,
        1,
    )
    counts = {"lines": 3, "changes": 2, "no_price": 1, "unchanged": 0, "errors": 0, "alerts": 0}
    assert body["summary"] == {**counts, "currency": "PEN"}
    assert lines(world, UUID(body["id"])) == {  # type: ignore[arg-type]  # el lote por su id
        shop.ip16: ("PENDING", Decimal("4399.00"), Decimal("4179.00"), Decimal("-5.00"), ""),
        shop.ip16_pro: ("PENDING", Decimal("5199.90"), Decimal("4940.00"), Decimal("-5.00"), ""),
        shop.ip15: ("SKIPPED", None, None, None, batches.NO_PRICE),
    }
    assert logged(migrator, "product_prices", "*") == prices_before
    again = client.get(f"{BASE}{body['id']}/")
    assert (again.status_code, again.json()) == (200, body)


@pytest.mark.parametrize("missing", sorted(GRANTS))
def test_each_permission_is_needed_before_reading_anything(
    world: Any, shop: Any, missing: str, migrator: psycopg.Connection[Any]
) -> None:
    """Sin uno de los tres, 403 en las dos rutas: también para un lote que no existe (el permiso
    se mira antes de buscarlo) y para un cuerpo que no vale (antes de validarlo)."""
    made = test_price_batches.apple(world, shop)
    client = editor(world, {k: v for k, v in GRANTS.items() if k != missing})
    before = migrator.execute(WRITES).fetchone()
    for answer in (
        post(client, wanted(shop)),
        post(client, {"value": " 5 "}),
        client.get(f"{BASE}{made.pk}/"),
        client.get(f"{BASE}{uuid4()}/"),
    ):
        assert (answer.status_code, answer.content) == DENIED
    assert migrator.execute(WRITES).fetchone() == before


def test_another_organization_or_a_hidden_list_is_not_found(world: Any, shop: Any) -> None:
    wholesale = price_list(world.a, "MAYORISTA", required_permission="prices.view_wholesale")
    manager = person(world, {"prices.manage": None, "prices.bulk_update": None})
    hidden = create(world, manager, wholesale, {"brand_id": shop.apple})
    (_, foreign), foreign_list = stock(world.b), price_list(world.b)
    with tenant_scope(ctx(world.b, person(world, EDITOR, world.b))) as tenant:
        theirs = batches.create_batch(
            tenant,
            price_list_id=foreign_list,
            operation="PERCENT_DECREASE",
            value="5",
            filters={"variant_ids": [foreign]},
            reason="Campaña",
        )
    client = editor(world)
    for batch in (hidden.pk, theirs.pk, uuid4()):
        answer = client.get(f"{BASE}{batch}/")
        assert (answer.status_code, answer.content) == NOT_FOUND
    answer = post(client, wanted(shop, price_list_id=str(wholesale)))
    assert said(answer.json()["fields"]) == {"price_list_id": ["No existe."]}
    sees_all = editor(world, {**GRANTS, "prices.manage": None})
    assert sees_all.get(f"{BASE}{hidden.pk}/").status_code == 200


@pytest.mark.parametrize(
    ("fields", "errors"),
    [
        ({"extra": 1}, {"extra": ["Campo desconocido."]}),
        ({"operation": "PERCENT_OFF"}, "operation"),
        ({"rounding": "0.90"}, "rounding"),
        ({"value": " 5 "}, "value"),
        ({"value": "5 "}, "value"),
        ({"value": 5}, "value"),
        ({"value": "0"}, "value"),
        ({"value": "1.234"}, "value"),
        ({"value": "100.01"}, {"value": ["Mayor que cero y, en un porcentaje, hasta 100."]}),
        ({"reason": "no"}, {"reason": ["De 3 a 500 caracteres, sin HTML."]}),
        ({"reason": 12345}, "reason"),
        ({"effective_at": "2020-01-01T00:00:00Z"}, "effective_at"),
        ({"effective_at": "2999-01-01T00:00:00"}, "effective_at"),
        ({"price_list_id": "REGULAR"}, "price_list_id"),
        ({"price_list_id": str(uuid4())}, {"price_list_id": ["No existe."]}),
        ({"filters": {}}, "filters"),
        ({"filters": {"sku": "IP16"}}, {"filters": {"sku": ["Campo desconocido."]}}),
        ({"filters": {"category_id": "iphone"}}, ("filters", "category_id")),
        ({"filters": {"brand_id": 7}}, ("filters", "brand_id")),
        ({"filters": {"product_id": "x" * 36}}, ("filters", "product_id")),
        ({"filters": {"category_id": str(uuid4())}}, {"filters": {"category_id": ["No existe."]}}),
        ({"filters": {"product_id": str(uuid4())}}, {"filters": {"product_id": ["No existe."]}}),
        ({"filters": {"variant_ids": []}}, ("filters", "variant_ids")),
        ({"filters": {"variant_ids": "IP16"}}, ("filters", "variant_ids")),
        ({"filters": {"variant_ids": ["IP16"]}}, ("filters", "variant_ids")),
        ({"filters": {"variant_ids": [str(uuid4())]}}, ("filters", "variant_ids")),
    ],
)
def test_each_400_is_on_its_field_never_a_500_and_writes_nothing(
    world: Any, shop: Any, fields: dict[str, Any], errors: Any, migrator: psycopg.Connection[Any]
) -> None:
    """Un id de filtro que no es un UUID es 400 en su campo dentro de `filters`, no el
    `ValidationError` de Django que daría un 500; un `value` con espacios también es 400."""
    before = migrator.execute(WRITES).fetchone()
    answer = post(editor(world), wanted(shop, **fields))
    assert answer.status_code == 400, answer.content
    got = answer.json()
    assert got["code"] == "VALIDATION_ERROR"
    if isinstance(errors, str):
        assert set(got["fields"]) == {errors}
    elif isinstance(errors, tuple):
        assert set(got["fields"]) == {"filters"} and set(got["fields"]["filters"]) == {errors[1]}
    else:
        assert said(got["fields"]) == errors
    assert migrator.execute(WRITES).fetchone() == before


def test_more_variants_than_the_cap_is_a_400_on_filters_with_the_count(
    world: Any, shop: Any, monkeypatch: pytest.MonkeyPatch, migrator: psycopg.Connection[Any]
) -> None:
    monkeypatch.setattr(batches, "LINES_MAX", 3)
    before, client = migrator.execute(WRITES).fetchone(), editor(world)
    answer = post(client, wanted(shop, filters={"brand_id": str(shop.apple)}))
    assert answer.status_code == 400, answer.content
    message = "La selección tiene 5 variantes y el tope es 3: acota los filtros."
    assert said(answer.json()["fields"]) == {"filters": [message]}
    assert migrator.execute(WRITES).fetchone() == before
    assert post(client, wanted(shop)).json()["summary"]["lines"] == 3  # justo el tope


def test_a_person_without_her_user_gets_the_403_even_with_a_bad_body(
    world: Any, shop: Any, monkeypatch: pytest.MonkeyPatch, migrator: psycopg.Connection[Any]
) -> None:
    """`ActorRefused` (`actor_id` no es `user_id`) pasa el permiso y llega a la ruta: 403
    `ACTOR_NOT_ALLOWED` antes de validar el cuerpo, nunca un 400 sobre un campo."""
    client = editor(world)
    monkeypatch.setattr(
        "core.tenancy.middleware.resolve_tenant",
        lambda *a: replace(resolve_tenant(*a), actor_id=uuid4()),
    )
    before = migrator.execute(WRITES).fetchone()
    for body in (wanted(shop), wanted(shop, value=" 5 "), {}):
        answer = post(client, body)
        assert answer.status_code == 403, answer.content
        assert answer.json() == {"code": "ACTOR_NOT_ALLOWED", "message": REFUSED}
    assert migrator.execute(WRITES).fetchone() == before


@pytest.mark.parametrize("actor", [ActorType.AI_AGENT, ActorType.INTEGRATION])
def test_a_robot_over_http_is_turned_away_before_the_route(
    world: Any, shop: Any, monkeypatch: pytest.MonkeyPatch, actor: ActorType
) -> None:
    """Por HTTP la primera barrera es el motor: a quien no es una persona lo trata como a un no
    miembro (el 404 de siempre, F3-39), nunca un 400 ni un 500."""
    client = editor(world)
    monkeypatch.setattr(
        "core.tenancy.middleware.resolve_tenant",
        lambda *a: replace(resolve_tenant(*a), actor_type=actor, actor_id=uuid4()),
    )
    for answer in (post(client, wanted(shop)), post(client, {"value": " 5 "})):
        assert (answer.status_code, answer.content) == NOT_FOUND
