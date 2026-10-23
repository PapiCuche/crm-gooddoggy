"""Importar stock por CSV (F3-20, E04-07, ADR-028 §5): cantidades absolutas por SKU y almacén.

Previsualizar no cambia el stock: lee el archivo (`apps.imports.csvfile`), valida cada fila
contra lo que hay y guarda la importación con sus filas y el `on_hand` que vio en cada celda.
Aplicar (F3-21) calculará el ajuste contra el `on_hand` de ese momento, con la celda bloqueada.

No comprueba permisos: los declara la API (`inventory.adjust`), la única que lo importa. Ningún
almacén tiene hoy otra autoridad que el CRM: cuando exista la marca de almacén del conector
(ADR-028 §3), su work item rechaza aquí, al validar, las filas de esos almacenes (§5).
"""

import hashlib
from collections import defaultdict
from typing import Any

from apps.audit.services import Entity, record
from apps.catalog.selectors import live_variants
from apps.imports import csvfile
from apps.imports.models import DataImport, DataImportRow
from apps.inventory.models import InventoryLevel, Warehouse
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
    sheet = csvfile.read(data, COLUMNS)
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
