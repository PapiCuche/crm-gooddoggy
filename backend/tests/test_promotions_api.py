"""F3-48d: las rutas de crear, listar y leer promociones (`…/pricing/promotions/`). Middleware,
sesión, motor de autorización y PostgreSQL con el rol `crm_app`. Los comandos se prueban en
`test_promotions`; publicar y cancelar por HTTP, en F3-48e."""

import json
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from django.test import Client
from django.utils import timezone

from apps.accounts.models import User
from apps.pricing.models import PriceList, Promotion, PromotionItem, PromotionPriceList
from core.tenancy.scope import tenant_scope
from tests import test_price_lists_api, test_self_context
from tests.factories import make_user
from tests.test_authorization import give
from tests.test_member_branch import DENIED
from tests.test_memberships import ctx, join
from tests.test_prices import price_list
from tests.test_prices_api import no_cost
from tests.test_pricing_engine import T0, row
from tests.test_products import send
from tests.test_promotion_tables import D1, D8, NOW, brand, final, promo, stock, tree
from tests.test_self_context import NOT_FOUND, reply, signed

shop, real_stack = test_price_lists_api.shop, test_self_context.real_stack
pytestmark = pytest.mark.usefixtures("tenant_db")
PROMOS = "/api/v1/o/org-a/pricing/promotions/"
FIELDS = "id name description terms discount_type value currency starts_at ends_at status priority"
FIELDS += " ai_exposable channels version created_at created_by_user_id items price_list_ids"
WHOLESALE = "prices.view_wholesale"


@pytest.fixture
def team(shop: SimpleNamespace, orgs: dict[str, UUID]) -> SimpleNamespace:
    """Org A con Owner `a`, Vendedora `v` (solo `prices.view`), una lista pública y una mayorista,
    y una variante en una categoría."""
    org = orgs["A"]
    category = tree(org, "celulares")[0]
    product, variant = stock(org, category, brand(org))
    return SimpleNamespace(
        **vars(shop),
        org=org,
        regular=price_list(org, "REGULAR", is_default=True),
        wholesale=price_list(org, "MAYO", required_permission=WHOLESALE),
        category=category,
        variant=variant,
    )


def body(team: Any, **fields: Any) -> dict[str, Any]:
    return {
        "name": " Campaña  de octubre ",
        "discount_type": "PERCENT_OFF",
        "value": "10",
        "starts_at": D1.isoformat(),
        "ends_at": D8.isoformat(),
        "price_list_ids": [str(team.regular)],
        "items": [{"category_id": str(team.category)}],
        **fields,
    }


def create(client: Client, team: Any, **fields: Any) -> Any:
    return send(client, "post", PROMOS, body(team, **fields))


def ids(client: Client, **query: Any) -> list[str]:
    response = client.get(PROMOS, query)
    assert response.status_code == 200, response.content
    return [row["id"] for row in response.json()["results"]]


def stored(team: Any) -> int:
    with tenant_scope(ctx(team.org)):
        return Promotion.objects.count()


def test_create_then_read_it_back_with_its_audit_and_without_costs(
    team: Any, migrator: psycopg.Connection[Any]
) -> None:
    made = create(team.a, team, terms="Hasta agotar stock", channels=["WEB"], priority=3)
    assert made.status_code == 201, made.content
    new = made.json()
    no_cost(new)
    assert set(new) == set(FIELDS.split())
    assert [new[k] for k in ("name", "status", "value", "version")] == [
        "Campaña de octubre",
        "DRAFT",
        "10.00",
        1,
    ]
    assert [i["category_id"] for i in new["items"]] == [str(team.category)]
    assert new["price_list_ids"] == [str(team.regular)] and new["channels"] == ["WEB"]
    assert team.a.get(f"{PROMOS}{new['id']}/").json() == new  # el mismo cuerpo al leerla
    assert team.a.get(PROMOS).json()["results"] == [new]  # la lista trae lo mismo
    query = "SELECT action, entity_id, actor_id FROM audit_logs WHERE entity_type = 'promotion'"
    audit = ("promotion.created", UUID(new["id"]), UUID(new["created_by_user_id"]))
    assert migrator.execute(query).fetchall() == [audit]


