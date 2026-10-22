"""Comandos de productos y variantes (F3-06, F3-06b, F3-07; E02-02 y E02-03).

No comprueban permisos: los declara la API (`products.manage`), la única que los importa
(contrato de import-linter). Validan lo que guardan y auditan cada cambio. Un producto nace con
su variante por defecto: `Product != Variant`, y todo precio y stock apuntará a la variante.
"""

import re
from typing import Any
from uuid import UUID

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.audit.services import Entity, record
from apps.catalog.events import variants_retiring
from apps.catalog.models import Brand, Category, Product, ProductVariant
from apps.catalog.slugs import clean_slug, slug_from
from core.tenancy.context import TenantContext
from core.tenancy.scope import require_scope
from core.text import line
from core.text import name as clean_name

NAME_MAX, SHORT_MAX, SKU_MAX, BARCODE_MAX, LABEL_MAX, DESCRIPTION_MAX = 200, 255, 64, 64, 100, 5000
SKU = re.compile(r"[A-Z0-9]([A-Z0-9._-]*[A-Z0-9])?")  # la misma forma que impone la tabla
SLUG_TAKEN, SKU_TAKEN = "products_org_slug_uq", "product_variants_org_sku_uq"


class ProductSlugTaken(Exception):
    """Ya hay un producto vivo con ese slug en la organización."""


class SkuTaken(Exception):
    """Ya hay una variante viva con ese SKU en la organización."""


class LastVariant(Exception):
    """Un producto no se queda sin variante viva: la última no se borra."""


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


def barcode(value: str) -> str:
    code = value.strip()
    if len(code) > BARCODE_MAX or not (code == "" or code.isascii() and code.isalnum()):
        raise ValueError(f"letras y cifras, hasta {BARCODE_MAX}")
    return code


def label(value: str) -> str:
    return line(value, LABEL_MAX)


def variant_name(product: str, tag: str) -> str:
    """El nombre de una variante: el del producto y, si la tiene, su etiqueta."""
    return f"{product} {tag}" if tag else product


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


PRODUCT_FIELDS = frozenset(
    {"name", "short_description", "description", "status", "ai_visible", "brand_id", "category_id"}
)


def update_product(ctx: TenantContext, *, product_id: UUID, **fields: Any) -> Product:
    """Cambia lo que se envía de un producto vivo y lo audita con el antes y el después; si nada
    cambia, no escribe. Renombrarlo renombra su variante por defecto. `DoesNotExist` si no es de
    la organización o está borrado."""
    alias = require_scope(ctx)
    if unknown := set(fields) - PRODUCT_FIELDS:
        raise ValueError(f"campos no admitidos: {sorted(unknown)}")
    with transaction.atomic(using=alias):
        product: Product = _live(Product, alias).select_for_update(no_key=True).get(pk=product_id)
        values = dict(fields)
        if "brand_id" in values:
            brand = _reference(Brand, alias, values.pop("brand_id"), "brand_id")
            values["brand_id"] = brand.pk if brand else None
        if "category_id" in values:
            category = _reference(Category, alias, values.pop("category_id"), "category_id")
            values["category_id"] = category.pk if category else None
        changes = {
            field: [_plain(getattr(product, field)), _plain(value)]
            for field, value in values.items()
            if getattr(product, field) != value
        }
        if changes:
            for field in changes:
                setattr(product, field, values[field])
            product.save(using=alias, update_fields=[*changes, "updated_at"])
            if "name" in changes:  # cada variante es el producto más su etiqueta
                rows = _live(ProductVariant, alias).select_for_update(no_key=True)
                for variant in rows.filter(product=product):
                    variant.name = variant_name(product.name, variant.label)
                    variant.save(using=alias, update_fields=["name", "updated_at"])
            record(ctx, "product.updated", Entity("product", product.pk, product.slug), changes)
    return product


def _plain(value: Any) -> Any:
    return str(value) if isinstance(value, UUID) else value


def _retiring(alias: str, variant_ids: list[UUID]) -> None:
    """Pregunta a los módulos de encima (`catalog.events`): `VariantsHeld` si alguna de las
    variantes, ya bloqueadas, tiene existencias o reservas."""
    variants_retiring.send(ProductVariant, using=alias, variant_ids=variant_ids)


