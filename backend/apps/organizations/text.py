"""Reglas de texto que comparten los comandos de `organizations` (sucursales, equipos)."""

import unicodedata


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
    """Un nombre: una línea que además lleva alguna letra o cifra."""
    text = line(value, limit)
    if not any(char.isalnum() for char in text):
        raise ValueError("nombre obligatorio, con alguna letra o cifra")
    return text
