"""Importar stock por CSV o XLSX (F3-20, F3-23, E04-07, ADR-028 §5): cantidades absolutas por SKU
y almacén.

Previsualizar no cambia el stock: lee el archivo (un ZIP con `apps.imports.xlsxfile`, que mira
si es un XLSX por su firma; lo demás con `apps.imports.csvfile`), valida cada fila
contra lo que hay y guarda la importación con sus filas y el `on_hand` que vio en cada celda.

Aplicar (F3-21) es síncrono, en la transacción de la petición: bloquea la importación (dos
aplicaciones a la vez van de una en una) y lleva cada fila a `stock.move` como un `ADJUSTMENT`
con la cantidad del archivo, `expected_on_hand` = lo visto y una clave derivada de la importación
y de la línea. Con la celda bloqueada, si el saldo cambió la fila queda `STALE` y no se aplica;
repetir no crea movimientos (la clave). Las filas van por variante y almacén ascendentes, el
orden de bloqueo de F3-09: dos importaciones a la vez no se interbloquean.

Revertir (F3-24) devuelve cada celda que la importación movió al saldo de antes con otro
`ADJUSTMENT` (`reference_type = IMPORT`, la clave de su fila con `REVERT_KEY`), solo si nadie la
escribió después: su `version` es la que dejó la importación (`expected_version`; un recuento que
la devolvió al mismo saldo también la cambia). Si no, la fila queda `STALE` y no se toca.
Nada del ledger se edita ni se borra: quedan los dos movimientos. Mismo orden de bloqueo que al
aplicar; revertir otra vez no crea movimientos (el estado y la clave).

No comprueba permisos: los declara la API (`inventory.adjust`), la única que lo importa. Ningún
almacén tiene hoy otra autoridad que el CRM: cuando exista la marca de almacén del conector
(ADR-028 §3), su work item rechaza aquí, al validar, las filas de esos almacenes (§5).
"""

import hashlib
from collections import defaultdict
from dataclasses import replace
from typing import Any
from uuid import UUID

from django.utils import timezone

from apps.audit.services import Entity, record
from apps.catalog.selectors import live_variants
from apps.imports import csvfile, xlsxfile
from apps.imports.models import DataImport, DataImportRow
from apps.inventory import stock
from apps.inventory.models import InventoryLevel, InventoryMovement, Warehouse
from apps.inventory.stock import QUANTITY_MAX
from core.tenancy.context import TenantContext
from core.tenancy.scope import require_scope

Action, Kind = DataImportRow.Action, DataImport.Kind
COLUMNS = {  # columna → alias de la cabecera (sin tildes ni mayúsculas, ver `csvfile.key`)
    "sku": ("sku",),
    "almacen": ("almacen", "codigo_almacen", "warehouse", "warehouse_code"),
    "cantidad": ("cantidad", "quantity", "stock", "on_hand"),
}
NAME_MAX = 255
REASON, MOVEMENT = "Importación de stock", "inventory_movement"
REVERT_REASON, REVERT_KEY = "Reversión de una importación de stock", ":revert"
REFUSED: dict[
    type[Exception], str
] = {  # lo que pudo cambiar desde la previsualización y deja la fila en ERROR
    stock.BadReference: "La variante o el almacén ya no existen.",
    stock.WarehouseInactive: "Almacén: está desactivado.",
    stock.InsufficientStock: "Cantidad: ahora hay más reservadas que eso.",
    stock.KeyReused: "Su clave ya registró otro movimiento.",  # no debería pasar: `IMPORT_KEY`
}


def template() -> bytes:
    """La plantilla: solo la cabecera."""
    return csvfile.write(list(COLUMNS), [])


def _quantity(text: str) -> int | None:
    """Lo contado: un entero de 0 a `QUANTITY_MAX` en cifras ASCII, sin signo ni decimales."""
    value = text.strip()
    if value.isascii() and value.isdigit() and len(value) <= 7 and int(value) <= QUANTITY_MAX:
        return int(value)
    return None


def code(text: str) -> str:
    """Un SKU o un código de almacén como se guardan: en mayúsculas ASCII. Otro texto no coincide
    con ninguno (`"ıp16".upper()` es `"IP16"`)."""
    value = text.strip()
    return value.upper() if value.isascii() else ""


