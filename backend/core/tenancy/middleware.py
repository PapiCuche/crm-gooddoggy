"""Rutas de tenant HTTP `/api/v1/o/{slug}/…` (tenancy-context §2)."""

import re
from collections.abc import Callable

from django.db import transaction
from django.http import HttpRequest, HttpResponseBase, JsonResponse

from core.api import AUTH_SCHEME
from core.api.errors import NOT_AUTHENTICATED, NOT_FOUND, error_response
from core.observability.context import current_correlation_id
from core.tenancy.context import TenantContextError
from core.tenancy.resolution import OrganizationSuspended, TenantNotFound, resolve_tenant
from core.tenancy.scope import assert_clean_connection, tenant_scope
from core.tenancy.second_factor import MESSAGE, MFA_REQUIRED, SecondFactorRequired

TENANT_PREFIX = "/api/v1/o/"
TENANT_PATH = re.compile(r"^/api/v1/o/(?P<slug>[-a-zA-Z0-9_]+)/")


def not_found() -> JsonResponse:
    """404 idéntico para organización inexistente, sin membresía o recurso de otro tenant."""
    return error_response(NOT_FOUND, 404)


class TenantResolutionMiddleware:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponseBase]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponseBase:
        match = TENANT_PATH.match(request.path_info)
        if match is None:
            if request.path_info.startswith(TENANT_PREFIX):
                return not_found()  # un slug imposible nunca llega a una vista sin contexto
            return self.get_response(request)  # auth/plataforma: sin contexto de tenant
        user = getattr(request, "user", None)
        if user is None or not getattr(user, "is_authenticated", True):
            unauthenticated = error_response(NOT_AUTHENTICATED, 401)
            unauthenticated.headers["WWW-Authenticate"] = AUTH_SCHEME  # como el 401 de DRF
            return unauthenticated
        assert_clean_connection()
        try:
            ctx = resolve_tenant(match["slug"], user, "http", current_correlation_id())
        except TenantNotFound:
            return not_found()
        except OrganizationSuspended:
            return error_response("ORG_SUSPENDED", 403)
        except SecondFactorRequired:  # ADR-023 §9 con `enforce`: ninguna ruta de tenant se abre
            return error_response(MFA_REQUIRED, 403, MESSAGE)
        request.tenant = ctx  # type: ignore[attr-defined]
        with tenant_scope(ctx):  # toda la vista y su serialización en una transacción
            response = self.get_response(request)
            if response.streaming:
                raise TenantContextError("Streaming prohibido en rutas de tenant (usar Celery)")
            if response.status_code >= 400:
                # Una petición que acaba en error no deja nada escrito: el scope es dueño de
                # la transacción y la deshace, lo haya convertido quien lo haya convertido.
                transaction.set_rollback(True)
            return response
