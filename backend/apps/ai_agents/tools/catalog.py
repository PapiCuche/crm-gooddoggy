"""`search_product` y `get_product_variants` (F8-07a; ADR-034 §6): el catálogo publicado, con
`products.view` de la persona. Publicado es vivo, `ACTIVE` y `ai_visible`, en el producto y en la
variante; un producto sin ninguna variante así no existe para la IA. Solo nombre y atributos
públicos: ni la descripción larga, ni SKU, código de barras, costos o notas."""

from typing import Any

from django.db.models import QuerySet

from apps.access.selectors import ExecutionContext
from apps.ai_agents.fields import Query, Uuid
from apps.ai_agents.registry import Audience, Found, NotFound, Risk, ToolSpec, register
from apps.catalog.models import Product, ProductVariant
from apps.catalog.selectors import live_variants, products, search

ACTIVE = "ACTIVE"
MAX_PRODUCTS, MAX_VARIANTS = 10, 20
TEXT = (str, type(None))
ROW = {"product_id": str, "name": str, "brand": TEXT, "category": TEXT, "short_description": str}
VARIANT = {"variant_id": str, "name": str, "label": str, "condition": str, "is_default": bool}


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


def _search(ectx: ExecutionContext, args: dict[str, Any]) -> Found:
    found = search(_published(), args["query"], ("name", "short_description"))
    rows = list(found.order_by("name", "id")[:MAX_PRODUCTS])
    data = [{"product_id": str(p.pk), "name": p.name, "brand": _name(p.brand),
             "category": _name(p.category), "short_description": p.short_description}
            for p in rows]  # fmt: skip
    return Found({"products": data}, tuple(p.pk for p in rows))


def _product_variants(ectx: ExecutionContext, args: dict[str, Any]) -> Found:
    product = _published().filter(pk=args["product_id"]).first()
    if product is None:
        raise NotFound
    rows = list(
        _variants().filter(product_id=product.pk).order_by("sort_order", "id")[:MAX_VARIANTS]
    )
    data = [{"variant_id": str(v.pk), "name": v.name, "label": v.label,
             "condition": v.condition, "is_default": v.is_default} for v in rows]  # fmt: skip
    found = {"product_id": str(product.pk), "name": product.name, "variants": data}
    return Found(found, (product.pk, *(v.pk for v in rows)))


SEARCH_PRODUCT = register(ToolSpec(
    name="search_product", permission="products.view", handler=_search,
    description="Busca productos publicados del catálogo por nombre o descripción corta. "
    f"Devuelve como mucho {MAX_PRODUCTS}, sin precio ni stock.",
    fields={"query": Query("Lo que busca el cliente: nombre, marca o tipo de producto")},
    output={"products": [ROW]}, risk=Risk.READ, audience=Audience.PUBLIC_SAFE, kind="CATALOG",
))  # fmt: skip
GET_PRODUCT_VARIANTS = register(ToolSpec(
    name="get_product_variants", permission="products.view", handler=_product_variants,
    description="Las variantes vendibles de un producto publicado (de `search_product`), "
    f"como mucho {MAX_VARIANTS}, sin precio ni stock.",
    fields={"product_id": Uuid("El `product_id` que devolvió `search_product`")},
    output={"product_id": str, "name": str, "variants": [VARIANT]}, risk=Risk.READ,
    audience=Audience.PUBLIC_SAFE, kind="CATALOG",
))  # fmt: skip
