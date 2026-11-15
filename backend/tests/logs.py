"""Lo que quedó en el log, para comprobar que un dato privado no llegó a él."""

import json
import logging

import pytest

from core.observability.logging import json_formatter

# Dónde y cuándo se emitió (la ruta del checkout, el proceso): no es del evento y no lleva datos.
# Con ellos, un checkout cuya ruta contenga el texto buscado haría fallar el test (F3-38, #365).
PLACE = {"pathname", "filename", "module", "funcName", "lineno", "created", "msecs",
         "relativeCreated", "thread", "threadName", "process", "processName",
         "taskName", "asctime"}  # fmt: skip
# Lo que el pipeline de logs añade a cada evento: la hora y la petición. Cambian en cada
# ejecución y son cifras: un número corto que se busca (un puerto, un código TOTP) cae dentro
# por azar, como `6379` en `13:15:41.826379Z` (F3-38b, #823). No llevan datos del evento.
STAMPS = {"timestamp", "request_id", "correlation_id"}


def _fields(record: logging.LogRecord) -> str:
    """Todos los campos del registro (el mensaje ya interpolado, sus argumentos, los extra y la
    excepción) menos los del lugar; de un evento de structlog, también sin sus sellos."""
    fields = {k: v for k, v in record.__dict__.items() if k not in PLACE}
    if isinstance(record.msg, dict):  # structlog: el evento entero, con su hora y su petición
        fields["msg"] = {k: v for k, v in record.msg.items() if k not in STAMPS}
        fields.pop("message", None)  # el mismo diccionario, con los sellos
    else:
        fields["message"] = record.getMessage()
    return str(fields)


def log_text(caplog: pytest.LogCaptureFixture) -> str:
    """Lo que dijo cada registro, sin el lugar ni la hora. No la línea formateada: `caplog.text`
    lleva el archivo y la línea y, en un evento de structlog, su hora y su petición."""
    return " ".join(_fields(record) for record in caplog.records)


def json_lines(caplog: pytest.LogCaptureFixture) -> str:
    """Cada registro como la línea JSON que llega a stdout (`json_formatter`, ya redactada),
    sin los sellos de hora y de petición."""
    formatter = json_formatter()
    lines = (json.loads(formatter.format(record)) for record in caplog.records)
    return "\n".join(json.dumps({k: v for k, v in ln.items() if k not in STAMPS}) for ln in lines)
