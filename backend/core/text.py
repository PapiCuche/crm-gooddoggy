"""Reglas de texto de los nombres y líneas que guardan los comandos (sucursales, equipos, marcas).
Puras: no dependen de ningún módulo."""

import unicodedata

# Rellenos hangul: letras para Unicode (`isalnum`), pero no se ven; un nombre solo de ellos
# parece vacío.
FILLERS = frozenset("\u115f\u1160\u3164\uffa0")


def line(value: str, limit: int) -> str:
    """Una línea de texto como se guarda: forma NFC, sin espacios exteriores y con los
    interiores reducidos a uno. `ValueError` si lleva saltos de línea, controles o caracteres
    que no se ven, o si pasa de `limit`."""
    spaced = "".join(" " if unicodedata.category(char) == "Zs" else char for char in value)
    text = unicodedata.normalize("NFC", spaced)
    if not text.strip().isprintable():  # antes de limpiar: un salto de línea no es un espacio
        raise ValueError("solo texto imprimible, en una línea")
    text = " ".join(text.split())
    if len(text) > limit:
        raise ValueError(f"hasta {limit} caracteres")
    return text


def name(value: str, limit: int) -> str:
    """Un nombre: una línea que además lleva alguna letra o cifra que se vea (un relleno hangul
    no cuenta)."""
    text = line(value, limit)
    if not any(char.isalnum() and char not in FILLERS for char in text):
        raise ValueError("nombre obligatorio, con alguna letra o cifra")
    return text
