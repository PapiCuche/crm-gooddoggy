"""Topes de tiempo de PostgreSQL de una transacción: `set_config(..., true)` (como `SET LOCAL`,
mueren con ella), nunca de sesión (F8-13e)."""

import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from django.db import connections

LIMIT = "SELECT set_config('statement_timeout', %s, true)"


class Expired(Exception):  # noqa: N818 — no es un fallo de la base: se acabó el plazo
    """El plazo se acabó antes de una sentencia: no se envía."""


def ms(seconds: float) -> str:
    """Un tope de PostgreSQL de al menos 1 ms: `0ms` sería «sin tope»."""
    return f"{max(int(seconds * 1000), 1)}ms"


@contextmanager
def within(end: float, using: str = "default") -> Iterator[None]:
    """Un plazo para todo el bloque (`end`, de `time.monotonic()`): antes de cada sentencia de este
    hilo en `using`, `statement_timeout` = lo que queda, por el cursor crudo (no vuelve a pasar por
    aquí); sin tiempo, `Expired` y la sentencia no sale. Solo dentro de una transacción (la abre
    quien llama). Al salir, aunque falle, se quita."""

    def limited(execute: Callable[..., Any], sql: str, params: Any, many: bool,
                context: dict[str, Any]) -> Any:  # fmt: skip
        left = end - time.monotonic()
        if left <= 0:
            raise Expired
        with context["connection"].wrap_database_errors:
            context["cursor"].cursor.execute(LIMIT, [ms(left)])
        return execute(sql, params, many, context)

    with connections[using].execute_wrapper(limited):
        yield
