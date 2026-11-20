"""Reglas de texto de los nombres y líneas que guardan los comandos (sucursales, equipos, marcas),
lo que no se ve en un texto libre que leerá el copiloto de IA (`unhide`, F8-11; ADR-034 §7) y la
marca `[verificar]` del copiloto, también disfrazada (`has_verify_mark`, F8-08a; ADR-034 §9.3).
Puras: solo dependen de su tabla de datos (`core.confusables`)."""

import re
import unicodedata

from core.confusables import PROTOTYPES

# Rellenos hangul y los blancos jeroglíficos (U+13441 y U+13442, Unicode 15): letras para Unicode
# (`isalnum`), pero no se ven; un nombre solo de ellos parece vacío.
FILLERS = frozenset("\u115f\u1160\u3164\uffa0\U00013441\U00013442")
# Lo que no se ve y que ningún nombre necesita (F2-131), además de `FILLERS` (el coreano en NFC va
# en sílabas compuestas): el CGJ, las vocales inherentes jemer que Unicode desaconseja, la cabeza
# de nota nula y el relleno kitán. Siempre se rechazan.
UNSEEN = frozenset("\u034f\u17b4\u17b5\U0001d159\U00016fe4") | FILLERS
# ZWNJ y ZWJ: sí hacen falta dentro de una palabra persa, árabe o índica (no en estas escrituras,
# ni en las letras que se les parecen: la letra o su forma NFKC, como ª, 𝐚, ℓ o la Ａ de ancho
# completo, cuenta como la latina; las modificadoras ʻ y las armenias, georgianas, cheroquis y
# coptas, aunque no tengan forma NFKC).
JOINERS = frozenset("\u200c\u200d")
_UNJOINED = ("LATIN", "GREEK", "CYRILLIC", "CJK", "HANGUL", "HIRAGANA", "KATAKANA",
             "MODIFIER LETTER", "ARMENIAN", "GEORGIAN", "CHEROKEE", "COPTIC")  # fmt: skip
HIDDEN_MAX = 8  # selectores y ZWNJ/ZWJ en una línea: un nombre real lleva de 1 a 3
MARKS_MAX = 4  # marcas combinantes seguidas: el vietnamita lleva 2; el hebreo con puntos, hasta 4
DIRECTION = frozenset("\u200e\u200f\u061c")  # LRM, RLM y ALM: el texto árabe y hebreo los usa
TONES = range(0x1F3FB, 0x1F400)  # los tonos de piel de los emoji
PLAIN = "\ufe0e\ufe0f"  # VS15 y VS16: texto o emoji (log2 3 bits por emoji visible), sin tope
# Lo que lleva VS15/16 sin ser un símbolo (So, Sm): los keycaps y ‼ ⁉ 〰 〽 ™ ℹ.
EMOJI = frozenset("#*0123456789\u203c\u2049\u3030\u303d\u2122\u2139")
# Las bases de un keycap (F8-07e): cifras de cualquier texto, así que VS15/16 tras ellas solo vale
# si sigue la tecla U+20E3; si no, cada cifra escondería un selector sin tope.
KEYS, KEYCAP = frozenset("#*0123456789"), "\u20e3"
INFO = "\u2139"  # U+2139: una letra para Unicode, pero también un emoji con VS16
BLANK = "\u2800"  # el braille vacío: un símbolo que no se ve
_IDEOGRAPH = ("CJK UNIFIED IDEOGRAPH", "CJK COMPATIBILITY IDEOGRAPH")


def _named(char: str, prefix: str | tuple[str, ...]) -> bool:
    return bool(char) and unicodedata.name(char, "").startswith(prefix)


def _selector_fits(selector: str, base: str, after: str) -> bool | None:
    """`None` si `selector` no es un selector de variación; si lo es, ¿puede cambiar a `base`
    (el carácter anterior, o vacío; `after` es el siguiente)? Python no trae la tabla de
    secuencias de variación de Unicode: es una aproximación por clase de base."""
    point = ord(selector)
    if selector in PLAIN:  # VS15/16: tras un símbolo o lo que puede ser emoji (keycap, ‼, ™, ℹ)
        if base in KEYS:
            return after == KEYCAP
        kind = unicodedata.category(base) if base else ""
        return base != BLANK and (kind in ("So", "Sm") or base in EMOJI)
    if 0xFE00 <= point <= 0xFE0F:  # VS1–14: emoji o texto (un corazón, un keycap) e ideogramas
        symbol = bool(base) and base != BLANK and unicodedata.category(base)[0] in "SNP"
        symbol = symbol or base == INFO
        return symbol or _named(base, _IDEOGRAPH)
    if 0xE0100 <= point <= 0xE01EF:  # VS17–256: variantes de ideograma (IVS)
        return _named(base, _IDEOGRAPH)
    if point in (0x180B, 0x180C, 0x180D, 0x180F):  # los selectores mongoles
        return _named(base, "MONGOLIAN LETTER")
    return None


