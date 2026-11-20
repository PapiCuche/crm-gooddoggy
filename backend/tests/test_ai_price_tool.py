"""F8-07c (#998; ADR-034 §6, §13 D-F8-06): `get_price` cita solo precios de las listas que el
propietario marcó (`ai_exposable`) y que la persona ve, por `PricingService` con `PUBLIC_AI`: como
mucho 3, con su nombre y la predeterminada primero; nunca costos, listas internas ni el SKU.
Primero lo que se rechaza. PostgreSQL con el rol `crm_app`."""

from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from django.utils import timezone

from apps.ai_agents.registry import TOOLS, invoke
from apps.ai_agents.tools import price
from apps.ai_gateway.llm import ToolCall
from apps.catalog.models import ProductVariant
from apps.pricing import services
from apps.pricing.models import INTERNAL, WHOLESALE, PriceList, ProductPrice
from core.tenancy.scope import tenant_scope
from tests.ai_queries import captured, writes
from tests.conftest import migrator_conninfo
from tests.logs import log_text
from tests.test_ai_read_tools import COST, allow, run, shop, world
from tests.test_authorization import acting
from tests.test_memberships import ctx
from tests.test_prices import seeding
from tests.test_pricing_engine import MARK, row
from tests.test_promotion_tables import promo

pytestmark = pytest.mark.usefixtures("tenant_db")
__all__ = ["shop", "world"]  # los fixtures, para pytest
OFF = "UPDATE price_lists SET ai_exposable = false"
FORBIDDEN = {"ok": False, "error": {"code": "FORBIDDEN"}}
NOT_FOUND = '{"error":{"code":"NOT_FOUND"},"ok":false}'
# código: (nombre, importe, campos); todas con precio para el collar grande
LISTS: dict[str, tuple[str, str, dict[str, Any]]] = {
    "REG": ("Precio Publico", "59.90", {"is_default": True, "sort_order": 5}),
    "OFE": ("Oferta Web", "55.00", {"sort_order": 1}),
    "TIE": ("Tienda", "57.50", {"sort_order": 2}),
    "EXT": ("Extra", "52.00", {"sort_order": 3}),  # la cuarta: fuera del tope
    "MAY": ("Mayorista", "40.00", {"sort_order": 0, "required_permission": WHOLESALE}),
    # las internas no se pueden marcar (F8-07f, el CHECK de la base): `store` lo comprueba
    "STF": ("Personal", "30.00", {"sort_order": -3, "kind": "INTERNAL", "ai_exposable": False}),
    "INT": ("Interna", "31.00", {"sort_order": -2, "required_permission": INTERNAL,
                                 "ai_exposable": False}),
    "OFF": ("Cerrada", "33.00", {"sort_order": -1, "is_active": False}),
    "DEL": ("Borrada", "34.00", {"sort_order": -1, "deleted_at": timezone.now()}),
    "NOM": ("Sin Marcar", "35.00", {"sort_order": -4, "ai_exposable": False}),
}  # fmt: skip


def as_owner(sql: str, params: list[Any]) -> None:
    """Marca o desmarca como el propietario: `crm_app` sin `ai.manage` no puede (F8-07f)."""
    with psycopg.connect(migrator_conninfo(), autocommit=True) as conn:
        conn.execute(sql, params)


def listed(org: UUID, code: str, name: str, **fields: Any) -> UUID:
    marked = fields.pop("ai_exposable", True)
    values = {"name": name, "kind": "RETAIL", "currency": "PEN",
              "required_permission": "prices.view", **fields}  # fmt: skip
    with tenant_scope(ctx(org)):
        made = UUID(str(PriceList.objects.create(code=code, **values).pk))
    if marked:
        as_owner(MARK, [[made]])
    return made


@pytest.fixture
def store(shop: Any) -> SimpleNamespace:
    """En A: las listas de `LISTS`, una promoción para la IA en REG y otra solo humana en OFE. En
    B: una lista marcada con precio para el collar ajeno."""
    made, past = SimpleNamespace(shop=shop, world=shop.world), timezone.now() - timedelta(days=1)
    a, big = shop.world.a, shop.big.pk
    made.lists = {
        code: listed(a, code, name, **fields) for code, (name, _, fields) in LISTS.items()
    }
    made.prices = {code: row(a, (big, made.lists[code]), LISTS[code][1], past) for code in LISTS}
    for internal in ("STF", "INT"):
        with pytest.raises(psycopg.errors.CheckViolation, match="price_lists_internal_not_ai_ck"):
            as_owner(MARK, [[made.lists[internal]]])
    week = timezone.now() + timedelta(days=7)
    target = [{"variant_id": big}]
    made.promo = promo(a, [made.lists["REG"]], target, "AMOUNT_OFF", "10", past, week,
                       name="Promo Verano", terms="Solo esta semana",
                       ai_exposable=True)  # fmt: skip
    promo(a, [made.lists["OFE"]], target, "AMOUNT_OFF", "20", past, week, name="Promo Secreta",
          terms="Solo humanos")  # fmt: skip
    with tenant_scope(ctx(shop.world.b)):
        made.foreign = shop.foreign.variants.get()
    made.ajena = listed(shop.world.b, "AJE", "Lista Ajena", is_default=True)
    row(shop.world.b, (made.foreign.pk, made.ajena), "99.00", past)
    return made


