"""Logs JSON en stdout con structlog (ADR-011 §2).

structlog y el `logging` estándar (Django, Celery, librerías) comparten el mismo pipeline:
contexto → nivel/logger → timestamp UTC → IDs de tenant/actor → excepción como texto →
REDACCIÓN (`core.redaction`) → JSON. Nada llega a stdout sin pasar por el redactor.
"""

from typing import Any

import structlog
from structlog.typing import EventDict, Processor

from core.observability.context import (
    current_correlation_id,
    current_logged_tenant,
    current_request_id,
)
from core.redaction import redact, redact_text
from core.tenancy.context import current


def add_context(logger: Any, method: str, event_dict: EventDict) -> EventDict:
    ctx = current() or current_logged_tenant()  # entre los scopes del copiloto (F8-08i)
    correlation_id = current_correlation_id() or (ctx.correlation_id if ctx else None)
    event_dict.setdefault("request_id", current_request_id())
    event_dict.setdefault("correlation_id", correlation_id)
    if ctx is not None:
        event_dict.setdefault("organization_id", str(ctx.organization_id))
        event_dict.setdefault("user_id", str(ctx.user_id) if ctx.user_id else None)
        event_dict.setdefault("actor_type", str(ctx.actor_type))
    return event_dict


def redact_event(logger: Any, method: str, event_dict: EventDict) -> EventDict:
    return redact(event_dict)  # type: ignore[no-any-return]


def redact_args(logger: Any, method: str, event_dict: EventDict) -> EventDict:
    """Antes de interpolar `%s`: un dict posicional conserva sus reglas por clave."""
    args = event_dict.get("positional_args")
    if args:  # stdlib guarda un único dict posicional como el propio dict
        event_dict["positional_args"] = tuple(
            redact([args] if isinstance(args, dict) else list(args))
        )
    return event_dict


def safe_default(value: Any) -> str:
    """Objetos no serializables (excepciones, modelos…): su texto también pasa por el redactor."""
    return redact_text(str(value))


SHARED: list[Processor] = [
    structlog.stdlib.add_log_level,
    structlog.stdlib.add_logger_name,
    redact_args,
    structlog.stdlib.PositionalArgumentsFormatter(),
    structlog.processors.TimeStamper(fmt="iso", utc=True),
    add_context,
]


def json_formatter() -> structlog.stdlib.ProcessorFormatter:
    """Formatter del handler de consola (LOGGING de Django): también para logs de stdlib."""
    return structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=SHARED,
        pass_foreign_args=True,  # stdlib: args sin interpolar, para redactarlos antes
        use_get_message=False,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.format_exc_info,  # str(excepción) se redacta después
            redact_event,
            structlog.processors.JSONRenderer(default=safe_default),
        ],
    )


def configure() -> None:
    structlog.configure(
        processors=[*SHARED, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )


get_logger = structlog.stdlib.get_logger
