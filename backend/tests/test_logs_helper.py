"""F3-38b (#823): `tests.logs` deja fuera del log la hora y la petición, y nada más. Un dato
privado en el mensaje, sus argumentos, un campo extra, un campo de structlog o la excepción
sigue apareciendo; un número que solo está en la hora o en el ID de la petición, no."""

import datetime as dt
import json
import logging
from types import SimpleNamespace
from typing import Any

import pytest
import structlog

from core.observability import logging as observability
from tests.logs import json_lines, log_text

STAMP = dt.datetime(2026, 11, 13, 13, 15, 41, 826379, tzinfo=dt.UTC)  # la del CI de #818
REQUEST = "01a1e00f-6379-7356-8269-10c94e1ece2b"
PORT = "6379"


class Stamped(dt.datetime):
    @classmethod
    def now(cls, tz: dt.tzinfo | None = None) -> Stamped:
        return cls.fromisoformat(STAMP.isoformat())


def test_a_number_only_in_the_stamps_is_not_in_the_log(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = SimpleNamespace(datetime=Stamped, timezone=dt.timezone)  # el de `TimeStamper`
    monkeypatch.setattr(structlog.processors, "datetime", clock)
    monkeypatch.setattr(observability, "current_request_id", lambda: REQUEST)
    caplog.set_level(logging.INFO)
    structlog.get_logger("core.http").error("http.request.failed", status_code=503)
    logging.getLogger("core.health").warning("readiness: %s no disponible", "Redis")
    assert "41.826379Z" in caplog.text and REQUEST in caplog.text  # la línea entera, sí
    assert PORT not in log_text(caplog) and PORT not in json_lines(caplog)
    assert "http.request.failed" in log_text(caplog) and "Redis" in log_text(caplog)
    lines = [json.loads(line) for line in json_lines(caplog).splitlines()]
    assert [(line["event"], line["level"]) for line in lines] == [
        ("http.request.failed", "error"),
        ("readiness: Redis no disponible", "warning"),
    ]
    assert lines[0]["status_code"] == 503
    assert not any({"timestamp", "request_id", "correlation_id"} & set(line) for line in lines)


@pytest.mark.parametrize(
    ("emit", "in_stdout"),
    [
        (lambda: logging.getLogger("tests").warning("conectando a broker:%s", PORT), True),
        # Un `extra` de stdlib no llega a la línea JSON (`json_formatter` no lo añade).
        (lambda: logging.getLogger("tests").warning("ready", extra={"port": int(PORT)}), False),
        (lambda: structlog.get_logger("tests").warning("readiness", port=int(PORT)), True),
        (lambda: structlog.get_logger("tests").warning(f"redis://broker:{PORT}/0"), True),
    ],
    ids=["args", "extra", "structlog-field", "structlog-event"],
)
def test_a_number_in_what_the_event_says_is_still_in_the_log(
    caplog: pytest.LogCaptureFixture, emit: Any, in_stdout: bool
) -> None:
    caplog.set_level(logging.INFO)
    emit()
    assert PORT in log_text(caplog) and (PORT in json_lines(caplog)) is in_stdout


def test_the_text_of_an_exception_is_still_in_the_log(caplog: pytest.LogCaptureFixture) -> None:
    try:
        raise ConnectionError(f"Error 111 connecting to broker.internal:{PORT}")
    except ConnectionError:
        logging.getLogger("tests").exception("readiness")
    assert PORT in log_text(caplog)  # el texto de la excepción no es un sello
