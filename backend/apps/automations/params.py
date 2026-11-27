"""Parámetros de cada regla registrada (F11-02; ADR-049 §2, D-F11-03, D-F11-04): tipos y rangos
cerrados y sus valores por omisión. Una clave que no está en el esquema se rechaza, nunca se
guarda. Todo lo que queda son números y códigos (`HH:MM`, días ISO), nunca texto libre."""

import re
from collections.abc import Callable, Mapping
from typing import Any

CONVERSATION_UNANSWERED, QUOTE_NO_RESPONSE = "conversation.unanswered", "quote.no_response"
_CLOCK = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d")


def _int(low: int, high: int) -> Callable[[Any], int]:
    def check(value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ValueError(f"un entero de {low} a {high}")
        return int(value)

    return check


def _clock(value: Any) -> str:
    if not isinstance(value, str) or not _CLOCK.fullmatch(value):
        raise ValueError("una hora HH:MM")
    return value


def _days(value: Any) -> list[int]:
    """Días ISO (1 = lunes … 7 = domingo), sin repetir y al menos uno; se guardan ordenados."""
    days = [_int(1, 7)(day) for day in value] if isinstance(value, list) else []
    if not days or len(set(days)) != len(days):
        raise ValueError("una lista de días sin repetir")
    return sorted(days)


_HOURS: dict[str, tuple[Callable[[Any], Any], Any]] = {  # D-F11-03: lunes a sábado, 09:00-19:00
    "days": (_days, [1, 2, 3, 4, 5, 6]),
    "opens_at": (_clock, "09:00"),
    "closes_at": (_clock, "19:00"),
}
SCHEMAS: dict[str, dict[str, tuple[Callable[[Any], Any], Any]]] = {
    CONVERSATION_UNANSWERED: {
        "first_after_minutes": (_int(1, 1440), 15),
        "escalate_after_minutes": (_int(1, 1440), 45),
        "max_recipients": (_int(1, 3), 3),  # D-F11-04 y E1: ningún aviso pasa de 3 personas
        "max_open_per_user": (_int(1, 100), 20),  # §6
        **_HOURS,
    },
    QUOTE_NO_RESPONSE: {
        "after_hours": (_int(1, 720), 48),
        "before_expiry_hours": (_int(0, 720), 24),  # 0: sin recordatorio antes de vencer
        **_HOURS,
    },
}
KEYS = tuple(SCHEMAS)


def defaults(key: str) -> dict[str, Any]:
    """Los parámetros de una regla sin configurar."""
    return {name: default for name, (_, default) in SCHEMAS[key].items()}


def validate(key: str, params: Mapping[str, Any]) -> dict[str, Any]:
    """`params` completos y limpios. `ValueError` con el nombre del parámetro, nunca su valor."""
    schema = SCHEMAS[key]
    if not isinstance(params, Mapping) or set(params) != set(schema):
        raise ValueError("params: claves no admitidas o que faltan")
    clean = {}
    for name, (check, _) in schema.items():
        try:
            clean[name] = check(params[name])
        except ValueError as error:
            raise ValueError(f"{name}: {error}") from None
    if clean["opens_at"] >= clean["closes_at"]:
        raise ValueError("closes_at: después de opens_at")
    return clean
