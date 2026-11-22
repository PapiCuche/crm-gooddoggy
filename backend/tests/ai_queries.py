"""Las consultas de una invocación de la IA (ADR-034 §2, §11): import-linter no ve una escritura
directa con el ORM ni SQL, así que se capturan todas y se buscan las que escriben, bloquean o
cambian la sesión. F8-07 no admite ninguna; F8-08 admitirá solo las de `ai_*` y `audit_logs`.

Desde F8-08c las tools corren en una transacción de solo lectura (`registry.read_only_scope`, el
mismo camino en las pruebas que en el copiloto): las escrituras de datos, `nextval` y los objetos
grandes los rechaza PostgreSQL, se escriban como se escriban. Esta búsqueda queda para lo que esa
transacción admite: candados consultivos, `set_config` de sesión, `LISTEN`… y lo que la
terminaría (`COMMIT`, `ROLLBACK`, `BEGIN`…). Lo que escribe se sigue marcando: otra barrera.

Qué no ve (F8-07e): lo que corre dentro del servidor (funciones, disparadores, reglas), otros
procesos (los workers de Celery) y lo que se manda a libpq sin un cursor de psycopg. Ante la duda
marca: un `set_config` cuyo tercer argumento no es el literal `true` (también un parámetro que
el servidor recibe aparte, `$1`) y las palabras dentro de un cuerpo `$$…$$`."""

import re
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from psycopg._cursor_base import BaseCursor

WRITES = re.compile(
    r"\b(insert|update|delete|merge|truncate|copy|lock|create|alter|drop|grant|revoke|call|into"
    r"|nextval|setval|refresh|vacuum|reindex|cluster|notify|pg_notify|do|checkpoint|prepare"
    r"|execute|pg_terminate_backend|pg_cancel_backend|unicode_escaped_identifier"
    r"|lo_(create|creat|unlink|import|export|from_bytea|put|truncate|write))\b"
    r"|\bfor\s+(key\s+)?share\b|\bpg_\w*advisory|\bcomment\s+on\b|\bsecurity\s+label\b"
    r"|\bset_config\s*\((?![^()]*,[^(),]*,\s*true\s*\))"  # sin `true`: queda en la sesión
    # al empezar una sentencia: lo que cambia la sesión (`SET LOCAL` y los de la transacción, no)
    r"|(^|;)\s*(set\b(?!\s+(local|transaction|constraints)\b)|reset\b|discard\b|listen\b"
    # y lo que termina la transacción de solo lectura (`ROLLBACK TO` un savepoint, no)
    r"|commit\b|end\b|abort\b|begin\b|start\b|rollback\b(?!\s+((work|transaction)\s+)?to\b))",
    re.IGNORECASE,
)  # `update` también caza `FOR UPDATE` y `FOR NO KEY UPDATE`; `into`, `SELECT … INTO`
LITERAL = re.compile(r"\b[Ee]'(?:[^'\\]|\\.|'')*'|'(?:[^']|'')*'")  # un texto no es SQL
# Antes de buscar, de izquierda a derecha (manda lo que abre primero): los textos, también
# `$tag$…$tag$` (no el `$` dentro de un nombre, `a$b$`), salen; los comentarios son un espacio
# (`FOR/**/SHARE` es `FOR SHARE`); un identificador entre comillas queda en sus palabras, sin
# comillas (`"set_config"(…)` es `set_config(…)`); y uno `U&"…"`, que puede esconder cualquier
# nombre con escapes y nunca lo genera el ORM, se marca.
NOISE = re.compile(
    LITERAL.pattern + r"|(?<![\w$])\$([^\W\d]\w*|)\$.*?\$\1\$"
    r'|(?:\b[Uu]&)?"(?:[^"]|"")*"|/\*.*?\*/|--[^\n]*',
    re.DOTALL,
)
ESCAPED = " unicode_escaped_identifier "  # lo que `WRITES` marca en lugar de un `U&"…"`


def _quiet(found: re.Match[str]) -> str:
    text = found[0]
    if text[0] in "Uu":
        return ESCAPED
    if text[0] == '"':
        return " " + re.sub(r"\W", " ", text[1:-1]) + " "
    return " " if text[0] in "/-" else "''"


def _bare(statement: str) -> str:
    return NOISE.sub(_quiet, statement)


def writes(statements: list[str]) -> list[str]:
    return [stmt for stmt in statements if WRITES.search(_bare(stmt))]


@contextmanager
def captured() -> Iterator[list[str]]:
    """Cada sentencia que psycopg prepara en este proceso, en cualquier conexión, alias o hilo,
    como la recibe el servidor (con los parámetros que el cursor de cliente ya puso dentro): las
    del ORM y `executemany`, y también las de un cursor directo del driver, `copy`, `stream`, los
    cursores con nombre, `sql.Composed` y las t-strings (todas pasan por
    `BaseCursor._convert_query`)."""
    seen: list[str] = []
    convert = BaseCursor._convert_query

    def record(cursor: Any, *args: Any, **kwargs: Any) -> Any:
        converted = convert(cursor, *args, **kwargs)
        seen.append(bytes(converted.query).decode(errors="replace"))
        return converted

    BaseCursor._convert_query = record  # type: ignore[method-assign, assignment]
    try:
        yield seen
    finally:
        BaseCursor._convert_query = convert  # type: ignore[method-assign]
