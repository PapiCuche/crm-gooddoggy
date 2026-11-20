"""F8-08e (#1022; ADR-034 §9, ADR-005 Parte D): el Output Guard I. La respuesta del copiloto es
una plantilla con claims; el servidor valida cada claim contra la evidencia de esta invocación y
copia el valor de la tool, o deja `[verificar]` con un hallazgo que no lleva el texto del modelo.
Puro: las pruebas de la lógica no tienen base de datos (una consulta fallaría); la última corre las
tools reales en PostgreSQL con el rol `crm_app`. Todos los datos son ficticios."""

import dataclasses
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import jsonschema  # type: ignore[import-untyped]  # la de drf-spectacular
import pytest
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings
from django.utils import timezone

from apps.ai_agents import guard
from apps.ai_agents.guard import KINDS, REPLY_SCHEMA, Finding, Malformed, Problem, render
from apps.ai_agents.registry import TOOLS, Evidence, ToolSpec, invoke_read_only
from apps.ai_agents.tools.catalog import GET_PRODUCT_VARIANTS, SEARCH_PRODUCT
from apps.ai_agents.tools.price import GET_PRICE
from apps.ai_agents.tools.stock import GET_STOCK
from apps.ai_gateway.llm import ToolCall
from core.tenancy.scope import tenant_scope
from core.text import VERIFY_MARK
from tests.test_ai_price_tool import store
from tests.test_ai_read_tools import allow, shop, world
from tests.test_ai_stock_tool import house, put
from tests.test_memberships import ctx

__all__ = ["shop", "store", "world"]  # los fixtures, para pytest
NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
PRODUCT, BIG, SMALL, PRICE_ID, PROMO = (str(uuid4()) for _ in range(5))
SENTINEL = 731942  # el número exacto del stock: nunca en el texto


def ev(spec: ToolSpec, data: dict[str, Any], *subjects: str, age: int = 0, **facts: int
       ) -> Evidence:  # fmt: skip
    return Evidence(uuid4(), spec.kind, spec.name, NOW - timedelta(seconds=age),
                    tuple(map(UUID, subjects)), facts, data)  # fmt: skip


def prices(variant: str) -> dict[str, Any]:
    """Con promoción en la primera fila; sin ella en la segunda."""
    rows = [{"price_list": "Precio Publico", "currency": "PEN", "amount": "1249.90",
             "regular_amount": "1299.90", "tax_included": True, "promotion": "Promo Verano",
             "promotion_id": PROMO, "valid_until": None, "price_id": PRICE_ID},
            {"price_list": "Tienda", "currency": "USD", "amount": "57.50",
             "regular_amount": "57.50", "tax_included": True, "promotion": None,
             "promotion_id": None, "valid_until": None, "price_id": str(uuid4())}]  # fmt: skip
    return {"variant_id": variant, "prices": rows,
            "promotions": [{"promotion_id": PROMO, "terms": "Solo esta semana"}]}  # fmt: skip


def stock(variant: str, band: str = "FEW_UNITS", age: int = 0) -> Evidence:
    data = {"variant_id": variant, "availability": band}
    return ev(GET_STOCK, data, PRODUCT, variant, age=age, available=SENTINEL)


VARIANTS = {"product_id": PRODUCT, "name": "Collar Rojo", "variants": [
    {"variant_id": BIG, "name": "Grande", "label": "", "condition": "NEW", "is_default": True},
    {"variant_id": SMALL, "name": "Chico", "label": "", "condition": "NEW", "is_default": False},
]}  # fmt: skip
EVIDENCE: dict[str, Evidence | None] = {
    "tu_v": ev(GET_PRODUCT_VARIANTS, VARIANTS, PRODUCT, BIG, SMALL),
    "tu_q": ev(SEARCH_PRODUCT, {"products": [{"product_id": PRODUCT, "name": "Collar Rojo",
               "brand": None, "category": None, "short_description": ""}]}, PRODUCT),
    "tu_p": ev(GET_PRICE, prices(BIG), PRODUCT, BIG, PRICE_ID, PROMO),
    "tu_p2": ev(GET_PRICE, prices(SMALL), PRODUCT, SMALL),
    "tu_s": stock(BIG),
    "tu_s2": stock(SMALL),
    "tu_old": ev(GET_PRICE, prices(BIG), PRODUCT, BIG, age=660),  # 11 minutos
    "tu_edge": ev(GET_PRICE, prices(BIG), PRODUCT, BIG, age=600),
    "tu_oldcat": ev(GET_PRODUCT_VARIANTS, VARIANTS, PRODUCT, BIG, age=3600),
    "tu_oldstock": stock(BIG, age=601),
    "tu_nt": stock(BIG, "NOT_TRACKED"),
    "tu_fail": None,  # la tool no dio `ok`
    # con la forma de `get_price`, pero de otra tool o de otro tipo de evidencia
    "tu_quote": Evidence(uuid4(), "PRICE", "get_quote", NOW, (), data=prices(BIG)),
    "tu_kind": Evidence(uuid4(), "CATALOG", "get_price", NOW, (), data=prices(BIG)),
}  # fmt: skip


