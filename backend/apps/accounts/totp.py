"""Códigos TOTP (RFC 6238 sobre RFC 4226), con la biblioteca estándar (ADR-023 §2).

HMAC-SHA-1, 6 dígitos y pasos de 30 segundos: lo que entiende cualquier aplicación de
autenticación. Aquí no se guarda nada: el secreto entra y sale como bytes (lo cifra y lo guarda
quien llama, con `core.sealed`), y el último paso aceptado lo trae y lo guarda quien llama.

Nada de aquí registra ni devuelve en un error el secreto o un código.
"""

import base64
import hashlib
import hmac
import secrets
from urllib.parse import quote, urlencode

SECRET_BYTES = 20  # 160 bits: la longitud que recomienda RFC 4226 §4 (R6)
DIGITS = 6
PERIOD = 30  # segundos de un paso
WINDOW = 1  # pasos a cada lado del actual que se aceptan: hasta 30 segundos de desfase


def new_secret() -> bytes:
    return secrets.token_bytes(SECRET_BYTES)


def as_text(secret: bytes) -> str:
    """El secreto como lo teclea una persona en su aplicación: base32, sin relleno."""
    return base64.b32encode(secret).decode().rstrip("=")


def uri(secret: bytes, *, issuer: str, account: str) -> str:
    """La URI `otpauth://` que una aplicación de autenticación lee de un código QR. Lleva el
    secreto: se enseña una vez, en el alta, y no se registra."""
    label = quote(f"{issuer}:{account}", safe="")
    query = {
        "secret": as_text(secret),
        "issuer": issuer,
        "algorithm": "SHA1",
        "digits": DIGITS,
        "period": PERIOD,
    }
    return f"otpauth://totp/{label}?{urlencode(query, quote_via=quote)}"


def step_at(seconds: float) -> int:
    """El paso de tiempo de ese instante (segundos desde 1970, UTC)."""
    return int(seconds // PERIOD)


def code(secret: bytes, step: int) -> str:
    """El código de ese paso: HOTP (RFC 4226 §5.3) con el paso como contador."""
    digest = hmac.new(secret, step.to_bytes(8, "big"), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    number = int.from_bytes(digest[offset : offset + 4], "big") & 0x7FFFFFFF
    return str(number % 10**DIGITS).zfill(DIGITS)


def accepted_step(secret: bytes, presented: str, *, now: float, last: int | None) -> int | None:
    """El paso cuyo código es `presented`, si vale; si no, `None`, sin decir por qué.

    Vale el código del paso actual, del anterior o del siguiente, y solo de un paso posterior a
    `last`, el último que se aceptó: el código de un paso ya usado, o de uno anterior, no
    vale aunque sea correcto (ADR-023 §2). Quien llama guarda el paso devuelto como el nuevo
    `last`, en la misma transacción en que da el código por bueno.

    Si dos pasos de la ventana dan el mismo código (pasa en uno de cada millón de pares),
    devuelve el último: con el primero como `last`, el mismo código valdría otra vez por el otro.

    Compara en tiempo constante y con todos los pasos de la ventana, sin salir al encontrar el
    que coincide. Lo que no son seis dígitos ASCII no vale.
    """
    shaped = len(presented) == DIGITS and presented.isascii() and presented.isdigit()
    given = presented.encode() if shaped else b"\x00" * DIGITS  # se compara igual: mismo tiempo
    current = step_at(now)
    found: int | None = None
    for step in range(current - WINDOW, current + WINDOW + 1):
        same = hmac.compare_digest(code(secret, step).encode(), given) if step >= 0 else False
        if same and shaped and (last is None or step > last):
            found = step  # sin `break` y sin quedarse con el primero: gana el último paso
    return found