def _letter(char: str) -> bool:
    """Una letra de una escritura que une con ZWNJ/ZWJ (ni de `_UNJOINED` ni parecida) y que se
    ve."""
    if unicodedata.category(char)[0] != "L" or char in FILLERS:
        return False
    folded = unicodedata.normalize("NFKC", char)[:1]
    return not (_named(char, _UNJOINED) or _named(folded, _UNJOINED))


def _mark(char: str) -> bool:
    return unicodedata.category(char)[0] == "M"


def _base(text: str | list[str], at: int) -> str:
    """El carácter de `text` antes de `at`, saltando sus marcas (como el virama índico)."""
    before = at - 1
    while before >= 0 and _mark(text[before]):
        before -= 1
    return text[before] if before >= 0 else ""


def _joins(text: str | list[str], at: int, after: str) -> bool:
    """¿El ZWNJ o ZWJ en `at` (`after` es lo que le sigue) está dentro de una palabra que lo
    necesita? Antes, una letra con sus marcas; después, otra letra; las dos de `_letter`."""
    before = _base(text, at)
    return bool(before) and _letter(before) and bool(after) and _letter(after)


def _glues(text: list[str], at: int, after: str) -> bool:
    """¿El ZWJ en `at` une dos emoji (la familia, el corazón en llamas, una profesión con tono de
    piel)? Antes, un símbolo o un tono de piel, con su VS16; después, otro símbolo (o uno
    matemático, como la flecha de la cara que niega)."""
    before = _base(text, at)
    emoji = bool(before) and (unicodedata.category(before) == "So" or ord(before) in TONES)
    return emoji and bool(after) and unicodedata.category(after) in ("So", "Sm")


def _hides(text: str) -> bool:
    """¿Lleva `text` algo que no se ve fuera de su sitio? Un carácter de `UNSEEN`, un selector de
    variación que no sigue a una base que pueda cambiar (uno por base: tras otro selector no
    cabe), más de `MARKS_MAX` marcas seguidas, un ZWNJ/ZWJ fuera de una palabra o más de
    `HIDDEN_MAX` selectores y ZWNJ/ZWJ."""
    hidden = marks = 0
    for at, char in enumerate(text):
        fits = _selector_fits(char, text[at - 1] if at else "", text[at + 1 : at + 2])
        joiner = char in JOINERS
        hidden += fits is not None or joiner
        marks = marks + 1 if _mark(char) else 0
        if char in UNSEEN or fits is False or marks > MARKS_MAX:
            return True
        if joiner and not _joins(text, at, text[at + 1 : at + 2]):
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


def _ignored(char: str) -> bool:
    """Lo que nunca se ve ni hace falta en un texto libre: `UNSEEN`, los caracteres de formato
    (salvo ZWNJ, ZWJ, LRM, RLM y ALM), con las etiquetas U+E0000–E007F, y los puntos sin asignar
    que Unicode manda no mostrar (Default_Ignorable: U+2065, U+FFF0–FFF8 y U+E0000–E0FFF fuera
    de los selectores ideográficos)."""
    point = ord(char)
    if char in UNSEEN or point == 0x2065 or 0xFFF0 <= point <= 0xFFF8:
        return True
    if 0xE0000 <= point <= 0xE0FFF and not 0xE0100 <= point <= 0xE01EF:
        return True
    return unicodedata.category(char) == "Cf" and char not in JOINERS and char not in DIRECTION


