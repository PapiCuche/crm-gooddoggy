"""Lecturas del inventario, por organización (RLS y `TenantManager`). El permiso lo exige quien
las sirve (`HasPermission`)."""

from uuid import UUID

from django.db.models import OuterRef, QuerySet, Subquery

from apps.imports.models import DataImport, DataImportRow
from apps.inventory.models import (
    InventoryLevel,
    InventoryMovement,
    InventoryReservation,
    Warehouse,
)


def warehouses() -> QuerySet[Warehouse]:
    """Los almacenes no borrados, activos o no. El orden lo pone la paginación (ADR-016)."""
    return Warehouse.objects.filter(deleted_at__isnull=True)


def levels() -> QuerySet[InventoryLevel]:
    """Los saldos de la organización, una fila por variante y almacén con movimientos, con su
    variante en la misma consulta, también si está borrada (su saldo sigue ahí)."""
    return InventoryLevel.objects.select_related("variant")


def movements() -> QuerySet[InventoryMovement]:
    """El ledger de la organización: solo se añade. Con su variante en la misma consulta, también
    si está borrada: el ledger la conserva. Cada pata de una transferencia trae el almacén de la
    otra (`other_warehouse_id`, F3-32b) en la misma consulta, por el índice de su grupo; la busca
    en la misma organización (`TenantManager` y RLS), así que nunca nombra un almacén ajeno."""
    # Ojo si el ledger llega a tener alcance por almacén (quien solo ve unos almacenes): esta
    # subconsulta no lo tiene, y daría el almacén de la otra pata aunque quede fuera de ese
    # alcance. Entonces `other_warehouse_id` debe ir en `null` o filtrarse por el mismo alcance
    # (F3-39). Lo mismo en la respuesta de `POST …/transfers/`, que lo copia de la otra pata.
    other = (
        InventoryMovement.objects.filter(transfer_group_id=OuterRef("transfer_group_id"))
        .exclude(pk=OuterRef("pk"))
        .values("warehouse_id")[:1]
    )
    rows: QuerySet[InventoryMovement] = InventoryMovement.objects.select_related("variant")
    return rows.annotate(other_warehouse_id=Subquery(other))


def reservations() -> QuerySet[InventoryReservation]:
    """Las reservas de la organización, en cualquier estado, con su variante en la misma consulta,
    también si está borrada (F3-35b)."""
    return InventoryReservation.objects.select_related("variant")


def stock_imports() -> QuerySet[DataImport]:
    """Las importaciones de stock de la organización (F3-20)."""
    return DataImport.objects.filter(kind=DataImport.Kind.STOCK)


def stock_import_rows(import_id: UUID) -> QuerySet[DataImportRow]:
    """Las filas de una importación, en el orden del archivo: se crearon así (UUIDv7)."""
    return DataImportRow.objects.filter(data_import_id=import_id)