def claim(cid: str, kind: str, ref: str, path: str) -> dict[str, str]:
    return {"id": cid, "kind": kind, "evidence_ref": ref, "path": path}


def reply(template: str, *claims: dict[str, str], handoff: bool = False) -> dict[str, Any]:
    return {"reply_template": template, "claims": list(claims), "wants_handoff": handoff}


def marks(done: guard.Rendered) -> list[str]:
    assert all(done.text[f.start : f.end] == VERIFY_MARK for f in done.findings)
    return [f.type for f in done.findings]


FULL = reply(
    "El {{c1}} cuesta {{c2}} (no S/ 10), antes {{c3}} con {{c4}} ({{c5}}), y está {{c6}}.",
    claim("c1", "PRODUCT_NAME", "tu_v", "variants.0.name"),
    claim("c2", "PRICE", "tu_p", "prices.0.amount"),
    claim("c3", "REGULAR_PRICE", "tu_p", "prices.0.regular_amount"),
    claim("c4", "PROMOTION", "tu_p", "prices.0.promotion"),
    claim("c5", "PROMOTION_TERMS", "tu_p", "promotions.0.terms"),
    claim("c6", "STOCK", "tu_s", "availability"),
)


def test_a_valid_template_is_rendered_with_the_values_copied_from_the_evidence() -> None:
    """El número que el modelo escribió fuera de un placeholder pasa tal cual: lo marca F8-08f."""
    done = render(FULL, EVIDENCE, NOW)
    assert done.text == (
        "El Grande cuesta S/ 1,249.90 (no S/ 10), antes S/ 1,299.90 con Promo Verano (Solo esta "
        "semana), y está disponible, con pocas unidades."
    )
    assert (done.ok, done.findings, done.problems, done.feedback()) == (True, (), (), "")
    assert done.expires_at == NOW + timedelta(seconds=600) and done.wants_handoff is False
    assert render({**FULL, "wants_handoff": True}, EVIDENCE, NOW).wants_handoff is True


def test_other_currencies_go_with_their_code_and_the_value_never_comes_from_the_template() -> None:
    done = render(reply("{{c1}}", claim("c1", "PRICE", "tu_p", "prices.1.amount")), EVIDENCE, NOW)
    assert done.text == "US$ 57.50"
    row = {**prices(BIG)["prices"][0], "currency": "EUR"}
    eur = ev(GET_PRICE, {**prices(BIG), "prices": [row]})
    done = render(reply("{{c1}}", claim("c1", "PRICE", "e", "prices.0.amount")), {"e": eur}, NOW)
    assert done.text == "EUR 1,249.90"


