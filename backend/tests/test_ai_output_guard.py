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

from apps.ai_agents import context, guard
from apps.ai_agents.guard import KINDS, Finding, Malformed, Problem, render, reply_schema
from apps.ai_agents.registry import TOOLS, Evidence, ToolSpec, invoke_read_only
from apps.ai_agents.tools.catalog import GET_PRODUCT_VARIANTS, SEARCH_PRODUCT
from apps.ai_agents.tools.price import GET_PRICE, _cut
from apps.ai_agents.tools.stock import GET_STOCK, NOT_TRACKED
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


def prices(variant: str) -> dict[str, Any]:  # con promoción en la primera fila; sin ella después
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
    "tu_p": ev(GET_PRICE, prices(BIG), PRODUCT, BIG, PRICE_ID, PROMO), "tu_fail": None,  # no ok
    "tu_p2": ev(GET_PRICE, prices(SMALL), PRODUCT, SMALL), "tu_s": stock(BIG),
    "tu_old": ev(GET_PRICE, prices(BIG), PRODUCT, BIG, age=660), "tu_s2": stock(SMALL),  # 11 min
    "tu_edge": ev(GET_PRICE, prices(BIG), PRODUCT, BIG, age=600), "tu_s300": stock(BIG, age=300),
    "tu_future": ev(GET_PRICE, prices(BIG), PRODUCT, BIG, age=-1), "tu_nt": stock(BIG, NOT_TRACKED),
    "tu_oldcat": ev(GET_PRODUCT_VARIANTS, VARIANTS, PRODUCT, BIG, age=3600),
    "tu_oldstock": stock(BIG, age=601), "tu_ni": ev(SEARCH_PRODUCT, {"products": [{"name": "X"}]}),
    # con la forma de `get_price`, pero de otra tool o de otro tipo de evidencia
    "tu_quote": Evidence(uuid4(), "PRICE", "get_quote", NOW, (), data=prices(BIG)),
    "tu_kind": Evidence(uuid4(), "CATALOG", "get_price", NOW, (), data=prices(BIG)),
    "tu_o": ev(GET_PRODUCT_VARIANTS, {**VARIANTS, "product_id": str(uuid4()), "name": "Arnés"}),
}  # fmt: skip


def claim(cid: str, kind: str, ref: str, path: str) -> dict[str, str]:
    return {"id": cid, "kind": kind, "evidence_ref": ref, "path": path}


def reply(template: str, *claims: dict[str, str], handoff: bool = False) -> dict[str, Any]:
    return {"reply_template": template, "claims": list(claims), "wants_handoff": handoff}


def marks(done: guard.Rendered) -> list[str]:
    assert all(done.text[f.start : f.end] == VERIFY_MARK for f in done.findings)
    assert {(f.start, f.end) for f in done.findings} <= set(done.inserted)
    return [f.type for f in done.findings]


def marked(template: str, *claims: dict[str, str], **found: Evidence | None) -> list[str]:
    return marks(render(reply(template, *claims), {**EVIDENCE, **found}, NOW))


def one(kind: str, path: str, ref: str = "e", **found: Evidence | None) -> guard.Rendered:
    return render(reply("{{c1}}", claim("c1", kind, ref, path)), {**EVIDENCE, **found}, NOW)


def codes(template: str, *claims: dict[str, str], **found: Evidence | None) -> list[str]:
    done = render(reply(template, *claims), {**EVIDENCE, **found}, NOW)
    marks(done)
    return [p.code for p in done.problems]


def two_promotions() -> Evidence:  # `prices(BIG)` con otra promoción en la fila 1
    data = prices(BIG)
    data["prices"][1] |= {"promotion": "Black Friday", "promotion_id": PRICE_ID}
    data["promotions"].append({"promotion_id": PRICE_ID, "terms": "Solo tarjetas"})
    return ev(GET_PRICE, data, PRODUCT, BIG)