def test_roles_and_the_anonymous(team: Any) -> None:
    made = create(team.a, team).json()["id"]
    one = f"{PROMOS}{made}/"
    assert reply(create(team.v, team)) == DENIED  # solo `prices.view`: no crea, ni ve borradores
    assert reply(team.v.get(one)) == NOT_FOUND and ids(team.v) == []
    assert ids(team.x) == [made]  # el Administrador tiene `prices.manage`
    blind = make_user(email="ciega@example.com")  # sin `prices.view` ninguna ruta se abre
    give(team.org, join(team.org, blind).pk, {"products.view": None})
    assert [reply(signed(blind).get(path)) for path in (PROMOS, one)] == [DENIED, DENIED]
    half = make_user(
        email="media@example.com"
    )  # `prices.view` sin `products.view`: revela objetivos
    give(team.org, join(team.org, half).pk, {"prices.view": None})
    assert [reply(signed(half).get(path)) for path in (PROMOS, one)] == [DENIED, DENIED]
    assert reply(create(signed(blind), team)) == DENIED
    nobody = Client()
    codes = [nobody.get(PROMOS), nobody.get(one), create(nobody, team)]
    assert [response.status_code for response in codes] == [401, 401, 401]
    assert stored(team) == 1


def test_a_viewer_sees_only_published_promotions_whose_lists_are_all_visible(
    team: Any, migrator: psycopg.Connection[Any]
) -> None:
    target = [{"category_id": team.category}]
    both = promo(team.org, [team.regular, team.wholesale], target)
    hidden = promo(team.org, [team.wholesale], target)
    draft = promo(team.org, [team.regular], target, status="DRAFT")
    cancelled = promo(team.org, [team.regular], target, status="CANCELLED")
    shown = promo(team.org, [team.regular], target)
    gone_list = price_list(team.org, "BORRADA")
    with tenant_scope(ctx(team.org)):
        PriceList.objects.filter(pk=gone_list).update(deleted_at=timezone.now())
    gone = promo(team.org, [team.regular, gone_list], target)
    off_list = price_list(team.org, "APAGADA")
    off = promo(team.org, [team.regular, off_list], target)
    with tenant_scope(ctx(team.org)):
        PriceList.objects.filter(pk=off_list).update(is_active=False)
    mine = ids(team.v)
    assert mine == [str(shown)]
    for hide in (both, hidden, draft, cancelled, gone, off):
        assert reply(team.v.get(f"{PROMOS}{hide}/")) == NOT_FOUND
    assert team.v.get(f"{PROMOS}{shown}/").status_code == 200
    assert len(ids(team.a)) == 7 == len(ids(team.x))  # `prices.manage`: todas, en cualquier estado
    assert team.a.get(f"{PROMOS}{draft}/").status_code == 200
    migrator.execute("UPDATE promotions SET deleted_at = now() WHERE id = %s", [shown])
    assert ids(team.v) == [] and reply(team.a.get(f"{PROMOS}{shown}/")) == NOT_FOUND


def test_another_organization_is_not_found_and_cannot_be_targeted(
    team: Any, orgs: dict[str, UUID]
) -> None:
    their_list, their_variant = price_list(orgs["B"], "REGULAR"), stock(orgs["B"])[1]
    theirs = promo(orgs["B"], [their_list], [{"variant_id": their_variant}])
    assert ids(team.a) == [] and reply(team.a.get(f"{PROMOS}{theirs}/")) == NOT_FOUND
    assert reply(team.a.get(PROMOS.replace("org-a", "org-b"))) == NOT_FOUND  # sin membresía
    for fields in (
        {"price_list_ids": [str(their_list)]},
        {"items": [{"variant_id": their_variant}]},
    ):
        refused = create(team.a, team, **fields)
        assert (refused.status_code, set(refused.json()["fields"])) == (400, set(fields))
    assert create(team.a, team, organization_id=str(orgs["B"])).status_code == 400  # no se admite
    ours = create(team.a, team).json()
    for org, mine in (("A", ours["id"]), ("B", str(theirs))):
        with tenant_scope(ctx(orgs[org])):
            assert [str(pk) for pk in Promotion.objects.values_list("pk", flat=True)] == [mine]


