"""Importes de dinero (ADR-006, 04 §K.8): `Decimal` en el código y string decimal en la API,
nunca `float`. Puras: no dependen de ningún módulo."""

import re
from decimal import Decimal

AMOUNT = re.compile(r"[0-9]{1,12}(\.[0-9]{1,2})?")  # NUMERIC(14,2), sin signo ni exponente
CURRENCY = re.compile(r"[A-Z]{3}")  # ISO 4217
CENT = Decimal("0.01")


def parse_amount(value: str | Decimal) -> Decimal:
    """Un importe como se guarda: cifras ASCII, como mucho 12 enteras y 2 decimales, sin signo.
    No redondea lo que escribe una persona: `4399.999` es un `ValueError`, no 4400.00. Un `float`
    es un `TypeError`: ya perdió los céntimos antes de llegar aquí. Un `Decimal` se lee como se
    escribe: `Decimal("1.100")` también es un `ValueError`, así que quien calcula un importe lo
    normaliza antes (`quantize(CENT)`)."""
    if isinstance(value, Decimal):
        text = format(value, "f") if value.is_finite() else ""
    elif isinstance(value, str):
        text = value.strip()
    else:
        raise TypeError("un importe es un string decimal o un Decimal, nunca un float")
    if not AMOUNT.fullmatch(text):
        raise ValueError("un importe con como mucho 12 cifras enteras y 2 decimales")
    return Decimal(text).quantize(CENT)


def currency(value: str) -> str:
    """La moneda de un importe: un código ISO 4217 de tres letras mayúsculas (`PEN`)."""
    if not CURRENCY.fullmatch(value):
        raise ValueError("un código ISO 4217 de tres letras mayúsculas")
    return value


def amount_text(value: Decimal) -> str:
    """Cómo viaja un importe por la API: string con dos decimales (`"4399.00"`)."""
    return f"{value.quantize(CENT):f}"
