"""Política única de canonicalización del email de acceso (D-F2-3, docs/phases/phase-2.md).

El email canónico es la identidad del usuario: se guarda y se busca siempre en esta forma.

1. Se quita el espacio exterior.
2. La parte local se pasa a minúsculas: la identidad no distingue mayúsculas. Debe ser ASCII
   (lo exige el validador de Django); una parte local con otros caracteres se rechaza.
3. El dominio se pasa a minúsculas y, si no es ASCII, a su forma IDNA (punycode), de modo que
   las dos escrituras de un dominio internacionalizado son la misma identidad.
4. Se valida el formato. Lo vacío o inválido se rechaza, nunca se "arregla".

No hay reglas por proveedor: los puntos y los `+tag` se conservan y distinguen direcciones.
"""

from django.core.exceptions import ValidationError
from django.core.validators import validate_email

MAX_LENGTH = 254  # `users.email`


def canonical_email(raw: str) -> str:
    local, _, domain = raw.strip().rpartition("@")
    domain = domain.lower()
    if not domain.isascii():
        try:
            domain = domain.encode("idna").decode("ascii")
        except UnicodeError as error:
            raise ValidationError("Email inválido", code="invalid") from error
    canonical = f"{local.lower()}@{domain}"
    validate_email(canonical)  # ValidationError: sin @, con espacios, parte local no ASCII…
    if len(canonical) > MAX_LENGTH:  # el validador admite hasta 320; la columna, 254
        raise ValidationError("Email demasiado largo", code="invalid")
    return canonical


def presented_email(raw: str) -> str:
    """Lo que se presenta en un acceso, canónico si es un email válido: dos escrituras de la
    misma cuenta se cuentan y se auditan como una. Lo inválido se deja como llegó."""
    try:
        return canonical_email(raw)
    except ValidationError:
        return raw
