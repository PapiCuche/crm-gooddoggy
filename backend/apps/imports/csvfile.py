"""Leer y escribir los CSV de las importaciones (ADR-028 §5). Puro: no toca la BD.

Leer: tope de bytes antes de decodificar y de filas al recorrer; UTF-8 (con o sin BOM) y, si no
lo es, Windows-1252, el «CSV» que guarda Excel en Windows (ningún otro códec se adivina); el
separador sale de la cabecera (`,`, `;` o tabulador). Las columnas se reconocen por alias, sin
tildes ni mayúsculas; las demás se ignoran y no se guardan.

Escribir: cada celda que una hoja de cálculo tomaría por fórmula lleva delante un apóstrofo
(OWASP, CSV injection; también tras espacios o en ancho completo), y el archivo va en UTF-8 con
BOM para que Excel lea las tildes.
"""

import csv
import io
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

MAX_BYTES, MAX_ROWS, CELL_MAX = 1_048_576, 1_000, 200
SEPARATORS = (",", ";", "\t")
_FORMULA = ("=", "+", "-", "@", "\t", "\r", "|")


class Unreadable(ValueError):
    """El archivo entero no se puede leer: el mensaje dice por qué, para quien lo subió."""


@dataclass(frozen=True, slots=True)
class Sheet:
    encoding: str
    separator: str
    ignored: list[str]  # las cabeceras que no son de ninguna columna conocida
    rows: list[tuple[int, dict[str, str]]]  # (línea del archivo, columna → texto)


def key(text: str) -> str:
    """`Almacén ` → `almacen`: sin tildes, en minúsculas y con `_` por espacios."""
    plain = unicodedata.normalize("NFKD", text.strip().lower())
    return "_".join("".join(c for c in plain if not unicodedata.combining(c)).split())


def _decode(data: bytes) -> tuple[str, str]:
    try:
        return data.decode("utf-8-sig"), "utf-8"
    except UnicodeDecodeError:
        if data.startswith(b"\xef\xbb\xbf"):  # dice ser UTF-8: no se adivina otro códec
            raise Unreadable(
                "No es texto UTF-8 válido: guárdalo otra vez como «CSV UTF-8»."
            ) from None
    try:
        return data.decode("cp1252"), "windows-1252"
    except UnicodeDecodeError:
        raise Unreadable("No es texto UTF-8 ni de Excel: guárdalo como «CSV UTF-8».") from None


def _columns(header: list[str], aliases: Mapping[str, Iterable[str]]) -> dict[int, str]:
    wanted = {key(alias): column for column, names in aliases.items() for alias in names}
    found: dict[int, str] = {}
    for index, title in enumerate(header):
        column = wanted.get(key(title))
        if column in found.values():
            raise Unreadable(f"La columna «{column}» está dos veces.")
        if column:
            found[index] = column
    if missing := [column for column in aliases if column not in found.values()]:
        raise Unreadable(f"Falta la columna «{missing[0]}» (usa la plantilla).")
    return found


def read(data: bytes, aliases: Mapping[str, Iterable[str]]) -> Sheet:
    """Las filas con texto del archivo, con las columnas de `aliases` (todas obligatorias)."""
    if not data.strip():
        raise Unreadable("El archivo está vacío.")
    if len(data) > MAX_BYTES:
        raise Unreadable(f"El archivo pasa de {MAX_BYTES // 1024} KB: divídelo.")
    if data.startswith(b"PK\x03\x04") or b"\x00" in data:
        raise Unreadable("No es un CSV. Si es un Excel, guárdalo como «CSV UTF-8».")
    decoded, encoding = _decode(data)
    text = decoded.lstrip(" \t\r\n")  # las líneas en blanco del principio no son la cabecera
    skipped = decoded[: len(decoded) - len(text)].count("\n")
    if not text:
        raise Unreadable("El archivo está vacío.")
    separator = max(SEPARATORS, key=text.splitlines()[0].count)
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=separator, strict=True)
    try:
        header = next(reader)
        columns = _columns(header, aliases)
        rows: list[tuple[int, dict[str, str]]] = []
        for cells in reader:
            if not any(cell.strip() for cell in cells):
                continue
            if len(rows) == MAX_ROWS:
                raise Unreadable(f"Más de {MAX_ROWS} filas: divídelo en varios archivos.")
            cell = {column: cells[i] if i < len(cells) else "" for i, column in columns.items()}
            rows.append((reader.line_num + skipped, cell))
    except csv.Error as error:
        line = reader.line_num + skipped
        raise Unreadable(f"Línea {line}: el CSV está mal formado ({error}).") from None
    ignored = [title.strip() for i, title in enumerate(header) if i not in columns]
    return Sheet(encoding, separator, [title for title in ignored if title], rows)


def _safe(value: object) -> str:
    text = "" if value is None else str(value)
    head = unicodedata.normalize("NFKC", text).lstrip()  # « =1», «＝1»
    return f"'{text}" if text.startswith(_FORMULA) or head.startswith(_FORMULA) else text


def write(header: Sequence[str], rows: Iterable[Sequence[object]]) -> bytes:
    """Un CSV que ninguna hoja de cálculo ejecuta al abrirlo."""
    out = io.StringIO(newline="")
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow([_safe(title) for title in header])
    writer.writerows([_safe(value) for value in row] for row in rows)
    return out.getvalue().encode("utf-8-sig")