class _Lookup:
    """Lo que hay en la organización para las celdas del archivo, en tres consultas."""

    def __init__(self, alias: str, rows: list[tuple[int, dict[str, str]]]) -> None:
        skus = {code(cells["sku"]) for _, cells in rows}
        codes = {code(cells["almacen"]) for _, cells in rows}
        found = live_variants().using(alias).filter(sku__in=skus)
        self.variants = {variant.sku: variant for variant in found}
        alive = Warehouse.objects.using(alias).filter(code__in=codes, deleted_at__isnull=True)
        self.warehouses = {warehouse.code: warehouse for warehouse in alive}
        levels = InventoryLevel.objects.using(alias).filter(
            variant_id__in=[v.pk for v in self.variants.values()],
            warehouse_id__in=[w.pk for w in self.warehouses.values()],
        )
        self.levels = {(lv.variant_id, lv.warehouse_id): lv for lv in levels}


def _check(lookup: _Lookup, cells: dict[str, str]) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    if any(len(text) > csvfile.CELL_MAX for text in cells.values()):
        return {}, [f"Alguna celda pasa de {csvfile.CELL_MAX} caracteres."]
    variant = lookup.variants.get(code(cells["sku"]))
    warehouse = lookup.warehouses.get(code(cells["almacen"]))
    if variant is None:
        errors.append(
            "SKU: no hay una variante con ese SKU." if cells["sku"].strip() else "SKU: falta."
        )
    elif not variant.product.tracks_inventory:
        errors.append("SKU: es un servicio, no lleva stock.")
    if warehouse is None:
        errors.append("Almacén: no hay un almacén con ese código.")
    elif not warehouse.is_active:
        errors.append("Almacén: está desactivado.")
    if (quantity := _quantity(cells["cantidad"])) is None:
        errors.append(f"Cantidad: un entero entre 0 y {QUANTITY_MAX}, sin decimales.")
    if errors or variant is None or warehouse is None or quantity is None:
        return {}, errors
    level = lookup.levels.get((variant.pk, warehouse.pk))
    on_hand, reserved = (level.on_hand, level.reserved) if level else (0, 0)
    if quantity < reserved:
        errors.append(f"Cantidad: hay {reserved} reservadas y la importación no libera reservas.")
    seen = {"variant_id": str(variant.pk), "warehouse_id": str(warehouse.pk)}
    return {**seen, "quantity": quantity, "on_hand_seen": on_hand, "new": level is None}, errors


def _summary(job: DataImport, sheet: csvfile.Sheet, rows: list[DataImportRow]) -> None:
    actions = [row.action for row in rows]
    job.total_rows, job.error_rows = len(rows), actions.count(Action.ERROR)
    job.valid_rows = job.total_rows - job.error_rows
    job.new_count, job.update_count = actions.count(Action.CREATE), actions.count(Action.UPDATE)
    job.summary |= {"encoding": sheet.encoding, "separator": sheet.separator}
    job.summary |= {"ignored_columns": sheet.ignored, "unchanged": actions.count(Action.UNCHANGED)}


