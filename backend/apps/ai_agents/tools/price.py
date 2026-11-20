"""`get_price` (F8-07c; ADR-034 §6, §13 D-F8-06): el precio publicado de una variante publicada,
con `prices.view` de la persona. Solo de las listas que el propietario marcó (`ai_exposable`) y
que la persona ve, como mucho `MAX_LISTS`, con su nombre y la predeterminada primero; todas por
`PricingService` con `PUBLIC_AI`, en el instante de la evidencia (F8-08c). Una lista sin precio,
sin marcar o que la persona no ve no aparece, sin decir cuál faltó: sin ninguna, `prices` va
vacía. Nunca costos, márgenes, la lista interna, el código de la lista ni el SKU (decisión del
propietario en #770). Las condiciones de cada promoción van una sola vez (F8-08c)."""

from datetime import datetime
from typing import Any

from apps.access.selectors import ExecutionContext
from apps.ai_agents.fields import Uuid
from apps.ai_agents.registry import Audience, Found, NotFound, Risk, ToolSpec, fits, register
from apps.ai_agents.tools.catalog import published_variants
from apps.pricing import services
from apps.pricing.services import ListNotVisible, PriceQuery, PriceResolution

MAX_LISTS = 3
TEXT = (str, type(None))
PRICE = {"price_list": str, "currency": str, "amount": str, "regular_amount": str,
         "tax_included": bool, "promotion": TEXT, "promotion_id": TEXT, "valid_until": TEXT,
         "price_id": str}  # fmt: skip
TERMS = {"promotion_id": str, "terms": str}  # una vez por promoción, no por lista (F8-08c)
CUT = " […]"  # la marca fija de unas condiciones recortadas


def _row(name: str, found: PriceResolution) -> dict[str, Any]:
    promotion, until = found.promotion, found.valid_until
    return {"price_list": name, "currency": found.currency, "amount": str(found.final_amount),
            "regular_amount": str(found.base_amount), "tax_included": bool(found.tax_included),
            "promotion": promotion.name if promotion else None,
            "promotion_id": str(promotion.id) if promotion else None,
            "valid_until": until.isoformat() if until else None,
            "price_id": str(found.price_id)}  # fmt: skip


def _cut(terms: str, keep: int) -> str:
    return terms if len(terms) <= keep else (terms[:keep].rstrip() + CUT).lstrip()


def _fitted(base: dict[str, Any], terms: dict[str, str]) -> dict[str, Any]:
    """`base` con las condiciones de cada promoción; si no caben en el sobre, todas recortadas al
    mismo número de caracteres, el mayor que cabe (las listas no se tocan: sin condiciones,
    tres listas con nombres y promociones de su largo máximo caben de sobra)."""

    def data(keep: int) -> dict[str, Any]:
        cut = [{"promotion_id": pk, "terms": _cut(text, keep)} for pk, text in terms.items()]
        return {**base, "promotions": cut}

    low, high = 0, max(map(len, terms.values()), default=0)
    if fits(data(high), "PRICE"):
        return data(high)
    while low < high - 1:  # `low` siempre cabe (o no hay nada más corto); `high`, no
        mid = (low + high) // 2
        low, high = (mid, high) if fits(data(mid), "PRICE") else (low, mid)
    return data(low)


def _price(ectx: ExecutionContext, args: dict[str, Any], as_of: datetime) -> Found:
    variant = published_variants().filter(pk=args["variant_id"]).first()
    if variant is None:
        raise NotFound
    shown: list[tuple[str, PriceResolution]] = []
    for list_id, name in services.ai_price_lists(ectx, MAX_LISTS):
        # todas en el instante de la evidencia, no en el `now()` de la transacción (F8-08c)
        query = PriceQuery(variant.pk, list_id, as_of, audience=services.Audience.PUBLIC_AI)
        try:
            found = services.resolve(ectx, query)
        except ListNotVisible:  # el motor la rechaza: igual que sin precio
            continue
        if found.found:
            shown.append((name, found))
    cited = tuple(pk for _, f in shown for pk in (f.price_id, f.promotion_id) if pk)
    terms = {str(f.promotion.id): f.promotion.terms for _, f in shown if f.promotion}
    base = {"variant_id": str(variant.pk), "prices": [_row(name, f) for name, f in shown]}
    return Found(_fitted(base, terms), (variant.product_id, variant.pk, *cited))


GET_PRICE = register(ToolSpec(
    name="get_price", permission="prices.view", handler=_price,
    description="El precio publicado de una variante publicada (de `get_product_variants`) en "
    f"las listas que el negocio autorizó para la IA, como mucho {MAX_LISTS}, la predeterminada "
    "primero: `amount` es el precio final (con la promoción que rige) y `regular_amount` el de "
    "la lista. Las condiciones de cada promoción van una vez en `promotions` (recortadas con "
    f"«{CUT.strip()}» si no caben). Si `prices` está vacía no hay precio que citar: no lo "
    "inventes ni lo estimes.",
    fields={"variant_id": Uuid("El `variant_id` que devolvió `get_product_variants`")},
    output={"variant_id": str, "prices": [PRICE], "promotions": [TERMS]}, risk=Risk.READ,
    audience=Audience.PUBLIC_SAFE, kind="PRICE",
))  # fmt: skip