def delete_product(ctx: TenantContext, *, product_id: UUID) -> None:
    """Borra un producto y sus variantes (borrado lógico) y lo audita; sus SKU y su slug quedan
    libres. Bloquea sus variantes por orden de id (como un movimiento, sin interbloqueos) antes de
    mirar su stock: `VariantsHeld` si alguna tiene existencias o reservas. `DoesNotExist` si no
    es de la organización o ya está borrado."""
    alias = require_scope(ctx)
    with transaction.atomic(using=alias):
        product: Product = _live(Product, alias).select_for_update(no_key=True).get(pk=product_id)
        now = timezone.now()
        rows = _live(ProductVariant, alias).select_for_update(no_key=True).filter(product=product)
        ids = list(rows.order_by("pk").values_list("pk", flat=True))
        _retiring(alias, ids)
        variants = ProductVariant.objects.using(alias).filter(pk__in=ids)
        count = variants.update(deleted_at=now, deleted_by_user_id=ctx.user_id, updated_at=now)
        product.deleted_at, product.deleted_by_user_id = now, ctx.user_id
        product.save(using=alias, update_fields=["deleted_at", "deleted_by_user_id", "updated_at"])
        record(
            ctx,
            "product.deleted",
            Entity("product", product.pk, product.slug),
            {"deleted_at": [None, now.isoformat()]},
            {"variants": count},
        )


def _product(alias: str, product_id: UUID) -> Product:
    """El producto vivo, bloqueado: serializa los cambios de sus variantes."""
    product: Product = _live(Product, alias).select_for_update(no_key=True).get(pk=product_id)
    return product


def create_variant(
    ctx: TenantContext, *, product_id: UUID, sku_code: str, tag: str = "", **fields: Any
) -> ProductVariant:
    """Añade una variante a un producto vivo y lo audita. `SkuTaken` si el SKU ya es de otra
    variante viva; `Product.DoesNotExist` si el producto no es de la organización."""
    alias = require_scope(ctx)
    code = sku(sku_code)
    try:
        with transaction.atomic(using=alias):
            product = _product(alias, product_id)
            variant: ProductVariant = ProductVariant.objects.using(alias).create(
                product=product,
                sku=code,
                label=tag,
                name=variant_name(product.name, tag),
                **fields,
            )
            changes = {"sku": [None, code], "label": [None, tag]}
            record(ctx, "variant.created", Entity("variant", variant.pk, code), changes)
    except IntegrityError as error:
        raise _taken(error, "", code) from None
    return variant


VARIANT_FIELDS = frozenset({"sku", "barcode", "label", "condition", "status", "ai_visible"})


def update_variant(
    ctx: TenantContext, *, product_id: UUID, variant_id: UUID, **fields: Any
) -> ProductVariant:
    """Cambia lo que se envía de una variante viva de ese producto y lo audita; cambiar la
    etiqueta cambia su nombre. `SkuTaken` si el SKU ya es de otra variante viva. `DoesNotExist`
    si la variante no es de ese producto o está borrada."""
    alias = require_scope(ctx)
    if unknown := set(fields) - VARIANT_FIELDS:
        raise ValueError(f"campos no admitidos: {sorted(unknown)}")
    try:
        with transaction.atomic(using=alias):
            product = _product(alias, product_id)
            variant: ProductVariant = (
                _live(ProductVariant, alias)
                .select_for_update(no_key=True)
                .get(pk=variant_id, product=product)
            )
            changes = {
                field: [getattr(variant, field), value]
                for field, value in fields.items()
                if getattr(variant, field) != value
            }
            if changes:
                for field, (_, value) in changes.items():
                    setattr(variant, field, value)
                variant.name = variant_name(product.name, variant.label)
                variant.save(using=alias, update_fields=[*changes, "name", "updated_at"])
                record(ctx, "variant.updated", Entity("variant", variant.pk, variant.sku), changes)
    except IntegrityError as error:
        raise _taken(error, "", fields.get("sku", "")) from None
    return variant


def delete_variant(ctx: TenantContext, *, product_id: UUID, variant_id: UUID) -> None:
    """Borra una variante (borrado lógico) y lo audita; su SKU queda libre. `LastVariant` si es
    la única viva del producto; `VariantsHeld` si tiene existencias o reservas (la mira ya
    bloqueada). `DoesNotExist` si no es de ese producto o ya está borrada."""
    alias = require_scope(ctx)
    with transaction.atomic(using=alias):
        product = _product(alias, product_id)
        variant: ProductVariant = (
            _live(ProductVariant, alias)
            .select_for_update(no_key=True)
            .get(pk=variant_id, product=product)
        )
        if not _live(ProductVariant, alias).filter(product=product).exclude(pk=variant.pk).exists():
            raise LastVariant(variant.sku)
        _retiring(alias, [variant.pk])
        variant.deleted_at, variant.deleted_by_user_id = timezone.now(), ctx.user_id
        variant.save(using=alias, update_fields=["deleted_at", "deleted_by_user_id", "updated_at"])
        record(
            ctx,
            "variant.deleted",
            Entity("variant", variant.pk, variant.sku),
            {"deleted_at": [None, variant.deleted_at.isoformat()]},
        )
