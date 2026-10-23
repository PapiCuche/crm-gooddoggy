"""Lecturas del catálogo. Filtran por organización (RLS y `TenantManager`), no por permiso: el
permiso lo exige quien las sirve (`HasPermission`)."""

from uuid import UUID

from django.db.models import QuerySet

from apps.catalog.models import Brand, Category, Product, ProductVariant


def brands() -> QuerySet[Brand]:
    """Las marcas no borradas de la organización del `tenant_scope` activo. El orden lo pone la
    paginación (ADR-016): el de creación."""
    return Brand.objects.filter(deleted_at__isnull=True)


def categories() -> QuerySet[Category]:
    """Las categorías no borradas de la organización del `tenant_scope` activo. El orden lo pone
    la paginación (ADR-016); quien las enseña en árbol las ordena por `path`."""
    return Category.objects.filter(deleted_at__isnull=True)


def products() -> QuerySet[Product]:
    """Los productos vivos, con su marca y su categoría en la misma consulta."""
    return Product.objects.filter(deleted_at__isnull=True).select_related("brand", "category")


def variants(product_id: UUID) -> QuerySet[ProductVariant]:
    """Las variantes vivas de un producto vivo de la organización."""
    return ProductVariant.objects.filter(
        product_id=product_id, deleted_at__isnull=True, product__deleted_at__isnull=True
    )


def live_variants() -> QuerySet[ProductVariant]:
    """Las variantes vivas de productos vivos, con su producto (el inventario lee
    `tracks_inventory`)."""
    live = ProductVariant.objects.filter(deleted_at__isnull=True, product__deleted_at__isnull=True)
    return live.select_related("product")