def ask(store: Any, variant: Any) -> tuple[Any, dict[str, Any]]:
    return run(store.world, "get_price", {"variant_id": str(variant.pk)})


def names(store: Any) -> list[tuple[str, str]]:
    body = ask(store, store.shop.big)[1]
    return [(p["price_list"], p["amount"]) for p in body["data"]["prices"]]


@pytest.mark.parametrize("granted", [(), ("products.view",), ("products.view", "inventory.view")])
def test_get_price_needs_prices_view_of_the_person(store: Any, granted: tuple[str, ...]) -> None:
    allow(store.world, *granted)
    done, body = ask(store, store.shop.big)
    assert body == FORBIDDEN and done.evidence is None


def test_a_malformed_variant_id_or_an_extra_argument_is_rejected(store: Any) -> None:
    allow(store.world, "prices.view")
    big = str(store.shop.big.pk)
    for data in ({"variant_id": "no-es-un-uuid"}, {"variant_id": 7},
                 {"variant_id": big, "price_list_id": str(store.lists["MAY"])},
                 {"variant_id": big, "organization_id": str(store.world.b)}):  # fmt: skip
        done, _ = run(store.world, "get_price", data)
        assert done.text == '{"error":{"code":"INVALID_INPUT"},"ok":false}' and not done.evidence


def test_unpublished_foreign_and_missing_variants_are_the_same_not_found(store: Any) -> None:
    allow(store.world, "prices.view", "prices.manage")
    shop = store.shop
    with tenant_scope(ctx(store.world.a)):
        hidden = list(ProductVariant.objects.filter(label__in=["Mini", "Secreta", "Vieja"]))
        # la variante está bien, el producto no: borrador, oculto, terminado, borrado, sin variante
        hidden += [ProductVariant.objects.get(product=p) for p in (
            shop.draft, shop.hidden, shop.ended, shop.gone, shop.bare)]  # fmt: skip
    for variant in hidden:  # con precio: no basta
        row(store.world.a, (variant.pk, store.lists["REG"]), "11.00", timezone.now())
    texts = {ask(store, v)[0].text for v in (*hidden, store.foreign)}
    texts.add(run(store.world, "get_price", {"variant_id": str(uuid4())})[0].text)
    assert texts == {NOT_FOUND} and len(hidden) == 8


def test_up_to_three_marked_lists_with_their_name_and_the_default_first(store: Any) -> None:
    allow(store.world, "prices.view")
    assert names(store) == [("Precio Publico", "49.90"), ("Oferta Web", "55.00"),
                            ("Tienda", "57.50")]  # fmt: skip


def test_with_no_list_marked_there_are_no_prices(store: Any) -> None:
    allow(store.world, "prices.view", "prices.manage")
    as_owner(f"{OFF} WHERE organization_id = %s", [store.world.a])
    done, body = ask(store, store.shop.big)
    assert body["ok"] is True and body["data"] == {"variant_id": str(store.shop.big.pk),
                                                   "prices": []}  # fmt: skip
    assert done.evidence is not None and done.evidence.subjects == (store.shop.red.pk,
                                                                    store.shop.big.pk)  # fmt: skip


def test_only_marked_lists_the_person_sees_and_never_the_internal_one(store: Any) -> None:
    allow(store.world, "prices.view")
    without = names(store)
    allow(store.world, "prices.view_wholesale")
    assert names(store) == [("Precio Publico", "49.90"), ("Mayorista", "40.00"),
                            ("Oferta Web", "55.00")]  # fmt: skip
    # lo que no ve es igual que lo no marcado
    as_owner(f"{OFF} WHERE organization_id = %s AND code = 'MAY'", [store.world.a])
    assert names(store) == without
    allow(store.world, "prices.manage", "prices.view_internal")  # lo ve todo
    kept = ["STF", "INT", "OFF", "DEL", "NOM"]
    as_owner(f"{OFF} WHERE organization_id = %s AND code <> ALL(%s)", [store.world.a, kept])
    assert names(store) == []  # ni interna, ni inactiva, ni borrada, ni sin marcar


def test_a_list_without_a_price_for_the_variant_is_left_out(store: Any) -> None:
    allow(store.world, "prices.view")
    listed(store.world.a, "VAC", "Vacia", sort_order=0)  # entra en las 3, sin precio
    assert names(store) == [("Precio Publico", "49.90"), ("Oferta Web", "55.00")]


def test_a_price_without_tax_says_so(store: Any) -> None:
    allow(store.world, "prices.view")
    net = listed(store.world.a, "NET", "Sin IGV", sort_order=0)
    with seeding(store.world.a):
        ProductPrice.objects.create(variant_id=store.shop.big.pk, price_list_id=net,
                                    amount=Decimal("50.85"), currency="PEN", tax_included=False,
                                    starts_at=timezone.now() - timedelta(days=1),
                                    change_reason="Semilla del test")  # fmt: skip
    rows = ask(store, store.shop.big)[1]["data"]["prices"]
    assert [(p["price_list"], p["amount"], p["tax_included"]) for p in rows] == [
        ("Precio Publico", "49.90", True), ("Sin IGV", "50.85", False),
        ("Oferta Web", "55.00", True)]  # fmt: skip


