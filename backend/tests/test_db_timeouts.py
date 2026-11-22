"""F8-13e (#1086): `core.db.timeouts`. Un tope nunca es `0ms` (en PostgreSQL, «sin tope») y
`within` acota **el conjunto** de sentencias de un bloque: cada una sale con lo que queda del plazo
y, sin tiempo, ni sale. Con PostgreSQL real como `crm_app`; sin datos."""

import time
from typing import Any

import pytest
from django.db import OperationalError, connection, transaction

from core.db import timeouts

pytestmark = pytest.mark.usefixtures("tenant_db")
SHOW = "SELECT current_setting('statement_timeout')"


class Stop(BaseException):  # no es `Exception`: también así se quita el tope
    pass


@pytest.mark.parametrize(
    ("seconds", "text"),
    [(0, "1ms"), (0.0004, "1ms"), (-5, "1ms"), (0.001, "1ms"), (0.0025, "2ms"), (3, "3000ms")],
)
def test_a_limit_is_never_zero_which_would_mean_none(seconds: float, text: str) -> None:
    assert timeouts.ms(seconds) == text


def ms(text: str) -> int:
    return (
        int(text.removesuffix("ms")) if text.endswith("ms") else int(text.removesuffix("s")) * 1000
    )


def test_each_statement_gets_what_is_left_and_nothing_stays_after_the_block() -> None:
    with connection.cursor() as cursor:
        cursor.execute(SHOW)
        [(before,)] = cursor.fetchall()
    seen: list[int] = []
    with timeouts.within(time.monotonic() + 2), transaction.atomic():
        with connection.cursor() as cursor:
            for _ in range(3):
                cursor.execute(SHOW)  # el valor que tenía la propia sentencia
                seen.append(ms(cursor.fetchone()[0]))
                cursor.execute("SELECT pg_sleep(0.1)")
    assert 1700 < seen[0] <= 2000 and seen[0] > seen[1] > seen[2] and seen[0] - seen[2] >= 180
    assert connection.execute_wrappers == []
    with connection.cursor() as cursor:  # la misma conexión: el tope murió con la transacción
        cursor.execute(SHOW)
        assert cursor.fetchone()[0] == before


def test_the_sum_is_bounded_even_if_each_statement_is_short() -> None:
    """Cinco sentencias de 0,4 s con un plazo de 1 s: la tercera se cancela a tiempo (con un tope
    por sentencia de 1 s, las cinco pasarían en 2 s)."""
    started = time.monotonic()
    with pytest.raises(OperationalError) as error:
        with timeouts.within(started + 1), transaction.atomic():
            with connection.cursor() as cursor:
                for _ in range(5):
                    cursor.execute("SELECT pg_sleep(0.4)")
    took = time.monotonic() - started
    assert error.value.__cause__.sqlstate == "57014" and 0.9 < took < 1.5  # type: ignore[union-attr]
    assert connection.execute_wrappers == []


def test_without_time_left_the_statement_never_leaves() -> None:
    sent: list[str] = []

    def record(execute: Any, sql: str, params: Any, many: bool, context: Any) -> Any:
        sent.append(sql)
        return execute(sql, params, many, context)

    with pytest.raises(timeouts.Expired):
        with timeouts.within(time.monotonic() - 0.001), connection.execute_wrapper(record):
            with transaction.atomic(), connection.cursor() as cursor:
                cursor.execute("SELECT 1")
    assert sent == [] and connection.execute_wrappers == []


def test_the_limit_goes_away_whatever_stops_the_block() -> None:
    with pytest.raises(Stop):
        with timeouts.within(time.monotonic() + 5), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(SHOW)
                assert ms(cursor.fetchone()[0]) <= 5000
            raise Stop
    assert connection.execute_wrappers == []
    with connection.cursor() as cursor:
        cursor.execute(SHOW)
        assert cursor.fetchone()[0] in {"0", "0ms"}
