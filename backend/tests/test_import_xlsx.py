"""F3-23: el lector XLSX de las importaciones (`apps.imports.xlsxfile`). Sin BD.

Sin archivos hechos en Excel a mano: los libros los escribe openpyxl y los hostiles se arman
parte a parte, con el XML que escribiría Excel."""

import datetime
import io
import struct
import tracemalloc
import zipfile
import zlib
from collections.abc import Callable
from typing import Any

import openpyxl.xml
import pytest
from openpyxl import Workbook
from openpyxl.cell.text import Text
from openpyxl.worksheet._reader import WorkSheetParser
from openpyxl.xml.functions import fromstring

from apps.imports import csvfile, xlsxfile
from apps.imports.csvfile import Unreadable

pytestmark = pytest.mark.filterwarnings("ignore::UserWarning")  # «sin estilos», de openpyxl
ALIASES = {"sku": ("sku",), "almacen": ("almacen", "warehouse"), "cantidad": ("cantidad",)}
MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
RELS = "http://schemas.openxmlformats.org/package/2006/relationships"
TYPES = '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
OFFICE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"
XLSM = "application/vnd.ms-excel.sheet.macroEnabled.main+xml"
HEADER = '<row r="1"><c t="inlineStr"><is><t>sku</t></is></c><c t="inlineStr"><is><t>almacen</t>'
HEADER += '</is></c><c t="inlineStr"><is><t>cantidad</t></is></c></row>'


def book(*rows: list[Any], other: list[Any] | None = None) -> bytes:
    """Un libro de openpyxl; `other`, una segunda hoja con una fila."""
    wb = Workbook()
    for row in rows:
        wb.active.append(row)
    if other is not None:
        wb.create_sheet("otra").append(other)
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def packed(parts: dict[str, bytes | str], method: int = zipfile.ZIP_DEFLATED) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", method) as archive:
        for name, content in parts.items():
            archive.writestr(name, content)
    return out.getvalue()


def override(kind: str, part: str = "/xl/workbook.xml") -> str:
    return f'<Override PartName="{part}" ContentType="{kind}"/>'


FIRST = '<sheet name="datos" sheetId="1" r:id="rId1"/>'
LINK = f'<Relationship Id="rId1" Type="{OFFICE}/worksheet" Target="worksheets/sheet1.xml"/>'


def handmade(
    sheet: str = HEADER,
    *,
    kind: str = XLSX,
    types: str = "",
    order: str = FIRST,
    links: str = LINK,
    parts: dict[str, bytes | str] | None = None,
) -> bytes:
    """Un XLSX mínimo armado a mano: `sheet` es el XML de `sheetData`; `types`, otros tipos;
    `order`, las hojas del libro; `links`, sus relaciones; `parts`, otras partes o en lugar."""
    return packed(
        {
            "[Content_Types].xml": f"{TYPES}{override(kind)}{types}</Types>",
            "xl/workbook.xml": f'<workbook xmlns="{MAIN}" xmlns:r="{OFFICE}"><sheets>{order}'
            "</sheets></workbook>",
            "xl/_rels/workbook.xml.rels": f'<Relationships xmlns="{RELS}">{links}</Relationships>',
            "xl/worksheets/sheet1.xml": f'<worksheet xmlns="{MAIN}"><sheetData>{sheet}</sheetData>'
            "</worksheet>",
            **(parts or {}),
        }
    )


def refused(data: bytes) -> str:
    with pytest.raises(Unreadable) as error:
        xlsxfile.read(data, ALIASES)
    return str(error.value)


def test_a_book_reads_like_its_csv() -> None:
    """La cabecera tras filas vacías, tildes y mayúsculas, columnas de más, filas vacías fuera y
    las cortas completadas; la línea es la fila de Excel."""
    rows: list[list[Any]] = [
        [],
        ["SKU", "Almacén ", "cantidad", "notas"],
        ["A1", "ALM", "3", "x"],
        [None, " ", None],
        ["B2", "ALM"],
        ["C3", "ALM", "4"],
    ]
    csv = "\n".join(",".join(cell or "" for cell in row) for row in rows).encode()
    sheet, same = xlsxfile.read(book(*rows), ALIASES), csvfile.read(csv, ALIASES)
    assert (sheet.rows, sheet.ignored) == (same.rows, same.ignored)
    assert [line for line, _ in sheet.rows] == [3, 5, 6]
    assert (sheet.encoding, sheet.separator) == ("xlsx", "")