# Una sola claim en «Cuesta {{c1}}.»: la marca va de 7 a 18
@pytest.mark.parametrize(("kind", "ref", "path", "found"), [
    ("PRICE", "tu_otra", "prices.0.amount", "EVIDENCE_MISSING"),  # de otra invocación
    ("PRICE", "tu_fail", "prices.0.amount", "EVIDENCE_MISSING"),  # la tool falló
    ("PRICE", "tu_s", "availability", "KIND_MISMATCH"),  # precio de `get_stock`
    ("PRICE", "tu_quote", "prices.0.amount", "KIND_MISMATCH"),
    ("PRICE", "tu_kind", "prices.0.amount", "KIND_MISMATCH"),
    ("STOCK", "tu_p", "prices.0.amount", "KIND_MISMATCH"),  # stock de `get_price`
    ("PRODUCT_NAME", "tu_p", "prices.0.price_list", "KIND_MISMATCH"),  # nombre de `get_price`
    ("PRICE", "tu_v", "name", "KIND_MISMATCH"),  # precio del catálogo
    ("PRICE", "tu_p", "prices.0.price_list", "KIND_MISMATCH"),
    ("PROMOTION", "tu_p", "prices.0.amount", "KIND_MISMATCH"),
    ("PRICE", "tu_p", "prices.0", "KIND_MISMATCH"),
    ("PRICE", "tu_p", "prices.2.amount", "PATH_MISSING"),
    ("PRICE", "tu_p", "prices.00.amount", "PATH_MISSING"),
    ("PRICE", "tu_p", "prices.0.cost", "PATH_MISSING"),
    ("PRICE", "tu_p", "amount", "PATH_MISSING"),
    ("STOCK", "tu_s", "available", "PATH_MISSING"),  # el número exacto no está en `data`
    ("PRICE", "tu_p", "prices.0.regular_amount", "PRICE_NOT_CURRENT"),
    ("PRICE", "tu_p", "prices.1.regular_amount", "PRICE_NOT_CURRENT"),  # sin promoción
    ("REGULAR_PRICE", "tu_p", "prices.1.regular_amount", "NO_PROMOTION"),
    ("PROMOTION", "tu_p", "prices.1.promotion", "NO_PROMOTION"),
    ("PRICE", "tu_old", "prices.0.amount", "EVIDENCE_STALE"),  # hace 11 minutos
    ("STOCK", "tu_oldstock", "availability", "EVIDENCE_STALE"),  # hace 601 segundos
    ("STOCK", "tu_nt", "availability", "VALUE_MISSING"),  # NOT_TRACKED: nada que afirmar
])  # fmt: skip
def test_a_claim_without_valid_evidence_leaves_the_mark_and_a_finding(
    kind: str, ref: str, path: str, found: str
) -> None:
    done = render(reply("Cuesta {{c1}}.", claim("c1", kind, ref, path)), EVIDENCE, NOW)
    assert (done.text, done.findings) == ("Cuesta [verificar].", (Finding(found, 7, 18),))
    tool = KINDS[kind].tools[0].name
    assert done.problems == (Problem(found, "c1", tool),) and not done.ok
    assert done.feedback() == f"la claim c1 no tiene evidencia válida ({found}); llama a `{tool}`"
    assert done.expires_at is None


def test_freshness_ends_at_the_ttl_and_the_catalog_does_not_expire() -> None:
    edge = claim("c1", "PRICE", "tu_edge", "prices.0.amount")
    done = render(reply("{{c1}} {{c2}}", edge, claim("c2", "PRODUCT_NAME", "tu_oldcat", "name")),
                  EVIDENCE, NOW)  # fmt: skip
    assert (done.text, done.expires_at) == ("S/ 1,249.90 Collar Rojo", NOW)
    with override_settings(AI_EVIDENCE_TTL=60):
        late = render(reply("{{c1}}", claim("c1", "STOCK", "tu_s", "availability")), EVIDENCE,
                      NOW + timedelta(seconds=61))  # fmt: skip
    assert marks(late) == ["EVIDENCE_STALE"]


@pytest.mark.parametrize("value", [0, 601, -1, "600", 600.0, True, None])
def test_the_evidence_ttl_is_a_fixed_integer_of_at_most_ten_minutes(value: Any) -> None:
    assert settings.AI_EVIDENCE_TTL == 600
    with override_settings(AI_EVIDENCE_TTL=value), pytest.raises(ImproperlyConfigured):
        render(FULL, EVIDENCE, NOW)


def test_the_stock_band_is_copied_with_its_fixed_text_never_the_exact_number() -> None:
    for band, text in (("AVAILABLE", "disponible"), ("OUT_OF_STOCK", "agotado")):
        found = {"s": stock(BIG, band)}
        done = render(reply("Está {{c1}}.", claim("c1", "STOCK", "s", "availability")), found, NOW)
        assert done.text == f"Está {text}." and str(SENTINEL) not in done.text


