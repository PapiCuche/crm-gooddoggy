"""Step-up: la comprobación «segundo factor verificado hace 15 minutos como mucho» (ADR-003 §4,
ADR-023 §7).

La prueba es `mfa_at` en la **sesión** (segundos Unix), tras la cookie HttpOnly: la escriben
solo el segundo paso del acceso, el alta del factor y la ruta del step-up; una sesión nueva no
la hereda. Una marca del futuro, o que no es un entero, no vale. Las vistas de tenant la piden
con `step_up_methods` (`HasPermission`); `require_recent_mfa` es la comprobación suelta.
"""

import math
import time
from typing import Any

from core.api.errors import ApiError

MFA_AT = "mfa_at"  # en la sesión: cuándo se verificó el segundo factor
MAX_AGE = 15 * 60  # segundos (ADR-003 §4)
STEP_UP_REQUIRED = "STEP_UP_REQUIRED"
# Una sola frase para quien no verificó y para quien no tiene factor: la respuesta no dice cuál.
MESSAGE = (
    "Esta acción pide verificar el segundo factor con un código reciente. Si tu cuenta aún "
    "no lo tiene, actívalo primero en la seguridad de tu cuenta."
)


def age(session: Any, *, now: float | None = None) -> float | None:
    """Segundos desde que se verificó el segundo factor en `session`; `None` si no consta, no es
    un entero o está en el futuro."""
    at = session.get(MFA_AT)
    if type(at) is not int:  # `True` es un `int`: tampoco
        return None
    elapsed = (time.time() if now is None else now) - at
    return elapsed if elapsed >= 0 else None


def seconds_left(session: Any, max_age: int = MAX_AGE, *, now: float | None = None) -> int | None:
    """Segundos que le quedan a la verificación de `session`, redondeados hacia arriba (0 solo en
    el instante en que vence); `None` si no hay una vigente."""
    elapsed = age(session, now=now)
    if elapsed is None or elapsed > max_age:
        return None
    return math.ceil(max_age - elapsed)


def require_recent_mfa(request: Any, max_age: int = MAX_AGE) -> None:
    """Deja pasar si la sesión de `request` verificó el segundo factor hace `max_age` segundos
    como mucho; si no, `ApiError` 403 `STEP_UP_REQUIRED`."""
    if seconds_left(request.session, max_age) is None:
        raise ApiError(STEP_UP_REQUIRED, 403, MESSAGE)