def preview(ctx: TenantContext, *, file_name: str, data: bytes) -> DataImport:
    """Valida el archivo y guarda la importación (`VALIDATED`) con una fila por línea con texto.
    `csvfile.Unreadable` si el archivo entero no se puede leer: no guarda nada."""
    alias = require_scope(ctx)
    assert ctx.user_id is not None  # noqa: S101 — la sube una persona (la API)
    sheet = (xlsxfile.read if xlsxfile.is_zip(data) else csvfile.read)(data, COLUMNS)
    if not sheet.rows:
        raise csvfile.Unreadable("El archivo no tiene filas, solo la cabecera.")
    digest = hashlib.sha256(data).hexdigest()
    earlier = DataImport.objects.using(alias).filter(kind=Kind.STOCK, content_sha256=digest)
    before = earlier.order_by("-id").values_list("id", flat=True).first()
    lookup = _Lookup(alias, sheet.rows)
    checked = [(line, cells, *_check(lookup, cells)) for line, cells in sheet.rows]
    lines: dict[tuple[Any, Any], list[int]] = defaultdict(list)
    for line, _, normalized, _ in checked:
        if normalized:
            lines[normalized["variant_id"], normalized["warehouse_id"]].append(line)
    name = "".join(char for char in file_name if char.isprintable()).strip()[:NAME_MAX]
    job = DataImport(
        organization_id=ctx.organization_id,
        kind=Kind.STOCK,
        status=DataImport.Status.VALIDATED,
        file_name=name or "stock.csv",
        content_sha256=digest,
        created_by_user_id=ctx.user_id,
        summary={"duplicate_of": str(before) if before else None},
    )
    rows = []
    for line, cells, normalized, errors in checked:
        cell = (normalized.get("variant_id"), normalized.get("warehouse_id"))
        if other := next((n for n in lines.get(cell, []) if n != line), None):
            errors.append(f"La misma celda (SKU y almacén) que la línea {other}.")
        new = normalized.pop("new", False)
        if errors:
            action, normalized = Action.ERROR, {}
        elif normalized["quantity"] == normalized["on_hand_seen"]:
            action = Action.UNCHANGED
        else:
            action = Action.CREATE if new else Action.UPDATE
        raw = {column: text[: csvfile.CELL_MAX] for column, text in cells.items()}
        rows.append(
            DataImportRow(
                organization_id=ctx.organization_id,
                data_import=job,
                row_number=line,
                raw=raw,
                normalized=normalized,
                action=action,
                errors=errors,
            )
        )
    _summary(job, sheet, rows)
    job.save(using=alias)
    DataImportRow.objects.using(alias).bulk_create(rows)
    counts = {"rows": job.total_rows, "errors": job.error_rows, "repeated": before is not None}
    record(ctx, "inventory.import_uploaded", Entity("data_import", job.pk), metadata=counts)
    return job


def _stale(row: DataImportRow, on_hand: int) -> None:
    row.action = Action.STALE
    seen = row.normalized["on_hand_seen"]
    row.errors = [f"El saldo cambió desde la previsualización: era {seen}, ahora {on_hand}."]


def _apply_row(alias: str, ctx: TenantContext, job: DataImport, row: DataImportRow) -> None:
    variant, warehouse = UUID(row.normalized["variant_id"]), UUID(row.normalized["warehouse_id"])
    if row.action == Action.UNCHANGED:  # sin movimiento; sin escribir, no hace falta bloquear
        cell = InventoryLevel.objects.using(alias).filter(variant=variant, warehouse=warehouse)
        on_hand = cell.values_list("on_hand", flat=True).first() or 0
        if on_hand == row.normalized["on_hand_seen"]:
            return
        _stale(row, on_hand)
    else:
        key = f"{stock.IMPORT_KEY}{job.pk}:{row.row_number}"
        adjust = stock.Type.ADJUSTMENT, row.normalized["quantity"], REASON, key
        wanted = stock.Wanted(variant, warehouse, *adjust, row.normalized["on_hand_seen"])
        wanted = replace(wanted, reference_type=stock.Reference.IMPORT, reference_id=job.pk)
        try:
            movement, new = stock.move(ctx, wanted)
            row.target_entity_type, row.target_entity_id = MOVEMENT, movement.pk
            if new:  # la `version` que dejó, con la celda aún bloqueada: revertir la compara
                cell = InventoryLevel.objects.using(alias).filter(
                    variant=variant, warehouse=warehouse
                )
                row.normalized["version"] = cell.values_list("version", flat=True).get()
        except stock.Stale as error:
            _stale(row, error.on_hand)
        except (
            stock.BadReference,
            stock.WarehouseInactive,
            stock.InsufficientStock,
            stock.KeyReused,
        ) as error:
            row.action, row.errors = Action.ERROR, [REFUSED[type(error)]]
    fields = ["action", "errors", "normalized", "target_entity_type", "target_entity_id"]
    row.save(using=alias, update_fields=fields)


