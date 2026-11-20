"""`get_price` (F8-07c; ADR-034 §6, §13 D-F8-06): el precio publicado de una variante publicada,
con `prices.view` de la persona. Solo de las listas que el propietario marcó (`ai_exposable`) y
que la persona ve, como mucho `MAX_LISTS`, con su nombre y la predeterminada primero; todas por
`PricingService` con `PUBLIC_AI`, en el mismo instante. Una lista sin precio, sin marcar o que la
persona no ve no aparece, sin decir cuál faltó: sin ninguna, `prices` va vacía. Nunca costos,
márgenes, la lista interna, el código de la lista ni el SKU (decisión del propietario en #770)."""

from datetime import datetime
from typing import Any

from apps.access.selectors import ExecutionContext
from apps.ai_agents.fields import Uuid
from apps.ai_agents.registry import Audience, Found, NotFound, Risk, ToolSpec, register
from apps.ai_agents.tools.catalog import published_variants
from apps.pricing import services
from apps.pricing.services import ListNotVisible, PriceQuery, PriceResolution

MAX_LISTS = 3
TEXT = (str, type(None))
PRICE = {"price_list": str, "currency": str, "amount": str, "regular_amount": str,
         "tax_included": bool, "promotion": TEXT, "promotion_terms": TEXT, "valid_until": TEXT,
         "price_id": str}  # fmt: skip


def _row(name: str, found: PriceResolution) -> dict[str, Any]:
    promotion, until = found.promotion, found.valid_until
    return {"price_list": name, "currency": found.currency, "amount": str(found.final_amount),
            "regular_amount": str(found.base_amount), "tax_included": bool(found.tax_included),
            "promotion": promotion.name if promotion else None,
            "promotion_terms": promotion.terms if promotion else None,
            "valid_until": until.isoformat() if until else None,
            "price_id": str(found.price_id)}  # fmt: skip


def _price(ectx: ExecutionContext, args: dict[str, Any]) -> Found:
    variant = published_variants().filter(pk=args["variant_id"]).first()
    if variant is None:
        raise NotFound
    at: datetime | None = None  # el de la primera resolución, para todas
    shown: list[tuple[str, PriceResolution]] = []
    for list_id, name in services.ai_price_lists(ectx, MAX_LISTS):
        query = PriceQuery(variant.pk, list_id, at, audience=services.Audience.PUBLIC_AI)
        try:
            found = services.resolve(ectx, query)
        except ListNotVisible:  # el motor la rechaza: igual que sin precio
            continue
        at = found.resolved_at
        if found.found:
            shown.append((name, found))
    cited = tuple(pk for _, f in shown for pk in (f.price_id, f.promotion_id) if pk)
    data = {"variant_id": str(variant.pk), "prices": [_row(name, f) for name, f in shown]}
    return Found(data, (variant.product_id, variant.pk, *cited))


GET_PRICE = register(ToolSpec(
    name="get_price", permission="prices.view", handler=_price,
    description="El precio publicado de una variante publicada (de `get_product_variants`) en "
    f"las listas que el negocio autorizó para la IA, como mucho {MAX_LISTS}, la predeterminada "
    "primero: `amount` es el precio final (con la promoción que rige) y `regular_amount` el de "
    "la lista. Si `prices` está vacía no hay precio que citar: no lo inventes ni lo estimes.",
    fields={"variant_id": Uuid("El `variant_id` que devolvió `get_product_variants`")},
    output={"variant_id": str, "prices": [PRICE]}, risk=Risk.READ,
    audience=Audience.PUBLIC_SAFE, kind="PRICE",
))  # fmt: skip
