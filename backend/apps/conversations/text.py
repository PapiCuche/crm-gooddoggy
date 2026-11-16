"""La regla del texto de un mensaje del Inbox, fuera de los comandos protegidos: la comparten los
comandos de mensajes (`commands`), los de respuestas rápidas (F6-08a1) y la API. Pura."""

import unicodedata
from typing import Any

from apps.conversations.models import BODY_MAX

BIDI = "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"


def clean_body(value: Any) -> str:
    """El texto de un mensaje: algo que se ve (no solo espacios ni caracteres de ancho cero), sin
    controles salvo el salto de línea y el tabulador, ni sustitutos Unicode sueltos (UTF-8 y
    PostgreSQL no los guardan), ni los controles que reordenan el texto (incrustaciones,
    anulaciones y aislamientos de dirección; las marcas LRM, RLM y ALM sí valen)."""
    if not isinstance(value, str) or len(value) > BODY_MAX:
        raise ValueError(f"texto: un texto de 1 a {BODY_MAX} caracteres")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("texto: caracteres Unicode no válidos") from None
    if any(unicodedata.category(c) == "Cc" and c not in "\n\r\t" for c in value):
        raise ValueError("texto: sin caracteres de control")
    if any(c in BIDI for c in value):  # reordenan lo que se ve (texto «trojan source»)
        raise ValueError("texto: sin controles de dirección")
    if all(unicodedata.category(c) in ("Cc", "Cf", "Zs", "Zl", "Zp") for c in value):
        raise ValueError("texto: debe tener algo visible")
    return value
