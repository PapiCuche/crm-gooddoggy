"""Validación de la configuración de `accounts` al cargar la aplicación."""

from django.conf import settings

from apps.accounts.throttle import SCOPES

LONGEST = 31 * 24 * 60 * 60  # ninguna espera razonable pasa de esto
MOST, SHORTEST = 1000, 60  # más intentos, o una ventana de calma menor, apagan el límite
CODES, CODES_WINDOW = 10, 24 * 60 * 60  # ADR-023 §5: el ajuste solo puede endurecerlo


def login_throttle() -> str | None:
    """`LOGIN_THROTTLE` es configuración: un valor mal escrito no puede apagar un límite ni
    romper el login. Devuelve el motivo por el que no vale, o `None`."""
    limits = getattr(settings, "LOGIN_THROTTLE", None)
    if not isinstance(limits, dict) or set(limits) != set(SCOPES):
        return f"debe tener exactamente estas claves: {', '.join(SCOPES)}"
    for scope, values in limits.items():
        whole = isinstance(values, tuple) and len(values) == 4
        if not whole or any(type(value) is not int or not 0 < value <= LONGEST for value in values):
            return f"{scope} debe ser una tupla de cuatro enteros entre 1 y {LONGEST}"
        if values[0] > MOST or values[1] < SHORTEST:  # p. ej. intentos y ventana intercambiados
            return f"{scope}: hasta {MOST} intentos y una ventana de {SHORTEST} s o más"
        if values[2] > values[3]:
            return f"{scope}: el primer bloqueo no puede superar el máximo"
    if any(limits["identifier"][index] < limits["pair"][index] for index in (2, 3)):
        return "la espera de identifier no puede ser menor que la de pair"
    return None


def mfa_code_throttle() -> str | None:
    """`MFA_CODE_THROTTLE` es configuración: un valor mal escrito no puede dejar probar más
    códigos del segundo factor que ADR-023 §5. Devuelve el motivo por el que no vale, o `None`."""
    values = getattr(settings, "MFA_CODE_THROTTLE", None)
    if not isinstance(values, tuple) or [type(value) for value in values] != [int, int]:
        return "debe ser una tupla de dos enteros: intentos y ventana en segundos"
    if not 0 < values[0] <= CODES or not CODES_WINDOW <= values[1] <= LONGEST:
        return f"de 1 a {CODES} intentos y una ventana de {CODES_WINDOW} a {LONGEST} s"
    return None
