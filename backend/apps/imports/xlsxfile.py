"""Leer los XLSX de las importaciones (F3-23, ADR-028 §5, ADR-012 §4.3). Puro: no toca la BD.

Da la misma `csvfile.Sheet` que el CSV (columnas por alias, filas vacías fuera, tope de filas y
de columnas ignoradas): previsualizar, aplicar y el informe no cambian. La línea es la fila de
Excel. Los topes van antes de que openpyxl (fijada) lea nada, con los metadatos del ZIP: partes,
bytes descomprimidos en total y razón de compresión de cada parte; sin cifrado y solo `stored` o
`deflate`. Las partes que openpyxl lee enteras como un árbol XML (tipos, libro, relaciones y
estilos) tienen además su propio tope: un `styles.xml` honrado de 9 MiB ya cuesta cientos de MiB.
Esos tamaños los declara quien sube el archivo, y `ZipFile.read()` descomprime el flujo entero
antes de recortarlo a lo declarado (una parte que dice 9 MiB puede inflarse a 1 GiB). Por eso ni
la firma ni openpyxl leen el ZIP subido: cada parte se descomprime con zlib hasta un byte más de
lo que declara, y tiene que dar justo eso, acabar el flujo y cuadrar su CRC; va a un ZIP nuevo
sin comprimir, que es el único que se lee. Es un XLSX por su firma: un ZIP cuyo
`[Content_Types].xml` declara un solo libro, del tipo `.xlsx` (no `.xlsm` ni plantillas); la
extensión y el `Content-Type` no cuentan.

openpyxl lee el XML con defusedxml (sin entidades ni DTD externas): sin ella, el módulo no carga.
Solo se lee la primera hoja declarada, con el parser de filas de openpyxl y no con su hoja de
solo lectura, que rellena las filas y columnas que faltan (una celda en `XFD1048576` serían miles
de millones). openpyxl arma entero cada elemento que lee (una fila, `mergeCells`, la tabla de
textos compartidos) antes de que cuente ningún tope (F3-23b): la hoja y los textos se recorren
aquí, contando sus elementos mientras llega el XML; a openpyxl solo le llega cada fila ya
contada (de cada texto se toman sus `<t>`) y lo demás (formatos, validaciones, enlaces) se tira
al cerrarse, sin pasar por él.
El parser arma los atributos antes de dar el evento y guarda cada nombre distinto: también se
cuentan los bytes sin un elemento (`MAX_QUIET`) y los nombres (`MAX_NAMES`). Las partes que
openpyxl lee enteras se recorren antes igual, con `MAX_WHOLE_NODES` elementos entre todas.
Una fórmula en cualquier celda rechaza el archivo: ni se evalúa ni se usa el valor que Excel
guardó. Lo que Excel convierte vuelve a texto: un número entero (`5.0`) en cifras («5»), otro
tal cual («5.5»), una fecha como fecha; la validación de cada importación decide.
"""

import io
import logging
import struct
import zipfile
import zlib
from collections.abc import Generator, Iterable, Mapping
from typing import IO, Any
from xml.etree.ElementTree import Element

from openpyxl.packaging.manifest import Manifest
from openpyxl.reader.excel import ExcelReader
from openpyxl.styles.stylesheet import apply_stylesheet
from openpyxl.utils import get_column_letter
from openpyxl.worksheet._reader import WorkSheetParser
from openpyxl.xml import DEFUSEDXML, LXML
from openpyxl.xml.constants import (
    ARC_CONTENT_TYPES,
    ARC_STYLE,
    SHARED_STRINGS,
    SHEET_MAIN_NS,
    XLSM,
    XLSX,
    XLTM,
    XLTX,
)
from openpyxl.xml.functions import fromstring, iterparse

from apps.imports.csvfile import MAX_BYTES, MAX_ROWS, Sheet, Unreadable, columns, ignored

if LXML or not DEFUSEDXML:  # openpyxl elige el parser al importarse
    raise ImportError("openpyxl debe leer el XML con defusedxml y sin lxml (ADR-012 §4.3).")

logger = logging.getLogger(__name__)

