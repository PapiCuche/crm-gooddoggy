"""Comandos de productos y variantes (F3-06, E02-02 y E02-03).

No comprueban permisos: los declara la API (`products.manage`), la única que los importa
(contrato de import-linter). Validan lo que guardan y auditan cada cambio. Un producto nace con
su variante por defecto: `Product != Variant`, y todo precio y stock apuntará a la variante.
"""

import re
from typing import Any
from uuid import UUID

from django.db import IntegrityError, transaction

from apps.audit.services import Entity, record
from apps.catalog.models import Brand, Category, Product, ProductVariant
from apps.catalog.slugs import clean_slug, slug_from
from core.tenancy.context import TenantContext
from core.tenancy.scope import require_scope
from core.text import line
from core.text import name as clean_name

NAME_MAX, SHORT_MAX, SKU_MAX, DESCRIPTION_MAX = 200, 255, 64, 5000
SKU = re.compile(r"[A-Z0-9]([A-Z0-9._-]*[A-Z0-9])?")  # la misma forma que impone la tabla
SLUG_TAKEN, SKU_TAKEN = "products_org_slug_uq", "product_variants_org_sku_uq"


class ProductSlugTaken(Exception):
    """Ya hay un producto vivo con ese slug en la organización."""


class SkuTaken(Exception):
    """Ya hay una variante viva con ese SKU en la organización."""


class BadReference(ValueError):
    """La marca o la categoría no existen en la organización (o están borradas)."""

    def __init__(self, field: str) -> None:
        super().__init__(field)
        self.field = field


def product_name(value: str) -> str:
    return clean_name(value, NAME_MAX)


def short_description(value: str) -> str:
    return line(value, SHORT_MAX)


def description(value: str) -> str:
    """Texto libre de varias líneas: sin espacios exteriores ni caracteres de control salvo
    saltos de línea y tabuladores."""
    text = value.strip()
    if len(text) > DESCRIPTION_MAX:
        raise ValueError(f"hasta {DESCRIPTION_MAX} caracteres")
    if any(not char.isprintable() and char not in "\n\t" for char in text):
        raise ValueError("solo texto imprimible")
    return text


def sku(value: str) -> str:
    """El SKU como se guarda: en mayúsculas ASCII, cifras, punto, guion y guion bajo entre
    ellos. Solo ASCII: `upper()` podría convertir otras letras en ASCII."""
    code = value.strip().upper() if value.isascii() else ""
    if not (len(code) <= SKU_MAX and SKU.fullmatch(code)):
        raise ValueError(
            f"letras sin acento, cifras y . _ - entre ellas, hasta {SKU_MAX} caracteres"
        )
    return code


def _live(model: Any, alias: str) -> Any:
    return model.objects.using(alias).filter(deleted_at__isnull=True)


def _reference(model: Any, alias: str, pk: UUID | None, field: str) -> Any:
    if pk is None:
        return None
    try:  # bloqueada como al borrarla: no se borra mientras un producto pasa a usarla
        return _live(model, alias).select_for_update(no_key=True).get(pk=pk)
    except model.DoesNotExist:
        raise BadReference(field) from None


def _taken(error: IntegrityError, slug: str, code: str) -> Exception:
    text = str(error)
    if SLUG_TAKEN in text:
        return ProductSlugTaken(slug)
    if SKU_TAKEN in text:
        return SkuTaken(code)
    raise error


def create_product(
    ctx: TenantContext,
    *,
    name: str,
    sku_code: str,
    slug: str | None = None,
    product_type: str = Product.Type.PHYSICAL,
    brand_id: UUID | None = None,
    category_id: UUID | None = None,
    **fields: Any,
) -> Product:
    """Crea un producto en borrador y su variante por defecto con `sku_code`, y lo audita. Un
    servicio no lleva stock. `ProductSlugTaken`, `SkuTaken` o `BadReference` sin escribir nada."""
    alias = require_scope(ctx)
    cleaned, code = product_name(name), sku(sku_code)
    chosen = clean_slug(slug) if slug is not None else slug_from(cleaned)
    try:
        with transaction.atomic(using=alias):
            product: Product = Product.objects.using(alias).create(
                name=cleaned,
                slug=chosen,
                product_type=product_type,
                tracks_inventory=product_type == Product.Type.PHYSICAL,
                brand=_reference(Brand, alias, brand_id, "brand_id"),
                category=_reference(Category, alias, category_id, "category_id"),
                **fields,
            )
            variant = ProductVariant.objects.using(alias).create(
                product=product, sku=code, name=cleaned, is_default=True
            )
            changes = {"name": [None, cleaned], "slug": [None, chosen], "sku": [None, code]}
            metadata = {"variant_id": str(variant.pk)}
            record(ctx, "product.created", Entity("product", product.pk, chosen), changes, metadata)
    except IntegrityError as error:
        raise _taken(error, chosen, code) from None
    return product
