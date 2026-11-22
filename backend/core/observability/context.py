"""Contexto de observabilidad: `request_id` (una petición HTTP) y `correlation_id` (cadena de
trabajo entre procesos). Fuente de verdad propia: structlog lo consume, no lo posee.

`logged_tenant` (F8-08i): en una petición de tenant sin el scope de la petición (las del copiloto,
tenancy-context §2), los logs y los eventos de Sentry de **entre** sus scopes cortos llevan igual
la organización y la persona. Solo para observar: no abre ningún scope ni autoriza nada; dentro
de un `tenant_scope` manda el suyo.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token

from core.tenancy.context import TenantContext

_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)
_correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)
_tenant: ContextVar[TenantContext | None] = ContextVar("logged_tenant", default=None)
Tokens = tuple[Token[str | None], Token[str | None]]


def current_request_id() -> str | None:
    return _request_id.get()


def current_correlation_id() -> str | None:
    return _correlation_id.get()


def bind(*, request_id: str | None, correlation_id: str | None) -> Tokens:
    """Fija AMBOS valores (un `None` también oculta el anterior). Deshacer con `reset()`."""
    return _request_id.set(request_id), _correlation_id.set(correlation_id)


def reset(tokens: Tokens) -> None:
    _request_id.reset(tokens[0])
    _correlation_id.reset(tokens[1])


@contextmanager
def bound(*, request_id: str | None, correlation_id: str | None) -> Iterator[None]:
    tokens = bind(request_id=request_id, correlation_id=correlation_id)
    try:
        yield
    finally:
        reset(tokens)


def current_logged_tenant() -> TenantContext | None:
    return _tenant.get()


@contextmanager
def logged_tenant(ctx: TenantContext) -> Iterator[None]:
    token = _tenant.set(ctx)
    try:
        yield
    finally:
        _tenant.reset(token)
