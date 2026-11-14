"""Lo que quedó en el log, para comprobar que un dato privado no llegó a él."""

import pytest

# Dónde y cuándo se emitió (la ruta del checkout, el proceso): no es del evento y no lleva datos.
# Con ellos, un checkout cuya ruta contenga el texto buscado haría fallar el test (F3-38, #365).
PLACE = {"pathname", "filename", "module", "funcName", "lineno", "created", "msecs",
         "relativeCreated", "thread", "threadName", "process", "processName",
         "taskName"}  # fmt: skip


def log_text(caplog: pytest.LogCaptureFixture) -> str:
    """Las líneas formateadas y todos los campos de cada registro, menos los del lugar."""
    fields = ({k: v for k, v in r.__dict__.items() if k not in PLACE} for r in caplog.records)
    return caplog.text + " ".join(str(f) for f in fields)
