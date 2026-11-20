"""Las consultas de una invocación de la IA (ADR-034 §2, §11): import-linter no ve una escritura
directa con el ORM ni SQL, así que se capturan todas y se buscan las que escriben o bloquean.
F8-07 no admite ninguna; F8-08 admitirá solo las de `ai_*` y `audit_logs`."""

import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from django.db import connection

WRITES = re.compile(
    r"\b(insert|update|delete|merge|truncate|copy|lock|create|alter|drop|grant|revoke|call)\b",
    re.IGNORECASE,
)  # `update` también caza `FOR UPDATE` y `FOR NO KEY UPDATE`
LITERAL = re.compile(r"'(?:[^']|'')*'")  # un texto entre comillas simples no es SQL


def writes(statements: list[str]) -> list[str]:
    return [sql for sql in statements if WRITES.search(LITERAL.sub("''", sql))]


@contextmanager
def captured() -> Iterator[list[str]]:
    """Cada sentencia que llega a la conexión, también las de `executemany`."""
    seen: list[str] = []

    def record(execute: Callable[..., Any], sql: str, *args: Any) -> Any:
        seen.append(str(sql))
        return execute(sql, *args)

    with connection.execute_wrapper(record):
        yield seen
