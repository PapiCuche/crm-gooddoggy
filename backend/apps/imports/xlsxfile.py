"""Leer los XLSX de las importaciones (F3-23, ADR-028 §5, ADR-012 §4.3). Puro: no toca la BD.

Da la misma `csvfile.Sheet` que el CSV (columnas por alias, filas vacías fuera, tope de filas y
de columnas ignoradas): previsualizar, aplicar y el informe no cambian. La línea es la fila de
Excel. Los topes van antes de que openpyxl (fijada) lea nada, con los metadatos del ZIP: partes,
bytes descomprimidos en total y razón de compresión de cada parte; sin cifrado y solo `stored` o
`deflate`. `zipfile` no descomprime más de lo que declara cada parte, así que el total acota la
memoria. Es un XLSX por su firma: un ZIP cuyo `[Content_Types].xml` declara un solo libro, del
tipo `.xlsx` (no `.xlsm` ni plantillas); la extensión y el `Content-Type` no cuentan.

openpyxl lee el XML con defusedxml (sin entidades ni DTD externas): sin ella, el módulo no carga.
Solo se lee la primera hoja declarada, con el parser de filas de openpyxl y no con su hoja de
solo lectura, que rellena las filas y columnas que faltan (una celda en `XFD1048576` serían miles
de millones). Una fórmula en cualquier celda rechaza el archivo: ni se evalúa ni se usa el valor
que Excel guardó. Lo que Excel convierte vuelve a texto: un número entero (`5.0`) en cifras
(«5»), otro tal cual («5.5»), una fecha como fecha; la validación de cada importación decide.
"""

import io
import logging
import zipfile
from collections.abc import Iterable, Mapping
from typing import Any

from openpyxl.packaging.manifest import Manifest
from openpyxl.reader.excel import ExcelReader
from openpyxl.styles.stylesheet import apply_stylesheet
from openpyxl.utils import get_column_letter
from openpyxl.worksheet._reader import WorkSheetParser
from openpyxl.xml import DEFUSEDXML, LXML
from openpyxl.xml.constants import ARC_CONTENT_TYPES, XLSM, XLSX, XLTM, XLTX
from openpyxl.xml.functions import fromstring

from apps.imports.csvfile import MAX_BYTES, MAX_ROWS, Sheet, Unreadable, columns, ignored

if LXML or not DEFUSEDXML:  # openpyxl elige el parser al importarse
    raise ImportError("openpyxl debe leer el XML con defusedxml y sin lxml (ADR-012 §4.3).")

logger = logging.getLogger(__name__)

MAX_PARTS, MAX_UNPACKED, MAX_RATIO = 100, 10 * MAX_BYTES, 100
MAX_CELLS, MAX_LINE = 100_000, 1_048_576  # celdas leídas (una fila vacía cuenta una); la de Excel
ZIP = b"PK\x03\x04"
METHODS = (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
WORKBOOKS = (XLSX, XLSM, XLTX, XLTM)
DAMAGED = "No pudimos abrir el Excel: está dañado. Ábrelo y guárdalo otra vez como .xlsx."
NOT_XLSX = "No es un Excel .xlsx: guárdalo como «Libro de Excel (.xlsx)» o como «CSV UTF-8»."


def is_zip(data: bytes) -> bool:
    """Empieza como un ZIP: lo lee este módulo (que comprueba si es un XLSX), no el de CSV."""
    return data.startswith(ZIP)


def _parts(archive: zipfile.ZipFile) -> None:
    parts = archive.infolist()
    if len(parts) > MAX_PARTS:
        raise Unreadable(f"El Excel tiene más de {MAX_PARTS} partes: deja solo la hoja de datos.")
    if len({part.filename for part in parts}) < len(parts):
        raise Unreadable(DAMAGED)
    for part in parts:
        if part.flag_bits & 0x1 or part.compress_type not in METHODS:
            raise Unreadable(DAMAGED)
        if part.file_size > MAX_RATIO * max(part.compress_size, 1):
            raise Unreadable("Una parte del Excel se descomprime demasiado: no parece de Excel.")
    if sum(part.file_size for part in parts) > MAX_UNPACKED:
        mb = MAX_UNPACKED // MAX_BYTES
        raise Unreadable(f"El Excel pasa de {mb} MB descomprimido: deja solo la hoja de datos.")


def _signature(archive: zipfile.ZipFile) -> None:
    if ARC_CONTENT_TYPES not in archive.namelist():
        raise Unreadable(NOT_XLSX)
    manifest = Manifest.from_tree(fromstring(archive.read(ARC_CONTENT_TYPES)))
    declared = [*manifest.Override, *manifest.Default]
    if [kind.ContentType for kind in declared if kind.ContentType in WORKBOOKS] != [XLSX]:
        raise Unreadable(NOT_XLSX)


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))  # 5.0 → «5»
    return str(value)