def priced(change: dict[str, Any], **data: Any) -> Evidence:  # `prices(BIG)`, la fila 0 cambiada
    rows = [{**prices(BIG)["prices"][0], **change}, *prices(BIG)["prices"][1:]]
    return ev(GET_PRICE, {**prices(BIG), "prices": rows, **data}, PRODUCT, BIG)


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
    assert [done.text[a:b] for a, b in done.inserted] == [
        "Grande", "S/ 1,249.90", "S/ 1,299.90", "Promo Verano", "Solo esta semana",
        "disponible, con pocas unidades"]  # fmt: skip
    assert done.expires_at == NOW + timedelta(seconds=600) and done.wants_handoff is False
    assert render({**FULL, "wants_handoff": True}, EVIDENCE, NOW).wants_handoff is True


@pytest.mark.parametrize(("change", "text"), [
    ({}, "S/ 1,249.90"), ({"currency": "USD"}, "US$ 1,249.90"),
    ({"currency": "EUR"}, "EUR 1,249.90"),
    ({"tax_included": False}, "S/ 1,249.90 + IGV"), ({"tax_included": 0}, VERIFY_MARK),
    ({"currency": "pen"}, VERIFY_MARK), ({"amount": "1e3"}, VERIFY_MARK),
    ({"amount": "١٢٣"}, VERIFY_MARK), ({"amount": "-1"}, VERIFY_MARK),
])  # fmt: skip
def test_money_is_formatted_on_the_server_or_marked(change: dict[str, Any], text: str) -> None:
    done = one("PRICE", "prices.0.amount", e=priced(change))
    assert done.text == text and done.ok is (text != VERIFY_MARK)


# «tipo:evidencia:path:hallazgo»: de otra invocación o fallida; la tool o el tipo de otra (precio
# de `get_stock`, de otra tool, de otro tipo; stock o nombre de `get_price`; precio del catálogo);
# el path de otro tipo o inexistente (también el stock exacto); el «antes» como actual o sin
# promoción; la evidencia de hace 11 minutos, de 601 segundos o del futuro; un nombre sin sujeto
CASES = """
PRICE:tu_otra:prices.0.amount:EVIDENCE_MISSING PRICE:tu_fail:prices.0.amount:EVIDENCE_MISSING
PRICE:tu_s:availability:KIND_MISMATCH PRICE:tu_quote:prices.0.amount:KIND_MISMATCH
PRICE:tu_kind:prices.0.amount:KIND_MISMATCH STOCK:tu_p:prices.0.amount:KIND_MISMATCH
PRODUCT_NAME:tu_p:prices.0.price_list:KIND_MISMATCH PRICE:tu_v:name:KIND_MISMATCH
PRICE:tu_p:prices.0.price_list:KIND_MISMATCH PROMOTION:tu_p:prices.0.amount:KIND_MISMATCH
PRICE:tu_p:prices.0:KIND_MISMATCH PRICE:tu_p:prices.2.amount:PATH_MISSING
PRICE:tu_p:prices.00.amount:PATH_MISSING PRICE:tu_p:prices.0.cost:PATH_MISSING
PRICE:tu_p:amount:PATH_MISSING STOCK:tu_s:available:PATH_MISSING
PRICE:tu_p:prices.0.regular_amount:PRICE_NOT_CURRENT
PRICE:tu_p:prices.1.regular_amount:PRICE_NOT_CURRENT
REGULAR_PRICE:tu_p:prices.1.regular_amount:NO_PROMOTION
PROMOTION:tu_p:prices.1.promotion:NO_PROMOTION
PRICE:tu_old:prices.0.amount:EVIDENCE_STALE STOCK:tu_oldstock:availability:EVIDENCE_STALE
PRICE:tu_future:prices.0.amount:EVIDENCE_STALE PRODUCT_NAME:tu_ni:products.0.name:VALUE_MISSING
""".split()


@pytest.mark.parametrize("case", CASES)
def test_a_claim_without_valid_evidence_leaves_the_mark_and_a_finding(case: str) -> None:
    kind, ref, path, found = case.split(":")
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
    two = (claim("c1", "PRICE", "tu_p", "prices.0.amount"),
           claim("c2", "STOCK", "tu_s300", "availability"))  # fmt: skip
    first = NOW + timedelta(seconds=300)  # la primera que caduca
    assert render(reply("{{c1}} {{c2}}", *two), EVIDENCE, NOW).expires_at == first
    with pytest.raises(ValueError, match="zona horaria"):
        render(FULL, EVIDENCE, NOW.replace(tzinfo=None))


