"""Códigos de respaldo del segundo factor (ADR-023 §6), con la biblioteca estándar.

Diez códigos de un solo uso por cuenta. Cada uno, 16 símbolos aleatorios de un alfabeto de 32
(80 bits): lo bastante para guardarlo como SHA-256 sin que quien lea una copia de la base de
datos lo recupere. Aquí no se guarda nada: quien llama guarda el hash, y lo gasta.

Nada de aquí registra ni devuelve en un error un código.
"""

import hashlib
import secrets

COUNT = 10
LENGTH = 16
GROUP = 4  # se enseñan en grupos de cuatro: `7K3M-9QXA-…`
# Base32 de Crockford: sin I, L, O ni U, que se confunden con un dígito o entre sí.
ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
# Lo que una persona teclea por lo que quiso decir: las letras que se leen como un dígito.
_MEANT = str.maketrans({"I": "1", "L": "1", "O": "0", "-": None, " ": None})


def new_codes() -> list[str]:
    """Los códigos de una cuenta, como se enseñan una vez: distintos, en grupos de cuatro."""
    codes: set[str] = set()
    while len(codes) < COUNT:
        codes.add("".join(secrets.choice(ALPHABET) for _ in range(LENGTH)))
    return ["-".join(code[at : at + GROUP] for at in range(0, LENGTH, GROUP)) for code in codes]


def normalized(presented: str) -> str | None:
    """Lo tecleado, como el código que se generó: en mayúsculas, sin guiones ni espacios y con
    las letras confundibles como su dígito. `None` si después no son 16 símbolos del alfabeto."""
    if not presented.isascii():  # antes de pasar a mayúsculas: `ı` daría `I`, y `ß`, dos letras
        return None
    code = presented.upper().translate(_MEANT)
    if len(code) != LENGTH or any(symbol not in ALPHABET for symbol in code):
        return None
    return code


def code_hash(presented: str) -> str | None:
    """Lo que se guarda de un código, y por lo que se busca: el SHA-256 del código normalizado.
    `None` si lo presentado no es un código: quien llama lo trata como uno que no vale."""
    code = normalized(presented)
    return None if code is None else hashlib.sha256(code.encode()).hexdigest()