def unhide(text: str) -> str:
    """`text` sin lo que no se ve fuera de su sitio; lo que se ve no cambia. Quita lo de
    `_ignored`, un selector de variación que no sigue a una base que pueda cambiar (uno por base),
    más de `MARKS_MAX` marcas seguidas sobre una base, un ZWNJ/ZWJ fuera de una palabra que lo
    necesite (o un ZWJ fuera de un emoji compuesto) y, pasado el tope del valor, los que pueden
    llevar datos: selectores (salvo VS15 y VS16), ZWNJ/ZWJ entre letras y LRM, RLM y ALM. El tope
    es `HIDDEN_MAX` más uno por cada 20 caracteres que `unhide` nunca quita. Nunca lanza, es
    idempotente y lineal. Para lo que entra de un cliente; lo que escribe el personal se rechaza
    si cambiaría (`shows_all`)."""
    seen = sum(not (_mark(c) or c in JOINERS or c in DIRECTION or _ignored(c)) for c in text)
    room = HIDDEN_MAX + seen // 20
    ahead = [""] * (len(text) + 1)  # lo siguiente que no se quita nunca (`_ignored` sí se quita)
    for at in range(len(text) - 1, -1, -1):
        ahead[at] = ahead[at + 1] if _ignored(text[at]) else text[at]
    kept: list[str] = []
    marks = 0  # marcas seguidas sobre la última base
    for at, char in enumerate(text):
        if _ignored(char):
            continue
        after, mark = ahead[at + 1], _mark(char)
        fits = _selector_fits(char, kept[-1] if kept else "", after)
        if char in JOINERS:
            glue = char == "\u200d" and _glues(kept, len(kept), after)
            keep, counted = glue or _joins(kept, len(kept), after), not glue
        elif fits is not None:
            keep, counted = fits, char not in PLAIN
        else:
            keep, counted = not mark or marks < MARKS_MAX, char in DIRECTION
        if keep and counted:
            keep, room = room > 0, room - 1
        if keep:
            kept.append(char)
            marks = marks + 1 if mark else marks if char in JOINERS or char in DIRECTION else 0
    return "".join(kept)


def shows_all(text: str) -> bool:
    """¿`text` se ve entero? Es decir, `unhide` no le quitaría nada."""
    return unhide(text) == text


VERIFY_MARK = "[verificar]"  # la marca del copiloto en lo que no tiene evidencia (ADR-034 §9)
_PROTOTYPE = str.maketrans({char: shape for shape, chars in PROTOTYPES.items() for char in chars})


def _unseen(char: str) -> bool:
    """Lo que el esqueleto de UTS #39 quita (Default_Ignorable) y no es de formato (`Cf`, que la
    clave ya quitó): `UNSEEN`, los selectores de variación y los puntos ignorables sin asignar."""
    hidden = _ignored(char) and unicodedata.category(char) != "Cf"
    return hidden or _selector_fits(char, "") is not None


def _skeleton(text: str) -> str:
    """El esqueleto de confusables de UTS #39: NFD, sin `_unseen`, cada carácter por su prototipo
    (`core.confusables`, en minúsculas) y NFD otra vez."""
    kept = "".join(char for char in unicodedata.normalize("NFD", text) if not _unseen(char))
    return unicodedata.normalize("NFD", kept.translate(_PROTOTYPE))


def _key(text: str) -> str:
    """La clave con la que se compara (ADR-034 §9.3), en este orden: NFKC, sin `Cf`, `casefold()` y
    el esqueleto. Nunca se guarda ni se envía: quitar los `Cf` rompería los emoji unidos por ZWJ."""
    normal = unicodedata.normalize("NFKC", text)
    shown = "".join(char for char in normal if unicodedata.category(char) != "Cf")
    return _skeleton(shown.casefold())


def _brackets(text: str) -> str:
    """Cada signo de apertura (`Ps`) como «(» y cada uno de cierre (`Pe`) como «)»."""
    kinds = {"Ps": "(", "Pe": ")"}
    return "".join(kinds.get(unicodedata.category(char), char) for char in text)


_MARK = re.compile(rf"\(\s*{re.escape(_skeleton(VERIFY_MARK[1:-1]))}\s*\)")


def has_verify_mark(text: str) -> bool:
    """¿Lleva `text` la marca `[verificar]`, aunque la disfracen letras parecidas de otra escritura,
    otro ancho, caracteres que no se ven o mayúsculas? Busca la palabra entre un signo de apertura y
    uno de cierre cualesquiera, con espacios opcionales, en la clave `_key`. Pura; nunca lanza."""
    return _MARK.search(_brackets(_key(text))) is not None