def bad_cases(team: Any) -> list[tuple[str, dict[str, Any]]]:
    ok, one = str(team.regular), {"category_id": str(team.category)}
    cases: dict[str, list[Any]] = {
        "priority": ["5", True, 5.5, 5.0, None, 10_001, [5]],
        "ai_exposable": ["true", 1, 0, None],
        "name": [5, True, None, "  ", "a" * 201, {}],
        "description": [5, None, "<script>", "a\x07b", "a\u202eb", "a\u2028b", "a\x00b"],
        "terms": [5, None, "a\x1bb", "a\u200fb", "a\u2029b", "a\U000e0041b", "a\ufeffb"],
        "overide_value": [5],
        "organization_id": [str(uuid4())],
        "currency": [5, "pen1", None],
        "discount_type": ["X", 5, None],
        "value": [10, 10.5, "abc", "0", "101", None, True, " 10 "],
        "starts_at": [5, "ayer", "2026-04-01T00:00:00", None],
        "ends_at": [5, "2026-04-01T00:00:00", "2026-03-01T00:00:00Z", D1.isoformat()],
        "channels": ["WEB", [5], ["WEB", "WEB"], [], ["a"] * 11, {"a": 1}],
        "price_list_ids": [
            ok,
            5,
            [5],
            ["no"],
            [],
            [str(uuid4())],
            [ok, ok],
            [ok] * 21,
            [uuid4().int],
        ],
        "items": ["x", {}, [], [5], [{}], [{"variant_id": 5}], [{"category_id": "no"}]],
    }
    cases["items"] += [[one | {"override_value": 5}], [one | {"override_value": "0"}]]
    cases["items"] += [[one | {"variant_id": str(team.variant)}], [one, one], [one] * 101]
    cases["items"] += [[{"variant_id": str(uuid4())}], [{"category_id": 123456}]]
    cases["items"] += [
        [{"variant_id": uuid4().int}],
        [one | {"overide_value": "5"}],
        [one | {"x": 1}],
    ]
    return [(field, {field: value}) for field, values in cases.items() for value in values]


def test_strict_input_is_a_400_in_its_field_never_a_500_and_writes_nothing(team: Any) -> None:
    for field, bad in bad_cases(team):
        refused = create(team.a, team, **bad)
        assert refused.status_code == 400, (bad, refused.content)
        answer = refused.json()
        assert answer["code"] == "VALIDATION_ERROR" and set(answer["fields"]) == {field}, bad
    for field in ("name", "discount_type", "value", "starts_at", "ends_at"):  # obligatorios
        sent = {k: v for k, v in body(team).items() if k != field}
        assert set(send(team.a, "post", PROMOS, sent).json()["fields"]) == {field}
    for raw in ("5", "null", '"x"', "[]", "{", ""):  # el cuerpo no es un objeto
        assert send(team.a, "post", PROMOS, raw).status_code == 400, raw
    for token in ("NaN", "Infinity"):  # JSON no estándar
        text = json.dumps(body(team)).replace('"value": "10"', f'"priority": {token}')
        assert send(team.a, "post", PROMOS, text).status_code == 400, token
    assert stored(team) == 0  # nada de lo anterior escribió
    extremes = {"starts_at": "0001-01-01T00:00:00+14:00", "ends_at": "9999-12-31T23:59:59-14:00"}
    assert create(team.a, team, **extremes).status_code in (201, 400)  # nunca un 500