@pytest.mark.parametrize("value", [0, 601, -1, "600", 600.0, True, None])
def test_the_evidence_ttl_is_a_fixed_integer_of_at_most_ten_minutes(value: Any) -> None:
    assert settings.AI_EVIDENCE_TTL == 600
    with override_settings(AI_EVIDENCE_TTL=value), pytest.raises(ImproperlyConfigured):
        render(FULL, EVIDENCE, NOW)


def test_the_stock_band_is_copied_with_its_fixed_text_never_the_exact_number() -> None:
    for band, text in (("AVAILABLE", "disponible"), ("OUT_OF_STOCK", "agotado")):
        done = one("STOCK", "availability", s=stock(BIG, band), ref="s")
        assert done.text == text and str(SENTINEL) not in done.text
    done = one("STOCK", "availability", "tu_nt")
    assert marks(done) == ["VALUE_MISSING"] and "no afirmes disponibilidad" in done.feedback()


@pytest.mark.parametrize(("terms", "problem"), [
    ("  ", "VALUE_MISSING"), ("Hoy […]", "TRUNCATED"),
    (_cut("Solo esta semana", 0), "TRUNCATED"),  # recortadas a cero: «[…]», sin el espacio
])  # fmt: skip
def test_blank_or_cut_promotion_terms_are_never_inserted(terms: str, problem: str) -> None:
    found = priced({}, promotions=[{"promotion_id": PROMO, "terms": terms}])
    both = (claim("c1", "PRICE", "e", "prices.0.amount"),
            claim("c2", "PROMOTION_TERMS", "e", "promotions.0.terms"))  # fmt: skip
    done = render(reply("{{c1}} ({{c2}})", *both), {"e": found}, NOW)
    assert marks(done) == ["VALUE_MISSING"] and [p.code for p in done.problems] == [problem]


def test_over_the_send_limit_nothing_but_the_mark_goes_out() -> None:
    found = priced({}, promotions=[{"promotion_id": PROMO, "terms": "t" * 2000}])
    claims = (claim("c1", "PRICE", "e", "prices.0.amount"),
              claim("c2", "PROMOTION_TERMS", "e", "promotions.0.terms"))  # fmt: skip
    fits, over = (render(reply("{{c1}}{{c2}}" + "x" * n, *claims), {"e": found}, NOW)
                  for n in (2085, 2086))  # 4096 y 4097 caracteres  # fmt: skip
    assert fits.ok and len(fits.text) == 4096 and over.problems[-1] == Problem("TOO_LONG")
    assert (over.text, marks(over)) == (VERIFY_MARK, ["TOO_LONG"])


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


def test_claims_after_a_product_name_talk_about_that_product_and_one_variant() -> None:
    kinds = marked
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
    assert kinds("El {{c7}} cuesta {{c3}}", product, price, tu_q=other) == ["SUBJECT_MISMATCH"]
    # un nombre inválido empieza otro producto: no hereda el anterior
    gone = claim("c8", "PRODUCT_NAME", "tu_x", "name")
    assert kinds("El {{c1}} y el {{c8}} cuesta {{c4}}", big, gone, price2) == ["EVIDENCE_MISSING"]
    # lo anterior al primer nombre es suyo; otro nombre del producto no cambia la variante
    arnes = claim("c9", "PRODUCT_NAME", "tu_o", "name")
    assert kinds("A {{c3}} tienes el {{c9}}", price, arnes) == ["SUBJECT_MISMATCH"]
    assert kinds("{{c3}} el {{c7}}, {{c6}}", price, product, stock2) == ["SUBJECT_MISMATCH"]
    assert kinds("{{c3}} el {{c7}}, {{c5}}", price, product, stock1) == []
    again = claim("c10", "PRODUCT_NAME", "tu_v", "name")  # el mismo producto: sigue la variante
    assert kinds("{{c7}} a {{c3}}, el {{c10}} {{c6}}", product, price, again, stock2) == [
        "SUBJECT_MISMATCH"]  # fmt: skip
    # dos nombres de productos distintos: el precio es del segundo
    assert kinds("El {{c10}} y el {{c9}} a {{c3}}", again, arnes, price) == ["SUBJECT_MISMATCH"]


