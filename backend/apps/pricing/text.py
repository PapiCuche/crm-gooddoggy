"""El motivo de un cambio de precio o de costo: una sola regla para `prices` y `costs`."""

import re

from core.text import line

REASON_MIN, REASON_MAX = 3, 500
TAG = re.compile(r"<[A-Za-z/!?]")  # el comienzo de una etiqueta o un comentario HTML


def change_reason(value: str) -> str:
    """El motivo como se guarda: una línea de 3 a 500 caracteres, sin etiquetas HTML."""
    text = line(value, REASON_MAX)
    if len(text) < REASON_MIN or TAG.search(text):
        raise ValueError(f"de {REASON_MIN} a {REASON_MAX} caracteres, sin HTML")
    return text