def test_what_excel_converts_comes_back_as_text() -> None:
    day = datetime.date(2026, 1, 2)
    data = book(["sku", "almacen", "cantidad"], [123, "ALM", 5.0], ["B", True, 5.5], ["C", 0, day])
    assert [cells for _, cells in xlsxfile.read(data, ALIASES).rows] == [
        {"sku": "123", "almacen": "ALM", "cantidad": "5"},
        {"sku": "B", "almacen": "TRUE", "cantidad": "5.5"},
        {"sku": "C", "almacen": "0", "cantidad": "2026-01-02 00:00:00"},  # la validación la rechaza
    ]
    excel = HEADER + '<row r="2"><c r="A2"><v>5.0</v></c><c r="B2"><v>1E3</v></c><c r="C2">'
    excel += "<v>-2.50</v></c></row>"
    assert xlsxfile.read(handmade(excel), ALIASES).rows == [
        (2, {"sku": "5", "almacen": "1000", "cantidad": "-2.5"})
    ]


def test_only_the_first_sheet_is_read() -> None:
    other = ["sku", "almacen", "cantidad"]
    assert refused(book([], other=other)) == "El archivo está vacío."
    assert xlsxfile.read(book(other, ["A", "B", 1], other=["x"]), ALIASES).rows == [
        (2, {"sku": "A", "almacen": "B", "cantidad": "1"})
    ]
    # La primera declarada en el libro, aunque su parte se llame `sheet2.xml`.
    links = (
        f'<Relationship Id="rId1" Type="{OFFICE}/worksheet" Target="worksheets/sheet1.xml"/>'
        f'<Relationship Id="rId2" Type="{OFFICE}/worksheet" Target="worksheets/sheet2.xml"/>'
    )
    order = '<sheet name="b" sheetId="2" r:id="rId2"/><sheet name="a" sheetId="1" r:id="rId1"/>'
    second = f'<worksheet xmlns="{MAIN}"><sheetData><row r="1"><c t="inlineStr"><is><t>nada'
    second += "</t></is></c></row></sheetData></worksheet>"
    flipped = handmade(order=order, links=links, parts={"xl/worksheets/sheet2.xml": second})
    assert "Falta la columna «sku»" in refused(flipped)
    chart = f'<Relationship Id="rId1" Type="{OFFICE}/chartsheet" Target="worksheets/sheet1.xml"/>'
    assert "no es de celdas" in refused(handmade(links=chart))


@pytest.mark.parametrize(
    "cell",
    [
        '<c r="C2"><f>1+1</f><v>2</v></c>',  # con el valor guardado: tampoco se usa
        '<c r="E2" t="str"><f>"x"</f><v>x</v></c>',  # en una columna ignorada
        '<c r="C2"><f t="shared" si="0"/></c>',
    ],
)
def test_a_formula_anywhere_refuses_the_file(cell: str) -> None:
    sheet = HEADER + f'<row r="2"><c r="A2" t="inlineStr"><is><t>A</t></is></c>{cell}</row>'
    where = cell.split('"')[1]
    assert refused(handmade(sheet)) == f"La celda {where} tiene una fórmula: pega solo los valores."
    assert "fórmula" in refused(book(["=SUM(1,2)", "almacen", "cantidad"]))  # en la cabecera