def test_before_price_and_promotion_are_of_the_current_price_row_and_terms_of_a_cited_one() -> None:
    now, before = claim("c1", "PRICE", "tu_p", "prices.0.amount"), claim(
        "c2", "REGULAR_PRICE", "tu_p", "prices.0.regular_amount")  # fmt: skip
    promo = claim("c3", "PROMOTION", "tu_p", "prices.0.promotion")
    assert marked("Ahora {{c1}}, antes {{c2}} con {{c3}}", now, before, promo) == []
    assert (
        marked("{{c3}}: {{c1}}", promo, now)
        == marked("Antes {{c2}}, hoy {{c1}}", before, now)
        == []
    )
    other = claim("c1", "PRICE", "tu_p", "prices.1.amount")  # otra lista, sin promoción
    assert marked("Ahora {{c1}}, antes {{c2}}", other, before) == ["SUBJECT_MISMATCH"]
    assert marked("{{c3}}: {{c1}}", promo, other) == ["SUBJECT_MISMATCH"]  # el precio que le sigue
    assert marked("Antes {{c2}}", before) == ["SUBJECT_MISMATCH"]  # un «antes» sin «ahora»
    edge = claim("c3", "PROMOTION", "tu_edge", "prices.0.promotion")  # otra evidencia
    assert marked("{{c1}} con {{c3}}", now, edge) == ["SUBJECT_MISMATCH"]
    terms = claim("c4", "PROMOTION_TERMS", "e", "promotions.1.terms")
    found = two_promotions()
    assert marked("{{c3}}: {{c4}}", {**promo, "evidence_ref": "e"}, terms, e=found) == [
        "SUBJECT_MISMATCH"]  # fmt: skip
    assert marked("{{c3}}: {{c4}}", {**promo, "evidence_ref": "e", "path": "prices.1.promotion"},
                  terms, e=found) == []  # fmt: skip
    assert marked("{{c4}}", terms, e=found) == ["SUBJECT_MISMATCH"]  # sin promoción citada


def test_the_schema_is_strict_and_its_kinds_are_the_registered_tools() -> None:
    schema = reply_schema()
    items = schema["properties"]["claims"]["items"]
    for obj in (schema, items):  # los dos únicos objetos
        assert obj["additionalProperties"] is False and obj["required"] == list(obj["properties"])
    enum = ["PRODUCT_NAME", "PRICE", "REGULAR_PRICE", "PROMOTION", "PROMOTION_TERMS", "STOCK"]
    assert items["properties"]["kind"]["enum"] == enum
    assert all(TOOLS[tool.name] is tool for kind in KINDS.values() for tool in kind.tools)
    assert {k: [t.name for t in v.tools] for k, v in KINDS.items() if k != "PRODUCT_NAME"} == {
        "PRICE": ["get_price"], "REGULAR_PRICE": ["get_price"], "PROMOTION": ["get_price"],
        "PROMOTION_TERMS": ["get_price"], "STOCK": ["get_stock"]}  # fmt: skip
    jsonschema.validate(FULL, schema)
    schema["properties"].clear()  # una copia: cambiarla no cambia el guard ni el contexto
    plan = context.PLANS[context.Purpose.SUGGEST]
    assert reply_schema()["properties"] and plan.output_schema is reply_schema


@pytest.mark.parametrize("change", [  # un campo extra, el valor en la claim, tipos y kinds ajenos
    {"intent": "price_question"}, {"claims": [{**FULL["claims"][0], "value": "S/ 10"}]},
    {"claims": [{**FULL["claims"][0], "kind": "DISCOUNT"}]}, {"wants_handoff": "no"},
    {"claims": [{**FULL["claims"][0], "path": 0}]}, {"claims": {}}, {"reply_template": None},
])  # fmt: skip
def test_what_the_schema_rejects_the_server_rejects_too(change: dict[str, Any]) -> None:
    bad = {**FULL, **change}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, reply_schema())
    with pytest.raises(Malformed):
        render(bad, EVIDENCE, NOW)


def test_too_many_claims_placeholders_or_characters_are_rejected() -> None:
    one = claim("c1", "PRICE", "tu_p", "prices.0.amount")
    render(reply("{{c1}}" * 40, one), EVIDENCE, NOW)
    long = {**one, "path": "a" * 65}  # 64 caracteres o menos por campo
    for bad in (reply("{{c1}}" * 41, one), reply("x" * 4001), reply("", *[one] * 41),
                reply("{{c1}}", long)):  # fmt: skip
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


