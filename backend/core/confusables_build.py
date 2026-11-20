"""Genera `core/confusables.py` (F8-08a; ADR-034 §9.3): la tabla reducida de los confusables de
Unicode (UTS #39) con la que `core.text.has_verify_mark` busca la marca `[verificar]`. Sin red: lee
una copia local del archivo fijado (`URL`) y se niega si su SHA-256 no es `SHA256` o si este Python
no trae la misma versión de Unicode (NFKC y las categorías salen de él).

Uso, desde `backend/`: `python -m core.confusables_build /ruta/a/confusables.txt`

Solo entra un carácter que puede llegar a la clave (NFKC, NFD y `casefold` no lo cambian y no es
`Cf`) y cuyo prototipo (en minúsculas y NFD) puede ser parte de una marca, entero o por un extremo:
apertura, espacios, la palabra, espacios y cierre. Lo que no entra se queda como está, así que la
búsqueda encuentra lo mismo que con el esqueleto completo, o algo más."""

import hashlib
import sys
import unicodedata
from pathlib import Path

from core.text import VERIFY_MARK

URL = "https://www.unicode.org/Public/security/16.0.0/confusables.txt"
VERSION = "16.0.0"
SHA256 = "95bd0aad6dced5ebc63436f459c06ab21a8d107cd842fb57f5c3a1e91bca8611"
OUT = Path(__file__).with_name("confusables.py")
WORD = VERIFY_MARK[1:-1]
WIDTH = 100  # la línea de ruff
HEADER = '''"""Generado por `core/confusables_build.py` (F8-08a): no editar a mano.

Los confusables de Unicode (UTS #39) que pueden formar la marca `[verificar]`: cada prototipo (en
minúsculas y NFD) con los caracteres que se le parecen. Fuente, Unicode {version}:
{url}
SHA-256 {sha256}.
© Unicode, Inc.; condiciones de uso: https://www.unicode.org/terms_of_use.html"""

VERSION = "{version}"
SHA256 = "{sha256}"
PROTOTYPES = {{
'''


def _keyable(char: str) -> bool:
    """¿Puede `char` llegar a la clave? NFKC, NFD y `casefold` no lo cambian y no es `Cf`."""
    same = unicodedata.normalize("NFKC", char) == char == unicodedata.normalize("NFD", char)
    return same and char.casefold() == char and unicodedata.category(char) != "Cf"


def _shape(text: str) -> str:
    """Cada carácter como lo ve la búsqueda: una letra de la palabra, un espacio, «(» o «)»; lo
    demás, «?»."""
    kinds = {"Ps": "(", "Pe": ")"}
    return "".join(
        c if c in WORD else " " if c.isspace() else kinds.get(unicodedata.category(c), "?")
        for c in text
    )  # fmt: skip


def contributes(target: str) -> bool:
    """¿Puede `target` ser parte de una marca: entero, por su final (el comienzo de la marca) o por
    su comienzo (el final de la marca)?"""
    s, size = _shape(target), len(target)
    for before in range(size + 1):
        for after in range(size + 1):
            mark = "(" + " " * before + WORD + " " * after + ")"
            ends = any(mark.startswith(s[c:]) or mark.endswith(s[:c]) for c in range(1, size))
            if s in mark or mark in s or ends:
                return True
    return False


def table(text: str) -> dict[str, str]:
    """De `confusables.txt`: cada prototipo que entra, con los caracteres que se le parecen."""
    found: dict[str, str] = {}
    for row in text.splitlines():
        data = row.split("#", 1)[0].strip()
        if not data:
            continue
        source, target = ("".join(chr(int(p, 16)) for p in f.split()) for f in data.split(";")[:2])
        shape = unicodedata.normalize("NFD", target.casefold())
        if len(source) == 1 and _keyable(source) and contributes(shape):
            found[shape] = found.get(shape, "") + source
    return dict(sorted(found.items()))


def _escape(text: str) -> str:
    return "".join(
        char if char.isascii() and char.isprintable() and char not in '"\\'
        else f"\\u{ord(char):04x}" if ord(char) < 0x10000 else f"\\U{ord(char):08x}"
        for char in text
    )  # fmt: skip


def render(prototypes: dict[str, str]) -> str:
    """El módulo `core/confusables.py`, con el formato de ruff (líneas de hasta `WIDTH`)."""
    lines = [HEADER.format(url=URL, version=VERSION, sha256=SHA256)]
    for shape, chars in prototypes.items():
        key, value = f'    "{_escape(shape)}": ', f'"{_escape(chars)}",'
        if len(key + value) <= WIDTH:
            lines.append(key + value + "\n")
            continue
        lines.append(key + "(\n")
        chunk = ""
        for char in map(_escape, chars):
            if len(chunk + char) > WIDTH - 10:
                lines.append(f'        "{chunk}"\n')
                chunk = ""
            chunk += char
        lines.append(f'        "{chunk}"\n    ),\n')
    return "".join(lines) + "}\n"


def build(raw: bytes) -> str:
    """El módulo a partir del `confusables.txt` fijado; `ValueError` si no es ese archivo o si este
    Python trae otra versión de Unicode."""
    if hashlib.sha256(raw).hexdigest() != SHA256:
        raise ValueError(f"no es el confusables.txt de {URL}")
    if unicodedata.unidata_version != VERSION:
        raise ValueError(f"este Python trae Unicode {unicodedata.unidata_version}, no {VERSION}")
    return render(table(raw.decode("utf-8-sig")))


if __name__ == "__main__":
    OUT.write_text(build(Path(sys.argv[1]).read_bytes()), encoding="utf-8")