def test_the_zip_is_capped_before_openpyxl_reads_it(monkeypatch: pytest.MonkeyPatch) -> None:
    assert (xlsxfile.MAX_PARTS, xlsxfile.MAX_RATIO) == (100, 100)
    assert (xlsxfile.MAX_UNPACKED, xlsxfile.MAX_CELLS) == (10 * csvfile.MAX_BYTES, 100_000)
    bomb = handmade(parts={"xl/media/pad.bin": b"\x00" * 1_000_000})  # razón ≈ 1000, 1 MB en total
    assert "se descomprime demasiado" in refused(bomb)
    pad = zipfile.ZipFile(io.BytesIO(bomb)).getinfo("xl/media/pad.bin")
    monkeypatch.setattr(xlsxfile, "MAX_RATIO", -(-pad.file_size // pad.compress_size))
    assert xlsxfile.read(bomb, ALIASES).rows == []  # justo en el tope
    monkeypatch.setattr(xlsxfile, "MAX_RATIO", pad.file_size // pad.compress_size)
    assert "se descomprime demasiado" in refused(bomb)
    monkeypatch.undo()
    many = handmade(parts={f"xl/p{n}.xml": "" for n in range(96)})  # 4 + 96 = 100 partes
    assert xlsxfile.read(many, ALIASES).rows == []
    many = handmade(parts={f"xl/p{n}.xml": "" for n in range(97)})
    assert refused(many) == "El Excel tiene más de 100 partes: deja solo la hoja de datos."
    data = handmade()
    unpacked = sum(part.file_size for part in zipfile.ZipFile(io.BytesIO(data)).infolist())
    monkeypatch.setattr(xlsxfile, "MAX_UNPACKED", unpacked)
    assert xlsxfile.read(data, ALIASES).rows == []
    monkeypatch.setattr(xlsxfile, "MAX_UNPACKED", unpacked - 1)
    assert "MB descomprimido" in refused(data)
    assert "KB: divídelo" in refused(data + b"\x00" * csvfile.MAX_BYTES)


def plus(name: str, method: int = zipfile.ZIP_DEFLATED, *, locked: bool = False) -> bytes:
    """Un XLSX válido con una parte más al final, que openpyxl ni abriría; `locked`, con la marca
    de cifrado en el directorio central (`zipfile` no la escribe)."""
    out = io.BytesIO(handmade())
    with zipfile.ZipFile(out, "a") as archive:
        archive.writestr(name, "<x/>", method)
    data = bytearray(out.getvalue())
    if locked:
        data[data.rindex(b"PK\x01\x02") + 8] |= 0x1
    return bytes(data)


def declare(data: bytes, name: str, size: int, content: bytes) -> bytes:
    """`data` con la parte `name` declarando `size` bytes descomprimidos y el CRC de `content`, en
    su cabecera local y en el directorio central: lo que miente un archivo hostil."""
    raw, crc = bytearray(data), zlib.crc32(content)
    local = zipfile.ZipFile(io.BytesIO(data)).getinfo(name).header_offset
    struct.pack_into("<I", raw, local + 14, crc)
    struct.pack_into("<I", raw, local + 22, size)
    central = raw.index(b"PK\x01\x02")
    while raw[central + 46 : central + 46 + len(name)] != name.encode():
        central = raw.index(b"PK\x01\x02", central + 1)
    struct.pack_into("<I", raw, central + 16, crc)
    struct.pack_into("<I", raw, central + 24, size)
    return bytes(raw)


@pytest.mark.parametrize("name", ["[Content_Types].xml", "xl/workbook.xml"])
def test_a_part_that_inflates_past_what_it_declares_is_never_inflated(name: str) -> None:
    """Dice 64 KiB (razón 4, pasa los topes) con el CRC de esos 64 KiB, pero su flujo da 16 MiB:
    `ZipFile.read()` los descomprimiría todos antes de recortar. Se lee por trozos, hasta 64 KiB."""
    small = 65_536
    bomb = declare(handmade(parts={name: b" " * (16 << 20)}), name, small, b" " * small)
    tracemalloc.start()
    try:
        assert refused(bomb) == xlsxfile.DAMAGED  # 64 KiB de espacios no son XML
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert peak < 2 << 20, peak


def styles(count: int) -> str:
    """Estilos honrados: `count` formatos de celda distintos (razón de compresión < 100)."""
    xfs = "".join(
        f'<xf numFmtId="{n}" fontId="{n % 97}" fillId="{n % 13}" borderId="{n % 7}" xfId="0"/>'
        for n in range(count)
    )
    return f'<styleSheet xmlns="{MAIN}"><cellXfs count="{count}">{xfs}</cellXfs></styleSheet>'


def just_over(build: Callable[[int], str], cap: int) -> int:
    """El menor número de elementos con el que `build` pasa de `cap` bytes (cada uno ocupa 20+)."""
    low, high = 0, cap // 20
    while high - low > 1:
        low, high = (mid, high) if len(build(mid := (low + high) // 2)) <= cap else (low, mid)
    return high


def sheets(count: int) -> str:
    ids = range(2, count + 2)
    return FIRST + "".join(f'<sheet name="h{n}" sheetId="{n}" r:id="rId{n}"/>' for n in ids)


def links(count: int) -> str:
    target = f'Type="{OFFICE}/worksheet" Target="worksheets/sheet1.xml"'
    return LINK + "".join(f'<Relationship Id="rId{n + 2}" {target}/>' for n in range(count))


def test_the_parts_read_whole_have_their_own_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """Estilos, libro, relaciones y tipos los lee openpyxl enteros: 9 MiB honrados de `<xf/>` ya
    son cientos de MiB. La hoja y los textos compartidos se leen por trozos: solo el tope total."""
    assert xlsxfile.MAX_WHOLE == csvfile.MAX_BYTES
    real = handmade(order=sheets(300), parts={"xl/styles.xml": styles(3000)})  # unos 200 KB
    assert xlsxfile.read(real, ALIASES).rows == []
    cap = csvfile.MAX_BYTES
    for big in (
        handmade(parts={"xl/styles.xml": styles(just_over(styles, cap))}),
        handmade(order=sheets(just_over(sheets, cap))),
        handmade(links=links(just_over(links, cap))),
    ):
        assert refused(big) == xlsxfile.TOO_BIG_PART
    for part, data in (
        ("xl/styles.xml", handmade(parts={"xl/styles.xml": styles(10)})),
        ("xl/workbook.xml", handmade(order=sheets(10))),
        ("xl/_rels/workbook.xml.rels", handmade(links=links(10))),
        ("[Content_Types].xml", handmade(types=override("text/xml", "/x.xml") * 10)),
    ):
        sizes = {i.filename: i.file_size for i in zipfile.ZipFile(io.BytesIO(data)).infolist()}
        size = sizes.pop(part)
        assert size > max(sizes.values())  # la más grande de todas
        monkeypatch.setattr(xlsxfile, "MAX_WHOLE", size)
        assert xlsxfile.read(data, ALIASES).rows == []
        monkeypatch.setattr(xlsxfile, "MAX_WHOLE", size - 1)
        assert refused(data) == xlsxfile.TOO_BIG_PART, part
    monkeypatch.setattr(xlsxfile, "MAX_WHOLE", 1024)  # la hoja, mucho más grande: sin ese tope
    rows = "".join(f'<row r="{n}"><c r="C{n}"><v>{n}</v></c></row>' for n in range(2, 200))
    strings = f'<sst xmlns="{MAIN}">' + "<si><t>s</t></si>" * 300 + "</sst>"
    padded = handmade(HEADER + rows, parts={"xl/sharedStrings.xml": strings})
    assert len(xlsxfile.read(padded, ALIASES).rows) == 198


def restream(stream: bytes, size: int, content: bytes) -> bytes:
    """Un XLSX válido cuya última parte (`xl/extra.xml`, que openpyxl no lee) es el flujo deflate
    `stream`, declarando `size` bytes y el CRC de `content`."""
    data = bytearray(handmade(parts={"xl/extra.xml": "<x/>"}))
    last = zipfile.ZipFile(io.BytesIO(bytes(data))).getinfo("xl/extra.xml")
    name, extra = struct.unpack_from("<HH", data, last.header_offset + 26)
    begin = last.header_offset + 30 + name + extra
    data[begin : begin + last.compress_size] = stream
    fields = (zlib.crc32(content), len(stream), size)
    struct.pack_into("<III", data, last.header_offset + 14, *fields)
    struct.pack_into("<III", data, data.rindex(b"PK\x01\x02") + 16, *fields)
    end = data.rindex(b"PK\x05\x06") + 16
    struct.pack_into("<I", data, end, struct.unpack_from("<I", data, end)[0] + len(stream) - 6)
    return bytes(data)


def deflate(content: bytes, end: bool = True) -> bytes:
    pack = zlib.compressobj(9, zlib.DEFLATED, -zlib.MAX_WBITS)
    return pack.compress(content) + pack.flush(zlib.Z_FINISH if end else zlib.Z_SYNC_FLUSH)


def test_a_stream_must_end_exactly_where_it_declares_with_its_crc() -> None:
    """`zipfile` corta en lo declarado un flujo más largo (con el CRC de ese trozo, lo acepta) y no
    mira si el flujo acaba: aquí, eso daña el archivo, como uno más corto o un CRC que no cuadra."""
    good = b"<x/>"
    assert len(deflate(good)) == 6  # lo que ocupaba la parte: `restream` mueve el directorio
    assert xlsxfile.read(restream(deflate(good), 4, good), ALIASES).rows == []
    for stream, size, crc in (
        (deflate(good + b" " * 1000), 4, good),  # sigue tras lo declarado
        (deflate(good, end=False), 4, good),  # no acaba
        (deflate(good), 5, good),  # acaba antes
        (deflate(good), 4, b"<y/>"),
    ):
        assert refused(restream(stream, size, crc)) == xlsxfile.DAMAGED


def test_a_part_that_runs_past_the_end_of_the_file_is_damaged() -> None:
    data = bytearray(handmade(parts={"xl/extra.xml": "<x/>"}))
    unsigned = bytes(data).replace(b"PK\x03\x04", b"PK\x00\x00")  # sin firma en la cabecera local
    struct.pack_into("<I", data, data.rindex(b"PK\x01\x02") + 20, 15 << 20)  # su tamaño comprimido
    assert refused(bytes(data)) == refused(unsigned) == xlsxfile.DAMAGED


def test_a_part_that_is_not_plain_zip_is_refused() -> None:
    assert xlsxfile.read(plus("xl/extra.xml", zipfile.ZIP_STORED), ALIASES).rows == []
    for method in (zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA):
        assert refused(plus("xl/extra.xml", method)) == xlsxfile.DAMAGED
    assert refused(plus("xl/extra.xml", locked=True)) == xlsxfile.DAMAGED  # cifrada
    twice = io.BytesIO(handmade(parts={"xl/extra.xml": "<x/>"}))
    with pytest.warns(UserWarning, match="Duplicate name"), zipfile.ZipFile(twice, "a") as archive:
        archive.writestr("xl/extra.xml", "<y/>")  # ¿cuál de las dos vale?
    assert refused(twice.getvalue()) == xlsxfile.DAMAGED


@pytest.mark.parametrize(
    "data",
    [
        b"PK\x03\x04 no es un zip",
        handmade(sheet="<row><c>"),  # XML roto
        handmade(parts={"xl/workbook.xml": "<workbook/>"}),  # sin hojas
        handmade(links=""),  # la hoja sin su relación
        handmade(links=f'<Relationship Id="rId1" Type="{OFFICE}/worksheet" Target="nada.xml"/>'),
        handmade(HEADER + '<row r="3"/><row r="2"/>'),  # filas fuera de orden
        handmade(HEADER + '<row r="1"/>'),  # la misma fila otra vez
        handmade('<row r="0"/>'),
        handmade('<row r="1048577"/>'),
        handmade(HEADER.replace("<c ", '<c r="A1" ', 2)),  # la misma celda dos veces
        handmade(HEADER + '<row r="2"><c r="A2" t="s"><v>7</v></c></row>'),  # texto que no hay
        handmade(HEADER + '<row r="2"><c r="A2"><v>inf</v></c></row>'),
    ],
)
def test_a_damaged_book_says_so(data: bytes) -> None:
    assert refused(data) == xlsxfile.DAMAGED


def test_the_last_excel_row_is_read_without_filling_the_gap() -> None:
    far = '<row r="1048576"><c r="XFD1048576" t="inlineStr"><is><t>x</t></is></c></row>'
    blank = {"sku": "", "almacen": "", "cantidad": ""}
    assert xlsxfile.read(handmade(HEADER + far), ALIASES).rows == [(1_048_576, blank)]


@pytest.mark.parametrize(
    ("data", "says"),
    [
        (packed({"a.txt": "hola"}), xlsxfile.NOT_XLSX),
        (packed({"mimetype": "application/vnd.oasis.opendocument.spreadsheet"}), "No es un Excel"),
        (handmade(kind=XLSM), xlsxfile.NOT_XLSX),  # con macros
        (handmade(types=override(XLSM, "/xl/b.xml")), xlsxfile.NOT_XLSX),  # dos libros
    ],
)
def test_the_signature_is_an_xlsx_workbook_not_the_name(data: bytes, says: str) -> None:
    assert xlsxfile.is_zip(data) and refused(data).startswith(says)
    assert not xlsxfile.is_zip(b"sku,almacen,cantidad\n")


ENTITIES = '<!DOCTYPE x [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">]>'
EXTERNAL = '<!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]>'


@pytest.mark.parametrize("dtd", [ENTITIES, EXTERNAL])
def test_xml_entities_are_refused_in_every_part(dtd: str) -> None:
    """defusedxml (que openpyxl usa si está): ni entidades que se expanden ni externas."""
    assert openpyxl.xml.DEFUSEDXML and not openpyxl.xml.LXML
    use = "&b;" if dtd == ENTITIES else "&e;"
    sheet = f'{dtd}<worksheet xmlns="{MAIN}"><sheetData><row r="1"><c t="inlineStr"><is><t>{use}'
    sheet += "</t></is></c></row></sheetData></worksheet>"
    types = f"{dtd}{TYPES}{override(XLSX)}</Types>"
    for part, xml in (("xl/worksheets/sheet1.xml", sheet), ("[Content_Types].xml", types)):
        assert refused(handmade(parts={part: xml})) == xlsxfile.DAMAGED


def test_rows_and_cells_are_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    header = ["sku", "almacen", "cantidad"]
    rows: list[list[Any]] = [header, *[["A", "B", 1]] * csvfile.MAX_ROWS]
    assert len(xlsxfile.read(book(*rows), ALIASES).rows) == csvfile.MAX_ROWS
    assert refused(book(*rows, ["A", "B", 1])) == "Más de 1000 filas: divídelo en varios archivos."
    four = handmade(HEADER + '<row r="2"/>')  # 3 celdas y una fila vacía, que cuenta una
    monkeypatch.setattr(xlsxfile, "MAX_CELLS", 4)
    assert xlsxfile.read(four, ALIASES).rows == []
    monkeypatch.setattr(xlsxfile, "MAX_CELLS", 3)
    assert refused(four) == "La hoja pasa de 3 celdas: deja solo la plantilla."


def test_a_row_is_capped_before_openpyxl_reads_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """openpyxl arma una fila entera antes de leerla: una de 700 000 celdas costaba +340 MiB y
    1,6 s antes de que contara el presupuesto. Sus elementos se cuentan mientras llega el XML."""
    assert xlsxfile.MAX_UNIT == 16_384  # las columnas de Excel
    real, parsed = WorkSheetParser.parse_row, []

    def parse_row(parser: Any, row: Any) -> Any:
        parsed.append(len(row))
        return real(parser, row)

    monkeypatch.setattr(WorkSheetParser, "parse_row", parse_row)
    monkeypatch.setattr(xlsxfile, "MAX_UNIT", 10)  # la cabecera: la fila y 3 × (c, is, t)
    wide = [handmade(HEADER + '<row r="2">' + "<c><v>1</v></c>" * n + "</row>") for n in (4, 5)]
    assert len(xlsxfile.read(wide[0], ALIASES).rows) == 1  # la fila y 4 × (c, v): 9
    parsed.clear()
    assert refused(wide[1]) == xlsxfile.TOO_FULL and parsed == [3]  # 11: ni se arma
    monkeypatch.setattr(xlsxfile, "MAX_UNIT", 9)
    parsed.clear()
    assert refused(handmade()) == xlsxfile.TOO_FULL and parsed == []


NOT_ROWS = (
    '<cols><col min="1" max="2" width="9"/></cols><sheetData>{}</sheetData>'
    '<mergeCells><mergeCell ref="A2:B2"/></mergeCells>'
    '<conditionalFormatting sqref="A2"><cfRule type="cellIs" priority="1" operator="equal">'
    "<formula>1</formula></cfRule></conditionalFormatting>"
    '<dataValidations><dataValidation type="whole" sqref="A2"><formula1>1</formula1>'
    '</dataValidation></dataValidations><hyperlinks><hyperlink ref="A2" location="h!A1"/>'
    "</hyperlinks>"
)


def test_what_is_not_a_row_is_dropped_but_counted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Columnas, celdas combinadas, formatos condicionales, validaciones y enlaces: openpyxl los
    armaba enteros fuera del presupuesto (hasta +137 MiB y 1,35 s). Solo se leen las filas; el
    resto se tira y cuenta en `MAX_NODES`, con todos los elementos de la hoja."""
    assert xlsxfile.MAX_NODES == 3 * xlsxfile.MAX_CELLS  # una celda con texto: c, is y t
    row = '<row r="2"><c r="A2" t="inlineStr"><is><t>A</t></is></c></row>'
    sheet = f'<worksheet xmlns="{MAIN}">{NOT_ROWS.format(HEADER + row)}</worksheet>'
    data = handmade(parts={"xl/worksheets/sheet1.xml": sheet})
    monkeypatch.setattr(xlsxfile, "MAX_NODES", 28)  # 10 + 4 de filas y 14 del resto
    assert xlsxfile.read(data, ALIASES).rows == [(2, {"sku": "A", "almacen": "", "cantidad": ""})]
    monkeypatch.setattr(xlsxfile, "MAX_NODES", 27)
    assert refused(data) == xlsxfile.TOO_FULL


def test_what_is_dropped_is_freed_as_it_closes() -> None:
    """Ni lo que no es fila ni las filas ya leídas se quedan en el árbol: 50 000 de cada."""
    sheet = NOT_ROWS.replace("<mergeCell ", '<mergeCell ref="A1:B1"/>' * 50_000 + "<mergeCell ")
    xml = f'<worksheet xmlns="{MAIN}">{sheet.format("<row/>" * 50_000)}</worksheet>'.encode()
    source = io.BytesIO(xml)
    tracemalloc.start()
    try:
        rows = sum(1 for _ in xlsxfile._units(source, xlsxfile.ROW, 10**6, "x"))
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert rows == 50_000
    assert peak < 1 << 20, peak  # sin quitarlos, unos 30 MiB


SST = "application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"


def with_strings(texts: str, rows: str) -> bytes:
    """Un XLSX con la tabla de textos compartidos `texts` (los `<si>`) y las filas `rows`."""
    parts: dict[str, bytes | str] = {"xl/sharedStrings.xml": f'<sst xmlns="{MAIN}">{texts}</sst>'}
    return handmade(HEADER + rows, types=override(SST, "/xl/sharedStrings.xml"), parts=parts)


def test_the_shared_strings_are_capped_while_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """openpyxl arma la tabla entera: 581 991 textos costaban +110 MiB y 2,3 s. Caben tantos como
    celdas (`MAX_CELLS`: son los de todas las hojas); sus elementos cuentan en `MAX_NODES` y los de
    cada texto en `MAX_UNIT`, como en la hoja."""
    texts = (
        "<si><t>A</t></si><si><r><t>B</t></r></si>" + "<si><r><t>z</t></r><r><t>z</t></r></si>" * 4
    )
    data = with_strings(
        texts, '<row r="2"><c r="A2" t="s"><v>0</v></c><c r="B2" t="s"><v>1</v></c></row>'
    )
    rows = [(2, {"sku": "A", "almacen": "B", "cantidad": ""})]
    monkeypatch.setattr(xlsxfile, "MAX_CELLS", 6)  # 6 textos y 5 celdas
    monkeypatch.setattr(xlsxfile, "MAX_NODES", 26)  # sst, 2, 3 y 4 × 5 (la hoja tiene 17)
    assert xlsxfile.read(data, ALIASES).rows == rows
    monkeypatch.setattr(xlsxfile, "MAX_NODES", 25)
    assert refused(data) == xlsxfile.TOO_MANY_TEXTS
    monkeypatch.setattr(xlsxfile, "MAX_NODES", 26)
    monkeypatch.setattr(xlsxfile, "MAX_CELLS", 5)
    assert refused(data) == xlsxfile.TOO_MANY_TEXTS
    monkeypatch.undo()
    monkeypatch.setattr(xlsxfile, "MAX_UNIT", 4)  # los últimos tienen 5 (y la cabecera, 10)
    assert refused(data) == xlsxfile.TOO_MANY_TEXTS


def test_as_many_shared_strings_as_cells_fit() -> None:
    """Excel guarda cada texto en la tabla, y la tabla es de todo el libro: 100 000 distintos."""
    texts = "".join(f"<si><t>texto {n}</t></si>" for n in range(xlsxfile.MAX_CELLS))
    row = '<row r="2"><c r="A2" t="s"><v>99999</v></c></row>'
    blank = {"almacen": "", "cantidad": ""}
    assert xlsxfile.read(with_strings(texts, row), ALIASES).rows == [
        (2, {"sku": "texto 99999", **blank})
    ]


@pytest.mark.parametrize(
    "text",
    [
        "<t> a b </t>",
        "<t/>",
        "",
        '<r><t>a</t></r><r><rPr><b/></rPr><t xml:space="preserve"> b</t></r>',
        '<t>p</t><r><t>q</t></r><rPh sb="0" eb="1"><t>fonética</t></rPh>',
        "<t>x005F_x</t>",
        "<t>a</t><t>b</t>",  # el último
    ],
)
def test_a_shared_string_reads_as_openpyxl_reads_it(text: str) -> None:
    """Su `<t>` y el de cada tramo con formato, sin la fonética, como `Text.content`."""
    same = Text.from_tree(fromstring(f'<si xmlns="{MAIN}">{text}</si>')).content
    row = '<row r="2"><c r="A2" t="s"><v>0</v></c><c r="C2"><v>1</v></c></row>'
    sheet = xlsxfile.read(with_strings(f"<si>{text}</si>", row), ALIASES)
    assert sheet.rows == [(2, {"sku": same.replace("x005F_", ""), "almacen": "", "cantidad": "1"})]


def test_a_long_tag_is_refused_before_it_is_built(monkeypatch: pytest.MonkeyPatch) -> None:
    """Los atributos de una etiqueta se arman antes de su evento: 270 000 costaban +140 MiB. No
    pasan más de `MAX_QUIET` bytes sin un elemento; el texto más largo de Excel sí cabe."""
    assert xlsxfile.MAX_QUIET == 256 * 1024
    longest = "".join(chr(0x4E00 + n % 20_000) for n in range(32_767))  # 3 bytes cada uno
    row = f'<row r="2"><c r="A2" t="inlineStr"><is><t>{longest}</t></is></c></row>'
    blank = {"almacen": "", "cantidad": ""}
    assert xlsxfile.read(handmade(HEADER + row), ALIASES).rows == [(2, {"sku": longest, **blank})]
    monkeypatch.setattr(xlsxfile, "MAX_NAMES", 10**6)  # solo este tope
    attrs = "".join(f' a{n}="1"' for n in range(30_000))  # unos 270 KB
    data = handmade(HEADER + f'<row r="2"{attrs}/>')
    tracemalloc.start()
    try:
        assert refused(data) == xlsxfile.TOO_FULL
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert peak < 4 << 20, peak  # armados, unos 10 MiB


def test_distinct_names_are_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    """El parser guarda cada nombre distinto de etiqueta o de atributo, aunque el elemento se
    tire: 425 000 costaban +130 MiB."""
    assert xlsxfile.MAX_NAMES == 1024
    monkeypatch.setattr(xlsxfile, "MAX_NAMES", 8)  # worksheet, sheetData, row, c, is, t, `r` y `t`
    assert xlsxfile.read(handmade(), ALIASES).rows == []
    monkeypatch.setattr(xlsxfile, "MAX_NAMES", 7)
    assert refused(handmade()) == xlsxfile.TOO_FULL


def test_row_attributes_are_not_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    """openpyxl guarda los atributos de cada fila que trae más que `r`: no se usan."""
    real, kept = WorkSheetParser.parse_row, []

    def parse_row(parser: Any, row: Any) -> Any:
        kept.append(len(parser.row_dimensions))
        return real(parser, row)

    monkeypatch.setattr(WorkSheetParser, "parse_row", parse_row)
    rows = "".join(f'<row r="{n}" ht="20"><c r="A{n}"><v>1</v></c></row>' for n in (2, 3, 4))
    assert len(xlsxfile.read(handmade(HEADER + rows), ALIASES).rows) == 3
    assert kept == [0, 0, 0, 0]


def spread(count: int, tag: str = "x") -> str:
    """`count` atributos distintos, 16 por elemento `tag`."""
    names = [f' a{n}="1"' for n in range(count)]
    return "".join(f"<{tag}{''.join(names[n : n + 16])}/>" for n in range(0, count, 16))


@pytest.mark.parametrize(
    "build",
    [
        lambda xml: handmade(
            parts={"xl/styles.xml": f'<styleSheet xmlns="{MAIN}">{xml}</styleSheet>'}
        ),
        lambda xml: handmade(order=FIRST + xml),
        lambda xml: handmade(links=LINK + xml),
        lambda xml: handmade(types=xml),
    ],
    ids=["estilos", "libro", "relaciones", "tipos"],
)
def test_the_parts_read_whole_are_walked_first(build: Callable[[str], bytes]) -> None:
    """openpyxl arma enteros estilos, libro, relaciones y tipos (1 MiB cada uno): 150 000 nombres
    distintos o los atributos de una sola etiqueta pasaban de 50 MiB. Se recorren antes con los
    topes de la hoja."""
    assert refused(build(spread(2 * xlsxfile.MAX_NAMES))) == xlsxfile.TOO_FULL_PART
    try:  # openpyxl puede no aceptar esos elementos; los topes, sí
        xlsxfile.read(build(spread(xlsxfile.MAX_NAMES // 2)), ALIASES)
    except Unreadable as error:
        assert str(error) == xlsxfile.DAMAGED


def test_a_long_tag_in_a_part_read_whole_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(xlsxfile, "MAX_NAMES", 10**6)  # solo `MAX_QUIET`
    attrs = "".join(f' a{n}="1"' for n in range(30_000))  # unos 270 KB
    styles = f'<styleSheet xmlns="{MAIN}"{attrs}/>'
    assert refused(handmade(parts={"xl/styles.xml": styles})) == xlsxfile.TOO_FULL_PART


def test_the_parts_read_whole_share_one_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hasta 100 partes de 1 MiB: los elementos se cuentan entre todas, el libro también."""
    assert xlsxfile.MAX_WHOLE_NODES == 32_768
    monkeypatch.setattr(xlsxfile, "MAX_WHOLE_NODES", 7)  # tipos 2, relaciones 2 y libro 3
    assert xlsxfile.read(handmade(), ALIASES).rows == []
    monkeypatch.setattr(xlsxfile, "MAX_WHOLE_NODES", 6)
    assert refused(handmade()) == xlsxfile.TOO_FULL_PART