def _first_sheet(data: bytes) -> tuple[ExcelReader, str]:
    reader = ExcelReader(io.BytesIO(data), read_only=True, keep_links=False)
    reader.read_manifest()
    reader.read_strings()
    reader.read_workbook()
    apply_stylesheet(reader.archive, reader.wb)  # los formatos que dicen qué número es fecha
    first = reader.parser.sheets[0] if reader.parser.sheets else None
    if first is None:
        raise Unreadable(DAMAGED)
    rel = reader.parser.rels[first.id]
    if not rel.Type.endswith("/worksheet"):
        raise Unreadable("La primera hoja del Excel no es de celdas: pon los datos en ella.")
    return reader, rel.target


def _read(data: bytes, aliases: Mapping[str, Iterable[str]]) -> Sheet:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        _parts(archive)
        _signature(archive)
    reader, path = _first_sheet(data)
    wb = reader.wb
    header: list[str] | None = None
    found: dict[int, str] = {}
    rows: list[tuple[int, dict[str, str]]] = []
    budget, last = MAX_CELLS, 0
    with reader.archive.open(path) as source:
        dates = {"date_formats": wb._date_formats, "timedelta_formats": wb._timedelta_formats}
        strings = reader.shared_strings
        parser = WorkSheetParser(source, strings, data_only=False, epoch=wb.epoch, **dates)
        for line, cells in parser.parse():
            budget -= max(len(cells), 1)
            if budget < 0:
                raise Unreadable(f"La hoja pasa de {MAX_CELLS} celdas: deja solo la plantilla.")
            if not last < line <= MAX_LINE:  # la línea es la clave de cada fila: única
                raise Unreadable(DAMAGED)
            last, texts = line, {}
            for cell in cells:
                if cell["data_type"] == "f":
                    where = f"{get_column_letter(cell['column'])}{line}"
                    raise Unreadable(f"La celda {where} tiene una fórmula: pega solo los valores.")
                if cell["column"] in texts:
                    raise Unreadable(DAMAGED)
                texts[cell["column"]] = _text(cell["value"])
            if not any(text.strip() for text in texts.values()):
                continue
            if header is None:
                header = [texts.get(column, "") for column in range(1, max(texts) + 1)]
                found = columns(header, aliases)
                continue
            if len(rows) == MAX_ROWS:
                raise Unreadable(f"Más de {MAX_ROWS} filas: divídelo en varios archivos.")
            rows.append((line, {column: texts.get(i + 1, "") for i, column in found.items()}))
    if header is None:
        raise Unreadable("El archivo está vacío.")
    return Sheet("xlsx", "", ignored(header, found), rows)


def read(data: bytes, aliases: Mapping[str, Iterable[str]]) -> Sheet:
    """Las filas con texto de la primera hoja, con las columnas de `aliases` (todas obligatorias).
    `csvfile.Unreadable` si el archivo entero no se puede leer, con el motivo para quien lo subió.
    """
    if len(data) > MAX_BYTES:
        raise Unreadable(f"El archivo pasa de {MAX_BYTES // 1024} KB: divídelo.")
    try:
        return _read(data, aliases)
    except Unreadable:
        raise
    except Exception as error:  # un archivo hostil hace fallar a openpyxl de mil maneras
        logger.info("xlsx_unreadable", extra={"error": type(error).__name__})
        raise Unreadable(DAMAGED) from None
