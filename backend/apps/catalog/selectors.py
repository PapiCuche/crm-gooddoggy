"""Lecturas del catálogo. Filtran por organización (RLS y `TenantManager`), no por permiso: el
permiso lo exige quien las sirve (`HasPermission`)."""

from django.db.models import QuerySet

from apps.catalog.models import Brand, Category


def brands() -> QuerySet[Brand]:
    """Las marcas no borradas de la organización del `tenant_scope` activo. El orden lo pone la
    paginación (ADR-016): el de creación."""
    return Brand.objects.filter(deleted_at__isnull=True)


def categories() -> QuerySet[Category]:
    """Las categorías no borradas de la organización del `tenant_scope` activo. El orden lo pone
    la paginación (ADR-016); quien las enseña en árbol las ordena por `path`."""
    return Category.objects.filter(deleted_at__isnull=True)