def test_the_list_is_paged_by_creation_with_a_cursor(team: Any) -> None:
    made = [create(team.a, team, name=f"Promo {i}").json()["id"] for i in range(5)]
    rows, answer = [], {"next": None}
    for _ in range(3):
        answer = team.a.get(
            PROMOS, {"limit": 2} | ({"cursor": c} if (c := answer["next"]) else {})
        ).json()
        rows += answer["results"]
    assert [row["id"] for row in rows] == made and answer["next"] is None  # sin repetir ni saltar
    assert rows == [team.a.get(f"{PROMOS}{one}/").json() for one in made]  # objetivos y listas


def test_a_created_draft_is_not_applied_by_pricing_service(team: Any) -> None:
    viewer = User.objects.get(email="v@example.com")
    row(team.org, (team.variant, team.regular), "4399", T0 - timedelta(days=90))
    made = create(team.a, team, items=[{"variant_id": str(team.variant)}])
    assert made.status_code == 201, made.content
    assert final(team.org, viewer, team.variant, team.regular, NOW).promotion_id is None
    with tenant_scope(ctx(team.org)):
        assert Promotion.objects.get(pk=made.json()["id"]).status == "DRAFT"
    twin = promo(team.org, [team.regular], [{"variant_id": team.variant}])  # el control
    assert str(final(team.org, viewer, team.variant, team.regular, NOW).promotion_id) == str(twin)


def test_an_unknown_key_is_named_in_the_400_at_the_top_and_inside_an_item(team: Any) -> None:
    top = create(team.a, team, overide_value="5").json()
    assert top["code"] == "VALIDATION_ERROR" and list(top["fields"]) == ["overide_value"]
    item = [{"category_id": str(team.category), "overide_value": "5"}]
    nested = create(team.a, team, items=item).json()["fields"]["items"]
    assert list(nested) == ["0"] and list(nested["0"]) == ["overide_value"]
    assert stored(team) == 0


def test_items_and_lists_come_back_by_id_whatever_their_insertion_order(team: Any) -> None:
    cats = tree(team.org, "audio", "bocinas", "portatiles")
    lists = [team.regular, price_list(team.org, "B"), price_list(team.org, "C")]
    keys = sorted(uuid4() for _ in range(3))  # se insertan de mayor a menor
    with tenant_scope(ctx(team.org)):
        made = Promotion.objects.create(
            name="P",
            currency="PEN",
            discount_type="PERCENT_OFF",
            value=Decimal(5),
            starts_at=D1,
            ends_at=D8,
        )
        for key, cat, one in reversed(list(zip(keys, cats, lists, strict=True))):
            PromotionItem.objects.create(id=key, promotion=made, category_id=cat)
            PromotionPriceList.objects.create(id=key, promotion=made, price_list_id=one)
        Promotion.objects.filter(pk=made.pk).update(status="PUBLISHED")
    expected = dict(zip(keys, zip(cats, lists, strict=True), strict=True))
    for shown in (
        team.a.get(f"{PROMOS}{made.pk}/").json(),
        team.a.get(PROMOS).json()["results"][0],
    ):
        assert [i["category_id"] for i in shown["items"]] == [str(expected[k][0]) for k in keys]
        assert shown["price_list_ids"] == [str(expected[k][1]) for k in keys]


def test_a_null_channel_list_is_every_channel(team: Any) -> None:
    made = create(team.a, team, channels=None)
    assert made.status_code == 201 and made.json()["channels"] is None
    assert create(team.a, team).json()["channels"] is None  # sin enviarlo, igual


def test_a_text_keeps_its_line_breaks_and_tabs(team: Any) -> None:
    made = create(team.a, team, terms="Uno\n\tDos", description="Ñandú\ttab")
    assert (made.json()["terms"], made.json()["description"]) == ("Uno\n\tDos", "Ñandú\ttab")


def test_a_text_keeps_composed_emoji_and_turns_windows_breaks_into_line_feeds(team: Any) -> None:
    family = "Oferta 👨‍👩‍👧 ¡Ñandú! con­guion‌blando"
    made = create(team.a, team, terms="Uno\r\nDos\rTres", description=family)
    assert made.status_code == 201
    assert (made.json()["terms"], made.json()["description"]) == ("Uno\nDos\nTres", family)
