"""Reglas de texto de los nombres y líneas que guardan los comandos (sucursales, equipos, marcas).
Puras: no dependen de ningún módulo."""

import unicodedata

# Rellenos hangul y los blancos jeroglíficos (U+13441 y U+13442, Unicode 15): letras para Unicode
# (`isalnum`), pero no se ven; un nombre solo de ellos parece vacío.
FILLERS = frozenset("\u115f\u1160\u3164\uffa0\U00013441\U00013442")
# Lo que no se ve y que ningún nombre necesita (F2-131), además de `FILLERS` (el coreano en NFC va
# en sílabas compuestas): el CGJ, las vocales inherentes jemer que Unicode desaconseja, la cabeza
# de nota nula y el relleno kitán. Siempre se rechazan.
UNSEEN = frozenset("\u034f\u17b4\u17b5\U0001d159\U00016fe4") | FILLERS
# ZWNJ y ZWJ: sí hacen falta dentro de una palabra persa, árabe o índica (no en estas escrituras).
JOINERS = frozenset("\u200c\u200d")
_UNJOINED = ("LATIN", "FULLWIDTH LATIN", "GREEK", "CYRILLIC", "CJK", "HANGUL", "HIRAGANA",
             "KATAKANA", "HALFWIDTH")  # fmt: skip
HIDDEN_MAX = 8  # selectores y ZWNJ/ZWJ en una línea: un nombre real lleva de 1 a 3
INFO = "\u2139"  # U+2139: una letra para Unicode, pero también un emoji con VS16
BLANK = "\u2800"  # el braille vacío: un símbolo que no se ve
_IDEOGRAPH = ("CJK UNIFIED IDEOGRAPH", "CJK COMPATIBILITY IDEOGRAPH")


def _named(char: str, prefix: str | tuple[str, ...]) -> bool:
    return bool(char) and unicodedata.name(char, "").startswith(prefix)


def _selector_fits(selector: str, base: str) -> bool | None:
    """`None` si `selector` no es un selector de variación; si lo es, ¿puede cambiar a `base`
    (el carácter anterior, o vacío)? Python no trae la tabla de secuencias de variación de
    Unicode: es una aproximación por clase de base."""
    point = ord(selector)
    if 0xFE00 <= point <= 0xFE0F:  # VS1–16: emoji o texto (un corazón, un keycap) e ideogramas
        symbol = bool(base) and base != BLANK and unicodedata.category(base)[0] in "SNP"
        symbol = symbol or base == INFO
        return symbol or _named(base, _IDEOGRAPH)
    if 0xE0100 <= point <= 0xE01EF:  # VS17–256: variantes de ideograma (IVS)
        return _named(base, _IDEOGRAPH)
    if point in (0x180B, 0x180C, 0x180D, 0x180F):  # los selectores mongoles
        return _named(base, "MONGOLIAN LETTER")
    return None


def _letter(char: str) -> bool:
    """Una letra de una escritura que une con ZWNJ/ZWJ (ni latina, ni griega, ni cirílica, ni
    CJK, ni hangul ni kana) y que se ve."""
    named = unicodedata.category(char)[0] == "L" and not _named(char, _UNJOINED)
    return named and char not in FILLERS


def _joins(text: str, at: int) -> bool:
    """¿El ZWNJ o ZWJ de `text[at]` está dentro de una palabra que lo necesita? Antes, una letra
    (con sus marcas, como el virama índico); después, otra letra; las dos de `_letter`."""
    before = at - 1
    while before >= 0 and unicodedata.category(text[before])[0] == "M":
        before -= 1
    after = text[at + 1 : at + 2]
    return before >= 0 and _letter(text[before]) and bool(after) and _letter(after)


def _hides(text: str) -> bool:
    """¿Lleva `text` algo que no se ve fuera de su sitio? Un carácter de `UNSEEN`, un selector de
    variación que no sigue a una base que pueda cambiar (uno por base: tras otro selector no
    cabe), un ZWNJ/ZWJ fuera de una palabra o más de `HIDDEN_MAX` selectores y ZWNJ/ZWJ."""
    hidden = 0
    for at, char in enumerate(text):
        fits = _selector_fits(char, text[at - 1] if at else "")
        joiner = char in JOINERS
        hidden += fits is not None or joiner
        if char in UNSEEN or fits is False or (joiner and not _joins(text, at)):
            return True
    return hidden > HIDDEN_MAX


def line(value: str, limit: int) -> str:
    """Una línea de texto como se guarda: forma NFC, sin espacios exteriores y con los
    interiores reducidos a uno. `ValueError` si lleva saltos de línea, controles o caracteres
    que no se ven (ZWNJ y ZWJ solo valen dentro de una palabra; un selector de variación, tras
    su base), o si pasa de `limit`. El mensaje nunca repite el valor."""
    spaced = "".join(" " if unicodedata.category(char) == "Zs" else char for char in value)
    text = unicodedata.normalize("NFC", spaced)  # no mueve ZWNJ, ZWJ ni selectores (ccc 0)
    shown = "".join(char for char in text.strip() if char not in JOINERS)
    if not shown.isprintable():  # antes de limpiar: un salto de línea no es un espacio
        raise ValueError("solo texto imprimible, en una línea")
    text = " ".join(text.split())
    if _hides(text):
        raise ValueError("solo texto imprimible, en una línea")
    if len(text) > limit:
        raise ValueError(f"hasta {limit} caracteres")
    return text


def name(value: str, limit: int) -> str:
    """Un nombre: una línea que además lleva alguna letra o cifra que se vea (un relleno de
    `FILLERS` no cuenta)."""
    text = line(value, limit)
    if not any(char.isalnum() and char not in FILLERS for char in text):
        raise ValueError("nombre obligatorio, con alguna letra o cifra")
    return text


def optional_name(value: str, limit: int) -> str:
    """Un nombre que se puede dejar vacío (un apellido): vacío, o con la regla de `name`."""
    text = line(value, limit)
    return name(text, limit) if text else text
