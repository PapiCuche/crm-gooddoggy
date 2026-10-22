"""Comandos de almacenes (F3-08, E04-01): crear, cambiar y borrar (F3-11).

No comprueban permisos: los declara la API (`warehouses.manage`), la única que los importa
(contrato de import-linter). Validan lo que guardan y auditan cada cambio. El código y la
sucursal no cambian: así se nombra el almacén en los movimientos.
"""

import re
from typing import Any
from uuid import UUID

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.audit.services import Entity, record
from apps.inventory.models import InventoryLevel, Warehouse
from core.tenancy.context import TenantContext
from core.tenancy.scope import require_scope
from core.text import name as clean_name

CODE = re.compile(r"[A-Z0-9]+(-[A-Z0-9]+)*")  # la misma forma que impone la tabla
CODE_MAX, NAME_MAX = 20, 100
CODE_TAKEN, BRANCH_FK = "warehouses_org_code_uq", "warehouses_branch_fk"
UPDATABLE = frozenset({"name", "type", "is_sellable", "is_active"})


class WarehouseCodeTaken(Exception):
    """Ya hay un almacén vivo con ese código en la organización."""


class WarehouseHasStock(Exception):
    """El almacén tiene existencias: no se borra."""


class UnknownBranch(ValueError):
    """La sucursal no es de la organización."""


def warehouse_code(value: str) -> str:
    """El código como se guarda: en mayúsculas ASCII, cifras y guiones entre ellas. Solo ASCII:
    `upper()` convertiría otras letras en letras ASCII."""
    code = value.strip().upper() if value.isascii() else ""
    if not (len(code) <= CODE_MAX and CODE.fullmatch(code)):
        raise ValueError(f"letras sin acento, cifras y guiones entre ellas, hasta {CODE_MAX}")
    return code


def warehouse_name(value: str) -> str:
    return clean_name(value, NAME_MAX)


def _cleaned(fields: dict[str, Any]) -> dict[str, Any]:
    """Solo lo que se puede cambiar, con el nombre limpio. El tipo lo comprueba la tabla."""
    if unknown := set(fields) - UPDATABLE:
        raise ValueError(f"campos no admitidos: {sorted(unknown)}")
    return {k: warehouse_name(v) if k == "name" else v for k, v in fields.items()}


def create_warehouse(
    ctx: TenantContext, *, branch_id: UUID, code: str, name: str, **fields: Any
) -> Warehouse:
    """Crea un almacén activo en una sucursal de la organización de `ctx` y lo audita.
    `WarehouseCodeTaken` o `UnknownBranch` sin escribir nada: la FK compuesta decide si la
    sucursal es de la organización."""
    alias = require_scope(ctx)
    values = {"code": warehouse_code(code), **_cleaned({"name": name, **fields})}
    try:
        with transaction.atomic(using=alias):  # savepoint: el almacén y su auditoría, o nada
            values["branch_id"] = branch_id
            warehouse: Warehouse = Warehouse.objects.using(alias).create(**values)
            entity = Entity("warehouse", warehouse.pk, warehouse.code)
            record(ctx, "warehouse.created", entity, {k: [None, v] for k, v in values.items()})
    except IntegrityError as error:
        if BRANCH_FK in str(error):
            raise UnknownBranch("branch_id") from None
        if CODE_TAKEN in str(error):
            raise WarehouseCodeTaken(values["code"]) from None
        raise
    return warehouse


def update_warehouse(ctx: TenantContext, *, warehouse_id: UUID, **fields: Any) -> Warehouse:
    """Cambia lo que se envía (nombre, tipo, si cuenta para la venta, si está activo) y lo
    audita con el antes y el después. Sin cambios no escribe ni audita. Uno borrado o de otra
    organización: `DoesNotExist`."""
    alias = require_scope(ctx)
    values = _cleaned(fields)
    with transaction.atomic(using=alias):
        rows = Warehouse.objects.using(alias).select_for_update(no_key=True)
        warehouse: Warehouse = rows.filter(deleted_at__isnull=True).get(pk=warehouse_id)
        changes = {
            field: [getattr(warehouse, field), value]
            for field, value in values.items()
            if getattr(warehouse, field) != value
        }
        if changes:
            for field, (_, value) in changes.items():
                setattr(warehouse, field, value)
            warehouse.save(using=alias, update_fields=[*changes, "updated_at"])
            entity = Entity("warehouse", warehouse.pk, warehouse.code)
            record(ctx, "warehouse.updated", entity, changes)
    return warehouse


def delete_warehouse(ctx: TenantContext, *, warehouse_id: UUID) -> None:
    """Borra el almacén (borrado lógico), libera su código y lo audita. `WarehouseHasStock` si
    tiene existencias. `FOR UPDATE` choca con el `FOR KEY SHARE` de los movimientos: espera a
    los que están en curso, y los que llegan después ya no lo encuentran. Uno borrado o de otra
    organización: `DoesNotExist`."""
    alias = require_scope(ctx)
    with transaction.atomic(using=alias):
        rows = Warehouse.objects.using(alias).select_for_update()
        warehouse: Warehouse = rows.filter(deleted_at__isnull=True).get(pk=warehouse_id)
        stock = InventoryLevel.objects.using(alias).filter(warehouse=warehouse, on_hand__gt=0)
        if stock.exists():
            raise WarehouseHasStock(warehouse_id)
        warehouse.deleted_at, warehouse.deleted_by_user_id = timezone.now(), ctx.user_id
        warehouse.save(
            using=alias, update_fields=["deleted_at", "deleted_by_user_id", "updated_at"]
        )
        record(ctx, "warehouse.deleted", Entity("warehouse", warehouse.pk, warehouse.code))
