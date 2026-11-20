"""`search_product` y `get_product_variants` (F8-07a; ADR-034 §6): el catálogo publicado, con
`products.view` de la persona. Publicado es vivo, `ACTIVE` y `ai_visible`, en el producto y en la
variante; un producto sin ninguna variante así no existe para la IA. Solo nombre y atributos
públicos: ni la descripción larga, ni SKU, código de barras, costos o notas. La búsqueda mira el
nombre, la descripción corta y la marca y la categoría vivas, sin distinguir mayúsculas ni tildes
(F8-07e), y las dos devuelven menos filas si no caben en el sobre (F8-08c)."""

import unicodedata
from datetime import datetime
from typing import Any

from django.db.models import CharField, F, Func, Q, QuerySet, Value

from apps.access.selectors import ExecutionContext
from apps.ai_agents.fields import Query, Uuid
from apps.ai_agents.registry import Audience, Found, NotFound, Risk, ToolSpec, fits, register
from apps.catalog.models import Product, ProductVariant
from apps.catalog.selectors import live_variants, products

ACTIVE = "ACTIVE"
MAX_PRODUCTS, MAX_VARIANTS = 10, 20
TEXT = (str, type(None))
ROW = {"product_id": str, "name": str, "brand": TEXT, "category": TEXT, "short_description": str}
VARIANT = {"variant_id": str, "name": str, "label": str, "condition": str, "is_default": bool}
# Las letras con tilde, diéresis o cedilla de Latin-1 (`á`, `Ñ`, `ç`…) y su base: `translate` las
# cambia igual en la consulta y en la base de datos (sin la extensión `unaccent`).
_BASE = {c: unicodedata.normalize("NFD", c)[0] for c in map(chr, range(0xC0, 0x100))}
ACCENTED = "".join(c for c, base in _BASE.items() if base != c)
PLAIN = "".join(_BASE[c] for c in ACCENTED)
UNACCENT = str.maketrans(ACCENTED, PLAIN)
SEARCHED = {"name": Q(), "short_description": Q(), "brand__name": Q(brand__deleted_at=None),
            "category__name": Q(category__deleted_at=None)}  # fmt: skip


def _variants() -> QuerySet[ProductVariant]:
    return live_variants().filter(status=ACTIVE, ai_visible=True)


def published_variants() -> QuerySet[ProductVariant]:
    """Las variantes publicadas de productos publicados, con su producto (también `get_stock`)."""
    return _variants().filter(product__status=ACTIVE, product__ai_visible=True)


def _published() -> QuerySet[Product]:
    shown = products().filter(status=ACTIVE, ai_visible=True)
    return shown.filter(pk__in=_variants().values("product_id"))


def _name(related: Any) -> str | None:
    return related.name if related is not None and related.deleted_at is None else None


def _plain(field: str) -> Func:
    return Func(F(field), Value(ACCENTED), Value(PLAIN), function="translate",
                output_field=CharField())  # fmt: skip


def _search(ectx: ExecutionContext, args: dict[str, Any], as_of: datetime) -> Found:
    wanted, match = args["query"].translate(UNACCENT), Q()
    found = _published().alias(**{f"plain_{n}": _plain(field) for n, field in enumerate(SEARCHED)})
    for n, live in enumerate(SEARCHED.values()):  # `icontains` escapa `%` y `_`: son literales
        match |= Q(**{f"plain_{n}__icontains": wanted}) & live
    rows = list(found.filter(match).order_by("name", "id")[:MAX_PRODUCTS])
    data = [{"product_id": str(p.pk), "name": p.name, "brand": _name(p.brand),
             "category": _name(p.category), "short_description": p.short_description}
            for p in rows]  # fmt: skip
    while len(data) > 1 and not fits({"products": data}, "CATALOG"):  # mejor menos que ninguna
        data.pop()
    return Found({"products": data}, tuple(p.pk for p in rows[: len(data)]))


def _product_variants(ectx: ExecutionContext, args: dict[str, Any], as_of: datetime) -> Found:
    product = _published().filter(pk=args["product_id"]).first()
    if product is None:
        raise NotFound
    rows = list(
        _variants().filter(product_id=product.pk).order_by("sort_order", "id")[:MAX_VARIANTS]
    )
    data = [{"variant_id": str(v.pk), "name": v.name, "label": v.label,
             "condition": v.condition, "is_default": v.is_default} for v in rows]  # fmt: skip
    found = {"product_id": str(product.pk), "name": product.name, "variants": data}
    while len(data) > 1 and not fits(found, "CATALOG"):  # F8-08c: menos filas, no ninguna
        data.pop()
    return Found(found, (product.pk, *(v.pk for v in rows[: len(data)])))


SEARCH_PRODUCT = register(ToolSpec(
    name="search_product", permission="products.view", handler=_search,
    description="Busca productos publicados del catálogo por nombre, marca, categoría o "
    "descripción corta, sin distinguir mayúsculas ni tildes. Devuelve como mucho "
    f"{MAX_PRODUCTS}, por nombre (menos si no caben), sin precio ni stock.",
    fields={"query": Query("Lo que busca el cliente: nombre, marca o categoría del producto")},
    output={"products": [ROW]}, risk=Risk.READ, audience=Audience.PUBLIC_SAFE, kind="CATALOG",
))  # fmt: skip
GET_PRODUCT_VARIANTS = register(ToolSpec(
    name="get_product_variants", permission="products.view", handler=_product_variants,
    description="Las variantes vendibles de un producto publicado (de `search_product`), "
    f"como mucho {MAX_VARIANTS} (menos si no caben), sin precio ni stock.",
    fields={"product_id": Uuid("El `product_id` que devolvió `search_product`")},
    output={"product_id": str, "name": str, "variants": [VARIANT]}, risk=Risk.READ,
    audience=Audience.PUBLIC_SAFE, kind="CATALOG",
))  # fmt: skip