def test_promotion_terms_go_with_the_nearest_cited_row_and_its_promotion() -> None:
    """Las de la fila citada más cercana (la anterior antes que la siguiente): dos promociones no
    cruzan sus condiciones, ni unas condiciones van junto a una fila sin promoción."""
    promo_a, promo_b = (claim(f"c{n + 1}", "PROMOTION", "e", f"prices.{n}.promotion")
                        for n in (0, 1))  # fmt: skip
    terms_a, terms_b = (claim(f"c{n + 3}", "PROMOTION_TERMS", "e", f"promotions.{n}.terms")
                        for n in (0, 1))  # fmt: skip
    claims, e = (promo_a, promo_b, terms_a, terms_b), two_promotions()
    assert marked("{{c1}} ({{c3}}) y {{c2}} ({{c4}})", *claims, e=e) == []
    assert marked("{{c4}}: {{c2}}", *claims[1::2], e=e) == []  # sin una anterior, la siguiente
    assert marked("{{c1}} ({{c4}}) y {{c2}} ({{c3}})", *claims, e=e) == ["SUBJECT_MISMATCH"] * 2
    price_a, price_b = (claim(f"c{n + 5}", "PRICE", "e", f"prices.{n}.amount") for n in (0, 1))
    assert codes("{{c5}} ({{c4}}) y {{c6}} ({{c3}})", price_a, price_b, terms_a, terms_b, e=e) == [
        "SUBJECT_MISMATCH", "PRICE_TWICE"]  # fmt: skip
    plain = claim("c1", "PRICE", "e", "prices.1.amount")  # la otra lista, sin promoción
    terms = claim("c2", "PROMOTION_TERMS", "e", "promotions.0.terms")
    assert marked("{{c1}} ({{c2}})", plain, terms, e=EVIDENCE["tu_p"]) == ["SUBJECT_MISMATCH"]
    bare = priced({}, promotions=[{"terms": "Sin promoción"}])  # unas condiciones sin su id
    assert marked("{{c1}} ({{c2}})", plain, terms, e=bare) == ["SUBJECT_MISMATCH"]


def test_one_current_price_per_variant_so_two_never_make_a_discount() -> None:
    now_a, now_b = (claim(f"c{n + 1}", "PRICE", "tu_p", f"prices.{n}.amount") for n in (0, 1))
    done = render(reply("Antes {{c1}}, ahora {{c2}}", now_a, now_b), EVIDENCE, NOW)
    assert done.text == "Antes S/ 1,249.90, ahora [verificar]"
    assert marks(done) == ["SUBJECT_MISMATCH"]
    assert done.feedback() == "la claim c2 es otro precio actual de una variante ya citada: da uno"
    later = claim("c2", "PRICE", "n", "prices.0.amount")  # otra evidencia tras un cambio de precio
    assert codes("Antes {{c1}}, ahora {{c2}}", now_a, later, n=priced({"amount": "999.00"})) == [
        "PRICE_TWICE"]  # fmt: skip
    big = claim("c3", "PRODUCT_NAME", "tu_v", "variants.0.name")  # otro nombre no lo reinicia
    assert codes("El {{c3}} costaba {{c1}}; el {{c3}} cuesta {{c2}}", big, now_a, now_b) == [
        "PRICE_TWICE"]  # fmt: skip
    same = claim("c4", "PRICE", "tu_p", "prices.0.amount")  # la misma fila: el mismo precio
    assert marked("{{c1}}, o sea {{c4}} ({{c1}})", now_a, same) == []
    # el «antes» es de la fila del precio que queda, no de la del segundo
    regular_b = claim("c3", "REGULAR_PRICE", "e", "prices.1.regular_amount")
    a, b = ({**now, "evidence_ref": "e"} for now in (now_a, now_b))
    assert codes("{{c1}} y {{c2}}, antes {{c3}}", a, b, regular_b, e=two_promotions()) == [
        "PRICE_TWICE", "SUBJECT_MISMATCH"]  # fmt: skip
