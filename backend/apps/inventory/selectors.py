"""Lecturas del inventario, por organización (RLS y `TenantManager`). El permiso lo exige quien
las sirve (`HasPermission`)."""

from django.db.models import QuerySet

from apps.inventory.models import Warehouse


def warehouses() -> QuerySet[Warehouse]:
    """Los almacenes no borrados, activos o no. El orden lo pone la paginación (ADR-016)."""
    return Warehouse.objects.filter(deleted_at__isnull=True)
