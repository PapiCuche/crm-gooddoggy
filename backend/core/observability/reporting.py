"""Reporte de errores desacoplado (ADR-011 §3): `ErrorReporter` con `NoopReporter` (sin
`SENTRY_DSN`: sin red) o `SentryReporter`. El dominio nunca importa `sentry_sdk`.

Sentry: sin PII por defecto, sin tracing ni profiling, sin variables locales ni cuerpos, y
`before_send = scrub_sentry_event` siempre (reglas propias + `core.redaction`). Los logs no
generan eventos (evita la doble captura con las integraciones de Django y Celery).
"""

from collections.abc import Mapping, Sequence
from functools import cache
from typing import Any, Protocol

from django.conf import settings

from core.observability.context import current_correlation_id, current_request_id
from core.redaction import redact, redact_text
from core.tenancy.context import current

SAFE_KEYS = ("request_id", "correlation_id", "organization_id", "user_id", "actor_type")
_KEEP_HEADERS = frozenset({"host", "user-agent", "content-type", "content-length", "accept"})


class ErrorReporter(Protocol):
    def capture_exception(
        self, exception: BaseException, context: Mapping[str, Any] | None = None
    ) -> None: ...

    def capture_message(
        self,
        message: str,
        context: Mapping[str, Any] | None = None,
        fingerprint: Sequence[str] | None = None,
    ) -> None: ...


class NoopReporter:
    def capture_exception(
        self, exception: BaseException, context: Mapping[str, Any] | None = None
    ) -> None:
        pass

    def capture_message(
        self,
        message: str,
        context: Mapping[str, Any] | None = None,
        fingerprint: Sequence[str] | None = None,
    ) -> None:
        pass


def safe_ids() -> dict[str, str]:
    """Solo identificadores técnicos: nunca el TenantContext, la petición ni modelos."""
    ctx = current()
    ids = {
        "request_id": current_request_id(),
        "correlation_id": current_correlation_id() or (ctx.correlation_id if ctx else None),
        "organization_id": str(ctx.organization_id) if ctx else None,
        "user_id": str(ctx.user_id) if ctx and ctx.user_id else None,
        "actor_type": str(ctx.actor_type) if ctx else None,
    }
    return {k: v for k, v in ids.items() if v}


def scrub_sentry_event(event: dict[str, Any], hint: Any = None) -> dict[str, Any]:
    request = event.get("request")
    if isinstance(request, dict):
        for key in ("data", "cookies", "query_string", "env"):
            request.pop(key, None)
        headers = request.get("headers") or {}
        request["headers"] = {k: v for k, v in headers.items() if k.lower() in _KEEP_HEADERS}
    user = event.get("user")
    event["user"] = {"id": user["id"]} if isinstance(user, dict) and user.get("id") else {}
    for value in (event.get("exception") or {}).get("values") or []:
        value["value"] = redact_text(str(value.get("value") or ""))
        for frame in (value.get("stacktrace") or {}).get("frames") or []:
            frame.pop("vars", None)
    event["tags"] = {**(event.get("tags") or {}), **safe_ids()}
    return redact(event)  # type: ignore[no-any-return]


def scrub_breadcrumb(crumb: dict[str, Any], hint: Any = None) -> dict[str, Any]:
    """Las migas de pan de Sentry (`http.client`, `urllib3`) llevan la URL con su query: la de una
    URL firmada de un medio (`oh=`, `oe=`) es una credencial. Se quita la query y el fragmento."""
    data = crumb.get("data")
    if isinstance(data, dict):
        for key in ("http.query", "http.fragment"):
            data.pop(key, None)
        if isinstance(data.get("url"), str):
            data["url"] = data["url"].split("#", 1)[0].split("?", 1)[0]
    return redact(crumb)  # type: ignore[no-any-return]


class SentryReporter:
    def __init__(self, dsn: str, environment: str | None = None) -> None:
        import sentry_sdk
        from sentry_sdk.integrations.celery import CeleryIntegration
        from sentry_sdk.integrations.django import DjangoIntegration
        from sentry_sdk.integrations.logging import LoggingIntegration

        self._sdk = sentry_sdk
        sentry_sdk.init(
            dsn=dsn,
            environment=environment,
            send_default_pii=False,
            traces_sample_rate=0.0,
            include_local_variables=False,
            max_request_body_size="never",
            auto_enabling_integrations=False,
            integrations=[
                DjangoIntegration(),
                CeleryIntegration(),
                LoggingIntegration(level=None, event_level=None),
            ],
            before_send=scrub_sentry_event,  # type: ignore[arg-type]
            before_breadcrumb=scrub_breadcrumb,
        )

    def capture_exception(
        self, exception: BaseException, context: Mapping[str, Any] | None = None
    ) -> None:
        with self._sdk.new_scope() as scope:
            scope.set_context("crm", redact(dict(context or {})))
            self._sdk.capture_exception(exception)

    def capture_message(
        self,
        message: str,
        context: Mapping[str, Any] | None = None,
        fingerprint: Sequence[str] | None = None,
    ) -> None:
        with self._sdk.new_scope() as scope:
            scope.set_context("crm", redact(dict(context or {})))
            if fingerprint:  # la misma huella agrupa el mismo aviso (F7-11a)
                scope.fingerprint = list(fingerprint)
            self._sdk.capture_message(redact_text(message))


@cache
def reporter() -> ErrorReporter:
    dsn = getattr(settings, "SENTRY_DSN", "")
    if not dsn:
        return NoopReporter()
    return SentryReporter(dsn, getattr(settings, "SENTRY_ENVIRONMENT", None) or None)