def test_the_engine_refuses_a_list_that_was_not_marked_even_if_selected(
    store: Any, monkeypatch: Any
) -> None:
    allow(store.world, "prices.view")
    picked = [(store.lists["NOM"], "Sin Marcar"), (store.lists["TIE"], "Tienda")]
    monkeypatch.setattr(services, "ai_price_lists", lambda ectx, limit: picked)
    assert names(store) == [("Tienda", "57.50")]  # `PUBLIC_AI` la rechaza: igual que sin precio


def test_the_ai_promotion_applies_and_a_human_only_one_does_not(store: Any) -> None:
    allow(store.world, "prices.view")
    done, body = ask(store, store.shop.big)
    first, second = body["data"]["prices"][:2]
    assert first == {"price_list": "Precio Publico", "currency": "PEN", "amount": "49.90",
                     "regular_amount": "59.90", "tax_included": True, "promotion": "Promo Verano",
                     "promotion_terms": "Solo esta semana", "valid_until": first["valid_until"],
                     "price_id": str(store.prices["REG"])}  # fmt: skip
    assert first["valid_until"] is not None
    assert (second["amount"], second["promotion"], second["promotion_terms"]) == ("55.00",) + (
        None, None)  # fmt: skip
    assert "Secreta" not in done.text and "Solo humanos" not in done.text


def test_evidence_has_its_id_moment_and_what_backs_each_price(store: Any) -> None:
    allow(store.world, "prices.view")
    done, body = ask(store, store.shop.big)
    evidence = done.evidence
    assert evidence is not None and (evidence.kind, evidence.tool) == ("PRICE", "get_price")
    assert body["meta"] == {"evidence_id": str(evidence.evidence_id), "kind": "PRICE",
                            "as_of": evidence.as_of.isoformat()}  # fmt: skip
    p = store.prices
    assert evidence.subjects == (store.shop.red.pk, store.shop.big.pk, p["REG"], store.promo,
                                 p["OFE"], p["TIE"])  # fmt: skip
    assert dict(evidence.facts) == {}


def test_the_organization_comes_from_the_session_never_from_another_tenant(store: Any) -> None:
    allow(store.world, "prices.view", "prices.manage")
    done, _ = ask(store, store.foreign)
    assert done.text == NOT_FOUND
    done, _ = ask(store, store.shop.big)
    assert "Lista Ajena" not in done.text and "99.00" not in done.text
    with acting(store.world.a, store.world.ana) as ectx:
        lists = services.ai_price_lists(ectx, 10)
    assert [name for _, name in lists] == ["Precio Publico", "Mayorista", "Oferta Web", "Tienda",
                                           "Extra"]  # fmt: skip


def test_no_output_carries_costs_internal_lists_codes_or_the_sku(store: Any) -> None:
    allow(store.world, "prices.view", "prices.manage", "prices.view_wholesale",
          "prices.view_internal")  # fmt: skip
    done, body = ask(store, store.shop.big)
    assert set(body) == {"ok", "data", "meta"} and set(body["data"]) == {"variant_id", "prices"}
    assert all(set(p) == set(price.PRICE) for p in body["data"]["prices"])
    for leak in (COST, "COL-Grande", "Collar-Rojo", "Personal", "Interna", "Sin Marcar", "30.00",
                 "31.00", "35.00", '"REG"', "MAY", "cost", "margin", "sku", "internal",
                 *(str(pk) for pk in store.lists.values())):  # fmt: skip
        assert leak not in done.text, leak


def test_get_price_is_registered_read_public_and_needs_prices_view() -> None:
    spec = TOOLS["get_price"]
    assert (spec.permission, spec.kind, list(spec.fields)) == ("prices.view", "PRICE",
                                                               ["variant_id"])  # fmt: skip
    assert "no lo inventes" in spec.description and price.MAX_LISTS == 3


@pytest.mark.parametrize("granted", [True, False])
def test_get_price_writes_or_locks_nothing_and_logs_no_price(
    store: Any, granted: bool, caplog: Any
) -> None:
    if granted:
        allow(store.world, "prices.view")
    with caplog.at_level("DEBUG"), acting(store.world.a, store.world.ana) as ectx:
        with captured() as seen:
            for variant in (store.shop.big, store.foreign):
                invoke(ectx, ToolCall("toolu_1", "get_price", {"variant_id": str(variant.pk)}))
    assert writes(seen) == [] and bool(seen) is granted  # sin permiso no se lee nada
    text = log_text(caplog)
    assert "get_price" in text and ("OK" if granted else "FORBIDDEN") in text
    for secret in ("49.90", "59.90", "Precio Publico", "Promo Verano", str(store.shop.big.pk)):
        assert secret not in text
