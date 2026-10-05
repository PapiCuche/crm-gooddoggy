"""Nombres de rol: cómo se guardan y cuándo dos se leen igual (F2-29).

Un nombre lo escribe una persona y lo lee otra en una lista. `str.isprintable` y `strip` no
bastan: dejan pasar caracteres que no se ven, y dos cadenas distintas pueden pintarse igual.
"""

import re
import unicodedata

# Sin tinta en pantalla y, casi todos, imprimibles para Python: rellenos, selectores de
# variación, braille vacío, el carácter de objeto. Con uno de ellos «Owner» y «Owner» serían
# dos nombres.
_UNSEEN = frozenset(
    chr(point)
    for first, last in (
        (0x034F, 0x034F),
        (0x115F, 0x1160),
        (0x17B4, 0x17B5),
        (0x180B, 0x180F),
        (0x2800, 0x2800),
        (0x3164, 0x3164),
        (0xFFA0, 0xFFA0),
        (0xFE00, 0xFE0F),
        (0xFFFC, 0xFFFC),
        (0x1D159, 0x1D159),
        (0xE0100, 0xE01EF),
    )
    for point in range(first, last + 1)
)


def spaced(name: str) -> str:
    """Todo separador de espacio (el de no separación, el ideográfico) es un espacio. Un salto
    de línea o un tabulador no lo son: siguen ahí para que la validación los rechace."""
    return "".join(" " if unicodedata.category(char) == "Zs" else char for char in name)


def clean(name: str) -> str:
    """Como se guarda: forma NFC, sin espacios exteriores y con los interiores reducidos a uno."""
    return " ".join(unicodedata.normalize("NFC", name).split())


def key(name: str) -> str:
    """Dos nombres con la misma clave se leen igual: sin distinguir mayúsculas, formas de
    composición, de anchura o de compatibilidad (un superíndice, una ligadura), espacios
    repetidos ni caracteres que no se ven. No cubre letras
    de otro alfabeto que se parecen (una «О» cirílica): eso no lo resuelve una validación."""
    folded = unicodedata.normalize("NFKC", name).casefold()
    folded = re.sub("(?<=[ij])\u0307", "", folded)  # un punto sobre una letra que ya lo lleva
    seen = "".join(char for char in folded if char not in _UNSEEN)
    return " ".join(unicodedata.normalize("NFC", seen).split())


def legible(name: str) -> bool:
    """¿Queda algo que leer? Solo marcas, espacios o caracteres que no se ven no es un nombre."""
    return any(unicodedata.category(char)[0] not in "MZ" for char in key(name))
