"""Las consultas de una invocación de la IA (ADR-034 §2, §11): import-linter no ve una escritura
directa con el ORM ni SQL, así que se capturan todas y se buscan las que escriben o bloquean.
F8-07 no admite ninguna; F8-08 admitirá solo las de `ai_*` y `audit_logs`.

Qué no ve (F8-07e): lo que corre dentro del servidor (funciones, disparadores, reglas), otros
procesos (los workers de Celery), lo que se manda a libpq sin un cursor de psycopg y un
`set_config` cuyo tercer argumento llega como parámetro (`%s`)."""

import re
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from psycopg import sql
from psycopg._cursor_base import BaseCursor

WRITES = re.compile(
    r"\b(insert|update|delete|merge|truncate|copy|lock|create|alter|drop|grant|revoke|call|into"
    r"|nextval|setval|refresh|vacuum|reindex|cluster|notify|pg_notify|do)\b"
    r"|\bfor\s+(key\s+)?share\b|\bpg_\w*advisory|\bcomment\s+on\b"
    r"|\bset_config\s*\((?![^()]*,[^(),]*,\s*true\s*\))",  # sin `true`: queda en la sesión
    re.IGNORECASE,
)  # `update` también caza `FOR UPDATE` y `FOR NO KEY UPDATE`; `into`, `SELECT … INTO`
LITERAL = re.compile(r"'(?:[^']|'')*'")  # un texto entre comillas simples no es SQL


def writes(statements: list[str]) -> list[str]:
    return [stmt for stmt in statements if WRITES.search(LITERAL.sub("''", stmt))]


@contextmanager
def captured() -> Iterator[list[str]]:
    """Cada sentencia que psycopg prepara en este proceso, en cualquier conexión, alias o hilo:
    las del ORM y `executemany`, y también las de un cursor directo del driver, `copy`, `stream`
    y los cursores con nombre (todas pasan por `BaseCursor._convert_query`)."""
    seen: list[str] = []
    convert = BaseCursor._convert_query

    def record(cursor: Any, query: Any, *args: Any, **kwargs: Any) -> Any:
        text = query.as_string(cursor) if isinstance(query, sql.Composable) else query
        seen.append(text.decode() if isinstance(text, bytes) else str(text))
        return convert(cursor, query, *args, **kwargs)

    BaseCursor._convert_query = record  # type: ignore[method-assign, assignment]
    try:
        yield seen
    finally:
        BaseCursor._convert_query = convert  # type: ignore[method-assign]