def test_every_placeholder_needs_its_claim_and_every_claim_its_placeholder() -> None:
    done = render(reply(
        "{{c1}} {{c9}} {{ c1 }} {{S/ 99}} {{c2}} {{c1}}",
        claim("c1", "PRICE", "tu_p", "prices.0.amount"),
        claim("c2", "PRICE", "tu_p", "prices.0.amount"), claim("c2", "STOCK", "tu_s", "x"),
        claim("c3", "PRICE", "tu_p", "prices.0.amount"),
        claim("precio S/ 99", "PRICE", "tu_p", "prices.0.amount"),
    ), EVIDENCE, NOW)  # fmt: skip
    assert done.text == "S/ 1,249.90 [verificar] [verificar] [verificar] [verificar] S/ 1,249.90"
    assert marks(done) == ["CLAIM_MISSING"] * 4
    assert [p.text() for p in done.problems] == [
        "la claim c2 está repetida", "una claim tiene un id inválido; usa c1, c2…",
        "el placeholder {{c9}} no tiene claim",
        "un placeholder no tiene claim con un id válido (c1, c2…)",
        "el placeholder {{c2}} no tiene claim", "la claim c3 no tiene placeholder",
    ]  # fmt: skip
    assert "99" not in done.feedback() and "precio" not in done.feedback()


def test_a_finding_carries_its_type_and_the_mark_position_never_the_model_text() -> None:
    assert [f.name for f in dataclasses.fields(Finding)] == ["type", "start", "end"]
    template = "Hoy {{c1}} y {{c2}}, el {{c3}}."
    claims = (claim("c1", "PRICE", "tu_old", "prices.0.amount"),
              claim("c2", "STOCK", "tu_s", "availability"),
              claim("c3", "PRICE", "tu_p", "prices.9.amount"))  # fmt: skip
    done = render(reply(template, *claims), EVIDENCE, NOW)
    assert done.text == "Hoy [verificar] y disponible, con pocas unidades, el [verificar]."
    assert done.findings == (Finding("EVIDENCE_STALE", 4, 15), Finding("PATH_MISSING", 53, 64))
    assert marks(done) == ["EVIDENCE_STALE", "PATH_MISSING"]


def test_claims_after_a_product_name_talk_about_that_product_and_one_variant() -> None:
    def kinds(template: str, *claims: dict[str, str]) -> list[str]:
        return marks(render(reply(template, *claims), EVIDENCE, NOW))

    big, small = claim("c1", "PRODUCT_NAME", "tu_v", "variants.0.name"), claim(
        "c2", "PRODUCT_NAME", "tu_v", "variants.1.name")  # fmt: skip
    price, price2 = claim("c3", "PRICE", "tu_p", "prices.0.amount"), claim(
        "c4", "PRICE", "tu_p2", "prices.0.amount")  # fmt: skip
    stock1, stock2 = claim("c5", "STOCK", "tu_s", "availability"), claim(
        "c6", "STOCK", "tu_s2", "availability")  # fmt: skip
    assert kinds("El {{c1}} cuesta {{c4}}", big, price2) == ["SUBJECT_MISMATCH"]
    assert kinds("El {{c1}} cuesta {{c3}}; el {{c2}}, {{c4}} y está {{c6}}", big, price, small,
                 price2, stock2) == []  # fmt: skip
    assert kinds("Cuesta {{c3}} y está {{c6}}", price, stock2) == ["SUBJECT_MISMATCH"]
    assert kinds("Cuesta {{c3}} y está {{c5}}", price, stock1) == []
    product = claim("c7", "PRODUCT_NAME", "tu_q", "products.0.name")  # el producto: sus variantes
    assert kinds("El {{c7}} cuesta {{c3}}", product, price) == []
    other = ev(SEARCH_PRODUCT, {"products": [{"product_id": str(uuid4()), "name": "Otro"}]})
    done = render(reply("El {{c7}} cuesta {{c3}}", product, price), {**EVIDENCE, "tu_q": other},
                  NOW)  # fmt: skip
    assert marks(done) == ["SUBJECT_MISMATCH"]
    assert done.problems == (Problem("SUBJECT_MISMATCH", "c3", "get_price"),)
    # un nombre inválido empieza otro producto: no hereda el anterior
    gone = claim("c8", "PRODUCT_NAME", "tu_x", "name")
    assert kinds("El {{c1}} y el {{c8}} cuesta {{c4}}", big, gone, price2) == ["EVIDENCE_MISSING"]


