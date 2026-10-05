"""Nombres de rol: cómo se guardan y cuándo dos se leen igual (F2-29).

Un nombre lo escribe una persona y lo lee otra en una lista. `str.isprintable` y `strip` no
bastan: dejan pasar caracteres que no se ven, y dos cadenas distintas pueden pintarse igual.
"""

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


# Letras que ya llevan su punto (Soft_Dotted, tras NFKC y casefold) y las dos que lo reciben
# de un U+0307: «ı» y «ȷ» con punto se pintan como «i» y «j».
_DOTTED = frozenset("ij\u0249\u0268\u029d\u03f3\u0456\u0458\u1d96\U0001df1a")
_DOTLESS = {"\u0131": "i", "\u0237": "j"}


def _one_dot(text: str) -> str:
    """Sin el punto superior (U+0307) que cae sobre una letra que ya lo lleva, aunque entre
    ambos haya marcas que no van arriba (un ogonek, un punto inferior)."""
    out: list[str] = []
    for char in unicodedata.normalize("NFD", text):
        if char == "\u0307":
            at = len(out) - 1
            while at >= 0 and unicodedata.combining(out[at]) not in (0, 230):
                at -= 1
            if at >= 0 and (out[at] in _DOTTED or out[at] in _DOTLESS):
                out[at] = _DOTLESS.get(out[at], out[at])
                continue
        out.append(char)
    return "".join(out)


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
    repetidos, caracteres que no se ven, el guion tipográfico ni un punto añadido a una letra
    que ya lo lleva. No cubre letras de otro alfabeto que se parecen (una «О» cirílica): eso
    no lo resuelve una validación."""
    folded = unicodedata.normalize("NFKC", name).casefold().replace("\u2010", "-")
    seen = "".join(char for char in folded if char not in _UNSEEN)  # antes del punto: no lo tapan
    return " ".join(unicodedata.normalize("NFC", _one_dot(seen)).split())


def legible(name: str) -> bool:
    """¿Queda algo que leer? Solo marcas, espacios o caracteres que no se ven no es un nombre."""
    return any(unicodedata.category(char)[0] not in "MZ" for char in key(name))
