"""F3-20: el lector y el escritor CSV de las importaciones (`apps.imports.csvfile`). Sin BD."""

import pytest

from apps.imports import csvfile
from apps.imports.csvfile import Unreadable

ALIASES = {"sku": ("sku",), "almacen": ("almacen", "warehouse"), "cantidad": ("cantidad",)}


def test_utf8_with_or_without_bom_excel_and_the_separator() -> None:
    for data, encoding in (
        ("sku,Almacén,cantidad\r\nA1,ALM,3\r\n".encode(), "utf-8"),
        ("\ufeffSKU;ALMACÉN ;Cantidad;Notas\nA1;ALM;3\n".encode(), "utf-8"),
        ("sku;almacén;cantidad;notas\nA1;ALM;3\n".encode("cp1252"), "windows-1252"),
    ):
        sheet = csvfile.read(data, ALIASES)
        assert sheet.encoding == encoding
        assert sheet.rows == [(2, {"sku": "A1", "almacen": "ALM", "cantidad": "3"})]
    tabs = csvfile.read(b"warehouse\tsku\tcantidad\textra\nALM\tA1\t3\n", ALIASES)
    assert (tabs.separator, tabs.ignored) == ("\t", ["extra"])
    assert tabs.rows[0][1] == {"sku": "A1", "almacen": "ALM", "cantidad": "3"}


def test_blank_lines_are_skipped_short_rows_are_padded_and_lines_are_the_files() -> None:
    data = b'\r\n \n\tsku;almacen;cantidad\n\n ; ;\nA1;ALM\n"B\n2";ALM;1\nC3;ALM;4\n'
    sheet = csvfile.read(data, ALIASES)
    assert [line for line, _ in sheet.rows] == [6, 8, 9]
    assert sheet.rows[0][1]["cantidad"] == "" and sheet.rows[1][1]["sku"] == "B\n2"


@pytest.mark.parametrize(
    ("data", "says"),
    [
        (b"", "vac"),
        (b"  \n", "vac"),
        (b"PK\x03\x04rest", "No es un CSV"),
        (b"s\x00k\x00u\x00", "No es un CSV"),
        (b"sku,almacen,cantidad\n\x81\x8d,ALM,1\n", "UTF-8"),
        (b"\xef\xbb\xbf\r\n", "vac"),
        (b"\xef\xbb\xbfsku,almac\xe9n,cantidad\n", "UTF-8 válido"),  # BOM y luego Windows-1252
        (b"sku,cantidad\nA1,1\n", "«almacen»"),
        (b"sku,almacen,warehouse,cantidad\n", "dos veces"),
        (b'\nsku,almacen,cantidad\n"A1,ALM,1\n', "Línea 3:"),
        (b"sku,almacen,cantidad\n" + b"A,B,1\n" * (csvfile.MAX_ROWS + 1), "1000 filas"),
        (b"sku,almacen,cantidad\n" + b"x" * csvfile.MAX_BYTES, "KB"),
    ],
)
def test_what_cannot_be_read_says_why(data: bytes, says: str) -> None:
    with pytest.raises(Unreadable) as refused:
        csvfile.read(data, ALIASES)
    assert says in str(refused.value)


def test_the_row_cap_counts_rows_with_text_only() -> None:
    data = b"sku,almacen,cantidad\n" + b"A,B,1\n\n" * csvfile.MAX_ROWS
    assert len(csvfile.read(data, ALIASES).rows) == csvfile.MAX_ROWS


def test_what_is_written_never_runs_as_a_formula() -> None:
    rows = [["=1+1", "+3", "-2", "@SUM(A1)", "\tx", "\rx", "ok", 7, None, 'a "b", c']]
    rows += [[" =1", "\uff1d1", "|cmd", "a=b"]]
    text = csvfile.write(["=cmd", "b"], rows).decode("utf-8")
    assert text.startswith("\ufeff'=cmd,b\r\n")
    assert text.split("\r\n")[1] == '\'=1+1,\'+3,\'-2,\'@SUM(A1),\'\tx,"\'\rx",ok,7,,"a ""b"", c"'
    assert text.split("\r\n")[2] == "' =1,'\uff1d1,'|cmd,a=b"