def apply(ctx: TenantContext, *, import_id: UUID) -> DataImport:
    """Aplica una importación previsualizada y la audita. Una aplicada se devuelve tal cual.
    `DataImport.DoesNotExist` si no es una importación de stock de la organización."""
    alias = require_scope(ctx)
    found = DataImport.objects.using(alias).select_for_update().filter(kind=Kind.STOCK)
    job: DataImport = found.get(pk=import_id)
    if job.status != DataImport.Status.VALIDATED:
        return job  # ya aplicada: aplicar otra vez no cambia nada
    pending = DataImportRow.objects.using(alias).filter(
        data_import=job, action__in=[Action.CREATE, Action.UPDATE, Action.UNCHANGED]
    )
    for row in sorted(
        pending, key=lambda r: (r.normalized["variant_id"], r.normalized["warehouse_id"])
    ):
        _apply_row(alias, ctx, job, row)
    done = DataImportRow.objects.using(alias).filter(data_import=job)
    result = {
        "applied": done.filter(target_entity_id__isnull=False).count(),
        "unchanged": done.filter(action=Action.UNCHANGED).count(),
        "stale": done.filter(action=Action.STALE).count(),
        "failed": done.filter(action=Action.ERROR).count() - job.error_rows,
    }
    job.status, job.applied_by_user_id = DataImport.Status.APPLIED, ctx.user_id
    job.applied_at, job.summary = timezone.now(), {**job.summary, "result": result}
    job.save(using=alias, update_fields=["status", "applied_by_user_id", "applied_at", "summary"])
    record(ctx, "inventory.import_applied", Entity("data_import", job.pk), metadata=result)
    return job


class NotApplied(Exception):
    """Solo se revierte una importación aplicada."""


REVERT_REFUSED = {**REFUSED, stock.InsufficientStock: "Hay más reservadas que el saldo de antes."}
TOO_MANY = f"El saldo de antes pasa de {QUANTITY_MAX}, el tope de un ajuste: corrígelo a mano."


def _revert_row(
    ctx: TenantContext, job: DataImport, row: DataImportRow, done: InventoryMovement
) -> str:
    """Devuelve la celda al saldo de antes de la fila: `reverted`, `stale` o `failed`. Con la
    celda sin escribir desde la importación (`expected_version`), un `ADJUSTMENT` al saldo de antes
    registra justo el delta contrario, y su repetición se reconoce por el saldo que deja."""
    before, after = done.on_hand_after - done.quantity, done.on_hand_after
    key = f"{stock.IMPORT_KEY}{job.pk}:{row.row_number}{REVERT_KEY}"
    wanted = stock.Wanted(
        done.variant_id,
        done.warehouse_id,
        stock.Type.ADJUSTMENT,
        before,
        REVERT_REASON,
        key,
        expected_version=row.normalized.get("version", 0),  # sin ella (otro CRM): STALE
        reference_type=stock.Reference.IMPORT,
        reference_id=job.pk,
    )
    try:
        if before > QUANTITY_MAX:
            row.action, row.errors = Action.ERROR, [TOO_MANY]
        else:
            stock.move(ctx, wanted)
            return "reverted"
    except stock.Stale as error:
        message = f"La celda se movió después de la importación (dejó {after}, ahora hay "
        row.action, row.errors = Action.STALE, [f"{message}{error.on_hand}): no se revierte."]
    except tuple(REFUSED) as error:
        row.action, row.errors = Action.ERROR, [REVERT_REFUSED[type(error)]]
    row.save(using=require_scope(ctx), update_fields=["action", "errors"])
    return "stale" if row.action == Action.STALE else "failed"


def revert(ctx: TenantContext, *, import_id: UUID) -> DataImport:
    """Revierte una importación aplicada y la audita. Una revertida se devuelve tal cual.
    `DataImport.DoesNotExist` como `apply`; `NotApplied` si no está aplicada."""
    alias = require_scope(ctx)
    found = DataImport.objects.using(alias).select_for_update().filter(kind=Kind.STOCK)
    job: DataImport = found.get(pk=import_id)
    if job.status == DataImport.Status.REVERTED:
        return job  # ya revertida: revertir otra vez no cambia nada
    if job.status != DataImport.Status.APPLIED:
        raise NotApplied(job.status)
    moved = DataImportRow.objects.using(alias).filter(
        data_import=job, action__in=[Action.CREATE, Action.UPDATE], target_entity_type=MOVEMENT
    )
    rows = {row.target_entity_id: row for row in moved}
    movements = InventoryMovement.objects.using(alias).filter(pk__in=list(rows))
    result = dict.fromkeys(("reverted", "stale", "failed"), 0)
    for done in sorted(movements, key=lambda m: (m.variant_id, m.warehouse_id)):  # F3-09
        result[_revert_row(ctx, job, rows[done.pk], done)] += 1
    job.status, job.summary = DataImport.Status.REVERTED, {**job.summary, "revert": result}
    job.save(using=alias, update_fields=["status", "summary"])
    record(ctx, "inventory.import_reverted", Entity("data_import", job.pk), metadata=result)
    return job
