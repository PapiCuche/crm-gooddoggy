"""Tope por cuenta de códigos fallidos del segundo factor (F2-102, ADR-023 §5).

Como mucho `MFA_CODE_THROTTLE[0]` intentos contados por cuenta en los últimos
`MFA_CODE_THROTTLE[1]` segundos, vengan de donde vengan. Lo comparten todas las rutas que
comprueban un código de un dispositivo confirmado: el segundo paso del acceso, el step-up,
quitar el factor y regenerar los códigos de respaldo.

`admit` cuenta sin preguntar quién llama. Cada ruta comprueba **antes** todo lo que no es el
código (la marca de acceso a medias o la sesión, la contraseña donde se pide, y que la cuenta
tiene un dispositivo confirmado): si no, quien no tiene nada de eso agotaría el tope de otra.

El intento se cuenta **antes** de comprobar el código (`admit`), con la fila de la cuenta
bloqueada: una ráfaga simultánea no prueba más códigos que el tope. Si el código vale, quien
llama lo devuelve (`give_back`) en la transacción que lo acepta; si no, la fila se queda y
cuenta hasta salir de la ventana. Alcanzado el tope, `admit` rechaza, también a quien traería
el código correcto, hasta que el intento más antiguo sale de la ventana: se levanta solo,
intento a intento. Cada intento es una fila con su hora (ventana deslizante, no un contador
por ventana fija: a caballo de dos ventanas fijas cabrían el doble de fallos seguidos).

Nada pone el contador a cero: tampoco cambiar la contraseña (ADR-023 §5). No guarda ni
registra ningún código. La fila de auditoría y el aviso al alcanzarse el tope son de la ruta
que lo alcanza (`Attempt.last`).
"""

from dataclasses import dataclass
from uuid import UUID

from django.conf import settings
from django.db import connection, transaction

from apps.accounts.models import User

_LOCK = "SELECT 1 FROM users WHERE id = %s FOR NO KEY UPDATE"
_SPAN = "%(window)s::int * interval '1 second'"
# `clock_timestamp()`, no `now()`: `now()` es el inicio de la transacción, anterior a la espera
# por la fila de la cuenta. La hora de un intento es la de cuando se contó.
_COUNTED = f"""
SELECT count(*),
  GREATEST(CEIL(EXTRACT(EPOCH FROM min(created_at) + {_SPAN} - clock_timestamp())), 1)::int
FROM user_mfa_code_attempts
WHERE user_id = %(user)s AND created_at > clock_timestamp() - {_SPAN}
"""  # noqa: S608 — texto fijo, sin datos
_COUNT = """
INSERT INTO user_mfa_code_attempts (user_id, created_at) VALUES (%s, clock_timestamp())
RETURNING id
"""
_PURGE = f"DELETE FROM user_mfa_code_attempts WHERE created_at <= clock_timestamp() - {_SPAN}"  # noqa: S608


@dataclass(frozen=True, slots=True)
class Attempt:
    id: UUID
    # Con este se alcanza el tope: si no se devuelve, quien llama audita y avisa. Lo es cada
    # intento admitido al que le falta uno para el tope: hasta tantas veces por ventana como el
    # tope si los fallos siguen, y también si otro que estaba en curso se devuelve después.
    last: bool


class Refused(Exception):
    """La cuenta alcanzó el tope: el intento no se evalúa ni cuenta."""

    def __init__(self, seconds: int) -> None:
        super().__init__(seconds)
        self.seconds = seconds


def admit(user_id: UUID) -> Attempt:
    """Cuenta un intento contra la cuenta `user_id` antes de que se compruebe su código.

    `Refused`, con los segundos que faltan para que el intento más antiguo salga de la ventana,
    si la cuenta ya tiene el tope; ese intento no cuenta. Se llama fuera de toda transacción:
    el intento queda contado aunque la de quien llama se deshaga. `User.DoesNotExist` si la
    cuenta no existe.
    """
    if connection.in_atomic_block:  # el intento se desharía con esa transacción
        raise RuntimeError("code_limit.admit no se llama dentro de una transacción")
    limit, window = settings.MFA_CODE_THROTTLE
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(_LOCK, [user_id])  # un intento a la vez por cuenta
        if cursor.fetchone() is None:
            raise User.DoesNotExist
        cursor.execute(_COUNTED, {"user": user_id, "window": window})
        counted, wait = cursor.fetchone()
        if counted >= limit:
            raise Refused(wait)
        cursor.execute(_COUNT, [user_id])
        return Attempt(cursor.fetchone()[0], last=counted + 1 >= limit)


def give_back(attempt: Attempt) -> None:
    """El código de este intento valió: deja de contar. En la transacción que lo acepta: si
    esa se deshace, el intento sigue contado."""
    with connection.cursor() as cursor:
        cursor.execute("DELETE FROM user_mfa_code_attempts WHERE id = %s", [attempt.id])


def purge() -> int:
    """Borra los intentos que ya salieron de la ventana: no cambia lo que haría el siguiente."""
    with connection.cursor() as cursor:
        cursor.execute(_PURGE, {"window": settings.MFA_CODE_THROTTLE[1]})
        return int(cursor.rowcount)
