"""Lecturas del inventario, por organización (RLS y `TenantManager`). El permiso lo exige quien
las sirve (`HasPermission`)."""

from django.db.models import QuerySet

from apps.inventory.models import InventoryLevel, InventoryMovement, Warehouse


def warehouses() -> QuerySet[Warehouse]:
    """Los almacenes no borrados, activos o no. El orden lo pone la paginación (ADR-016)."""
    return Warehouse.objects.filter(deleted_at__isnull=True)


def levels() -> QuerySet[InventoryLevel]:
    """Los saldos de la organización, una fila por variante y almacén con movimientos, con su
    variante en la misma consulta, también si está borrada (su saldo sigue ahí)."""
    return InventoryLevel.objects.select_related("variant")


def movements() -> QuerySet[InventoryMovement]:
    """El ledger de la organización: solo se añade. Con su variante en la misma consulta, también
    si está borrada: el ledger la conserva."""
    return InventoryMovement.objects.select_related("variant")