def test_the_schema_is_strict_and_its_kinds_are_the_registered_tools() -> None:
    def objects(node: Any) -> list[dict[str, Any]]:
        found = [node] if isinstance(node, dict) and node.get("type") == "object" else []
        children = node.values() if isinstance(node, dict) else node if type(node) is list else ()
        return found + [o for child in children for o in objects(child)]

    assert all(o["additionalProperties"] is False and o["required"] == list(o["properties"])
               for o in objects(REPLY_SCHEMA)) and len(objects(REPLY_SCHEMA)) == 2  # fmt: skip
    kinds = REPLY_SCHEMA["properties"]["claims"]["items"]["properties"]["kind"]["enum"]
    assert kinds == ["PRODUCT_NAME", "PRICE", "REGULAR_PRICE", "PROMOTION", "PROMOTION_TERMS",
                     "STOCK"]  # fmt: skip
    assert all(TOOLS[tool.name] is tool for kind in KINDS.values() for tool in kind.tools)
    assert {k: [t.name for t in v.tools] for k, v in KINDS.items() if k != "PRODUCT_NAME"} == {
        "PRICE": ["get_price"], "REGULAR_PRICE": ["get_price"], "PROMOTION": ["get_price"],
        "PROMOTION_TERMS": ["get_price"], "STOCK": ["get_stock"]}  # fmt: skip
    jsonschema.validate(FULL, REPLY_SCHEMA)


@pytest.mark.parametrize("change", [
    {"intent": "price_question"},  # un campo extra
    {"claims": [{**FULL["claims"][0], "value": "S/ 10"}]},  # el valor, en la claim
    {"claims": [{**FULL["claims"][0], "kind": "DISCOUNT"}]},
    {"claims": [{**FULL["claims"][0], "path": 0}]},
    {"wants_handoff": "no"},
    {"claims": {}},
    {"reply_template": None},
])  # fmt: skip
def test_what_the_schema_rejects_the_server_rejects_too(change: dict[str, Any]) -> None:
    bad = {**FULL, **change}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, REPLY_SCHEMA)
    with pytest.raises(Malformed):
        render(bad, EVIDENCE, NOW)


def test_too_many_claims_placeholders_or_characters_are_rejected() -> None:
    one = claim("c1", "PRICE", "tu_p", "prices.0.amount")
    render(reply("{{c1}}" * 40, one), EVIDENCE, NOW)
    for bad in (reply("{{c1}}" * 41, one), reply("x" * 4001), reply("", *[one] * 41)):
        with pytest.raises(Malformed):
            render(bad, EVIDENCE, NOW)
    missing = [{k: v for k, v in FULL.items() if k != key} for key in FULL]
    raws: list[object] = [None, [], json.dumps(FULL), *missing]
    for raw in raws:
        with pytest.raises(Malformed):
            render(raw, EVIDENCE, NOW)


@pytest.mark.usefixtures("tenant_db")
def test_with_the_real_tools_the_text_has_their_values_and_never_the_exact_stock(
    store: Any,
) -> None:
    """De punta a punta con lo que devuelven `get_product_variants`, `get_price` y `get_stock`."""
    shop, world = store.shop, store.world
    allow(world, "products.view", "prices.view", "inventory.view")
    with tenant_scope(ctx(world.a)):
        put(shop.big, house("MAIN"), SENTINEL)
    calls = {"tu_v": ("get_product_variants", {"product_id": str(shop.red.pk)}),
             "tu_p": ("get_price", {"variant_id": str(shop.big.pk)}),
             "tu_s": ("get_stock", {"variant_id": str(shop.big.pk)})}  # fmt: skip
    found = {ref: invoke_read_only(ctx(world.a, world.ana), ToolCall(ref, name, args)).evidence
             for ref, (name, args) in calls.items()}  # fmt: skip
    variants = found["tu_v"].data["variants"]  # type: ignore[union-attr]
    (index,) = [n for n, v in enumerate(variants) if v["variant_id"] == str(shop.big.pk)]
    claims = [{**c, "path": f"variants.{index}.name"} if c["id"] == "c1" else c
              for c in FULL["claims"]]  # fmt: skip
    done = render({**FULL, "claims": claims}, found, timezone.now())
    assert done.text == (
        f"El {variants[index]['name']} cuesta S/ 49.90 (no S/ 10), antes S/ 59.90 con Promo "
        "Verano (Solo esta semana), y está disponible."
    )
    assert done.ok and str(SENTINEL) not in done.text
    exact = render(reply("{{c1}}", claim("c1", "STOCK", "tu_s", "available")), found,
                   timezone.now())  # fmt: skip
    assert marks(exact) == ["PATH_MISSING"] and str(SENTINEL) not in exact.text