MAX_PARTS, MAX_UNPACKED, MAX_RATIO = 100, 10 * MAX_BYTES, 100
MAX_CELLS, MAX_LINE = 100_000, 1_048_576  # celdas leídas (una fila vacía cuenta una); la de Excel
MAX_WHOLE = MAX_BYTES  # cada parte que openpyxl lee entera: tipos, libro, relaciones, estilos
# Elementos XML que openpyxl arma: en la hoja o en los textos compartidos (una celda con texto
# en línea son 3, un texto con formato 3 o más) y en cada fila o texto (16 384 columnas en Excel).
MAX_NODES, MAX_UNIT = 3 * MAX_CELLS, 16_384
MAX_QUIET = 256 * 1024  # bytes seguidos sin que empiece o acabe un elemento (un texto de Excel:
# 32 767 caracteres)
MAX_NAMES = 1024  # nombres de etiqueta y de atributo distintos: el parser los guarda todos
MAX_WHOLE_NODES = 32_768  # entre las partes que openpyxl lee enteras (1 MiB de `<xf/>`: 16 000)
ROW, STRING, RUN, TEXT = (f"{{{SHEET_MAIN_NS}}}{tag}" for tag in ("row", "si", "r", "t"))
ZIP = b"PK\x03\x04"
METHODS = (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
WORKBOOKS = (XLSX, XLSM, XLTX, XLTM)
DAMAGED = "No pudimos abrir el Excel: está dañado. Ábrelo y guárdalo otra vez como .xlsx."
NOT_XLSX = "No es un Excel .xlsx: guárdalo como «Libro de Excel (.xlsx)» o como «CSV UTF-8»."
TOO_BIG_PART = f"Una parte interna del Excel pasa de {MAX_WHOLE // 1024} KB: no parece de Excel."
TOO_FULL_PART = "Una parte interna del Excel tiene demasiados datos: no parece de Excel."
TOO_FULL = "La hoja tiene demasiados datos (celdas, formatos o enlaces): deja solo la plantilla."
TOO_MANY_TEXTS = "El Excel tiene demasiados textos: deja solo la hoja de datos."


def is_zip(data: bytes) -> bool:
    """Empieza como un ZIP: lo lee este módulo (que comprueba si es un XLSX), no el de CSV."""
    return data.startswith(ZIP)


def _whole(name: str) -> bool:
    """Una parte que openpyxl lee entera, como un árbol XML (el libro, aparte, tras la firma)."""
    return name in (ARC_CONTENT_TYPES, ARC_STYLE) or name.endswith(".rels")


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
        if _whole(part.filename) and part.file_size > MAX_WHOLE:  # el libro, tras la firma
            raise Unreadable(TOO_BIG_PART)
    if sum(part.file_size for part in parts) > MAX_UNPACKED:
        mb = MAX_UNPACKED // MAX_BYTES
        raise Unreadable(f"El Excel pasa de {mb} MB descomprimido: deja solo la hoja de datos.")


def _content(data: bytes, part: zipfile.ZipInfo) -> bytes:
    """Lo que guarda la parte: hasta un byte más de lo que declara, nunca el flujo entero."""
    at = part.header_offset
    if at < 0 or data[at : at + 4] != ZIP:  # su cabecera local, dentro del archivo
        raise Unreadable(DAMAGED)
    name, extra = struct.unpack_from("<HH", data, at + 26)
    begin = at + 30 + name + extra
    raw = data[begin : begin + part.compress_size]
    if part.compress_type == zipfile.ZIP_STORED:
        content, ended = raw, True
    else:
        inflate = zlib.decompressobj(-zlib.MAX_WBITS)
        content = inflate.decompress(raw, part.file_size + 1)
        ended = inflate.eof
    whole = len(raw) == part.compress_size  # no acaba fuera del archivo
    if not (ended and whole) or len(content) != part.file_size or zlib.crc32(content) != part.CRC:
        raise Unreadable(DAMAGED)
    return content


def _unpacked(data: bytes) -> bytes:
    """El ZIP subido, con los topes de sus metadatos y cada parte comprobada (`_content`), en un ZIP
    sin comprimir: así los topes de `_parts`, hechos con lo declarado, valen para lo real."""
    out = io.BytesIO()
    with (
        zipfile.ZipFile(io.BytesIO(data)) as archive,
        zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as clean,
    ):
        _parts(archive)
        for part in archive.infolist():
            clean.writestr(part.filename, _content(data, part))
    return out.getvalue()


def _signature(archive: zipfile.ZipFile) -> str:
    """La parte del libro, si es un XLSX."""
    if ARC_CONTENT_TYPES not in archive.namelist():
        raise Unreadable(NOT_XLSX)
    manifest = Manifest.from_tree(fromstring(archive.read(ARC_CONTENT_TYPES)))
    kinds = (*manifest.Override, *manifest.Default)
    found = [kind for kind in kinds if kind.ContentType in WORKBOOKS]
    if [kind.ContentType for kind in found] != [XLSX]:
        raise Unreadable(NOT_XLSX)
    return str(getattr(found[0], "PartName", "/xl/workbook.xml"))[1:]  # como openpyxl


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))  # 5.0 → «5»
    return str(value)


