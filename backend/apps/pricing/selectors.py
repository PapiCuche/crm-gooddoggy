"""Lecturas de precios, por organización (RLS y `TenantManager`). El permiso de la ruta lo exige
quien las sirve (`HasPermission`); la visibilidad de cada lista, estas lecturas."""

from django.db.models import QuerySet

from apps.access.selectors import ExecutionContext, has_permission
from apps.pricing.models import READ_PERMISSIONS, PriceList

MANAGE = "prices.manage"


def visible_price_lists(ectx: ExecutionContext) -> QuerySet[PriceList]:
    """Las listas no borradas que el actor puede ver: las que piden un permiso de lectura que
    tiene, o todas con `prices.manage`. Una que no ve no se distingue de una que no existe. El
    orden lo pone la paginación (ADR-016)."""
    lists = PriceList.objects.filter(deleted_at__isnull=True)
    if has_permission(ectx, MANAGE):
        return lists
    held = [code for code in READ_PERMISSIONS if has_permission(ectx, code)]
    return lists.filter(required_permission__in=held)
