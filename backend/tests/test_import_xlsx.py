"""F3-23: el lector XLSX de las importaciones (`apps.imports.xlsxfile`). Sin BD.

Sin archivos hechos en Excel a mano: los libros los escribe openpyxl y los hostiles se arman
parte a parte, con el XML que escribiría Excel."""

import datetime
import io
import struct
import tracemalloc
import zipfile
import zlib
from typing import Any

import openpyxl.xml
import pytest
from openpyxl import Workbook

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


def test_a_part_shorter_than_what_it_declares_is_damaged() -> None:
    content = f"{TYPES}{override(XLSX)}</Types>".encode()
    name = "[Content_Types].xml"
    short = declare(handmade(), name, len(content) + 1, content)
    assert refused(short) == xlsxfile.DAMAGED
    assert xlsxfile.read(declare(handmade(), name, len(content), content), ALIASES).rows == []


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
        (
            handmade(types=override(XLSM, "/xl/b.xml")),
            xlsxfile.NOT_XLSX,  # dos libros: openpyxl leería el de macros
        ),
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