class _Paced:
    """La parte, para `iterparse`, que no da más de `MAX_QUIET` bytes sin un elemento: los
    atributos de una etiqueta se arman antes de su evento (270 000 costaban +140 MiB)."""

    def __init__(self, source: IO[bytes], refusal: str) -> None:
        self.source, self.refusal, self.quiet = source, refusal, 0

    def read(self, size: int) -> bytes:
        if self.quiet > MAX_QUIET:
            raise Unreadable(self.refusal)
        chunk = self.source.read(size)
        self.quiet += len(chunk)
        return chunk


def _units(source: IO[bytes], tag: str, most: int, refusal: str) -> Generator[Element, None, int]:
    """Cada elemento `tag` de la parte (una fila, un texto), entero; el resto se tira al cerrarse.
    openpyxl arma entero lo que lee (una fila de 700 000 celdas, `mergeCells`), así que los topes
    de elementos van mientras se lee el XML: `most` en la parte y `MAX_UNIT` en cada `tag`."""
    path: list[Element] = []
    names: set[str] = set()
    nodes, inside, depth = 0, 0, 0  # `inside`: elementos del `tag` abierto (0, ninguno)
    paced = _Paced(source, refusal)
    for event, element in iterparse(paced, events=("start", "end")):
        paced.quiet = 0
        if event == "start":
            nodes += 1
            if inside:
                inside += 1
            elif element.tag == tag:
                inside, depth = 1, len(path)
            names.add(element.tag)
            names.update(element.attrib)
            if nodes > most or inside > MAX_UNIT or len(names) > MAX_NAMES:
                raise Unreadable(refusal)
            path.append(element)
            continue
        path.pop()
        if inside and len(path) > depth:
            continue  # dentro del `tag`: lo lee openpyxl
        if inside:
            inside = 0
            yield element
        if path:
            path[-1].remove(element)  # su único hijo: los anteriores ya se quitaron
    return nodes


def _walk(contents: Iterable[bytes], left: int) -> int:
    """Recorre antes las partes que openpyxl leerá enteras, con los topes de `_units` (150 000
    atributos en 1 MiB pasaban de 50 MiB) y `left` elementos entre todas; da los que quedan."""
    for content in contents:
        walker = _units(io.BytesIO(content), "", left, TOO_FULL_PART)
        try:
            next(walker)  # ningún elemento se llama "": la recorre entera
        except StopIteration as end:
            left -= end.value
    return left


def _strings(reader: ExcelReader) -> list[str]:
    """Los textos compartidos, con sus topes: son de todas las hojas del libro, así que caben
    tantos como celdas (`MAX_CELLS`). El texto es el de `read_string_table` de openpyxl (su `<t>`
    y el de cada tramo con formato, sin la fonética), sin armar sus objetos (4 µs por texto)."""
    part = reader.package.find(SHARED_STRINGS)
    if part is None:
        return []
    strings: list[str] = []
    with reader.archive.open(part.PartName[1:]) as source:
        for text in _units(source, STRING, MAX_NODES, TOO_MANY_TEXTS):
            if len(strings) == MAX_CELLS:
                raise Unreadable(TOO_MANY_TEXTS)
            runs = (run.findtext(TEXT, "") for run in text.iterfind(RUN))
            plain = [t.text or "" for t in text.iterfind(TEXT)][-1:]  # el último, como openpyxl
            strings.append("".join((*plain, *runs)).replace("x005F_", ""))
    return strings


def _first_sheet(data: bytes) -> tuple[ExcelReader, str]:
    reader = ExcelReader(io.BytesIO(data), read_only=True, keep_links=False)
    reader.read_manifest()
    reader.shared_strings = _strings(reader)
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
    data = _unpacked(data)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        left = _walk(
            (archive.read(name) for name in archive.namelist() if _whole(name)), MAX_WHOLE_NODES
        )
        book = _signature(archive)
        if archive.getinfo(book).file_size > MAX_WHOLE:
            raise Unreadable(TOO_BIG_PART)
        _walk([archive.read(book)], left)
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
        for line, cells in map(parser.parse_row, _units(source, ROW, MAX_NODES, TOO_FULL)):
            parser.row_dimensions.clear()  # openpyxl guarda los atributos de la fila: no se usan
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
