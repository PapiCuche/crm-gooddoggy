"""Cuerpo de error único de la API (ADR-014 §1): `{"code", "message"?, "fields"?}`.

`code` es lo único que el cliente interpreta. Los 401 y los 404 van sin `message`: así un 404
de una ruta de tenant es idéntico venga de donde venga (ADR-001 §5.6).
"""

from typing import Any

from django.core.exceptions import ObjectDoesNotExist
from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.http import Http404, JsonResponse
from rest_framework import exceptions
from rest_framework.response import Response

from core.api import AUTH_SCHEME
from core.tenancy.context import current as current_tenant

NOT_FOUND = "NOT_FOUND"
NOT_AUTHENTICATED = "NOT_AUTHENTICATED"
PERMISSION_DENIED = "PERMISSION_DENIED"
CSRF_FAILED = "CSRF_FAILED"
INTERNAL_ERROR = "INTERNAL_ERROR"
# Respuestas que Django genera fuera de una vista, por código de estado.
BY_STATUS = {
    400: "BAD_REQUEST",
    401: NOT_AUTHENTICATED,
    403: PERMISSION_DENIED,
    404: NOT_FOUND,
    405: "METHOD_NOT_ALLOWED",
    406: "NOT_ACCEPTABLE",
    415: "UNSUPPORTED_MEDIA_TYPE",
    429: "RATE_LIMITED",
}
_BY_EXCEPTION: tuple[tuple[type[exceptions.APIException], str], ...] = (
    (exceptions.ValidationError, "VALIDATION_ERROR"),
    (exceptions.ParseError, "PARSE_ERROR"),
    (exceptions.NotAuthenticated, NOT_AUTHENTICATED),
    (exceptions.AuthenticationFailed, NOT_AUTHENTICATED),
    (exceptions.PermissionDenied, PERMISSION_DENIED),
    (exceptions.NotFound, NOT_FOUND),
    (exceptions.MethodNotAllowed, "METHOD_NOT_ALLOWED"),
    (exceptions.NotAcceptable, "NOT_ACCEPTABLE"),
    (exceptions.UnsupportedMediaType, "UNSUPPORTED_MEDIA_TYPE"),
    (exceptions.Throttled, "RATE_LIMITED"),
)
_JSON = {"ensure_ascii": False, "separators": (",", ":")}  # como el JSONRenderer de DRF


def body(code: str, message: str | None = None, fields: Any = None) -> dict[str, Any]:
    data: dict[str, Any] = {"code": code}
    if message:
        data["message"] = message
    if fields is not None:
        data["fields"] = fields
    return data


def error_response(code: str, status_code: int, message: str | None = None) -> JsonResponse:
    """Para lo que responde fuera de DRF (middlewares): mismos bytes que el manejador."""
    return JsonResponse(body(code, message), status=status_code, json_dumps_params=_JSON)


class ApiError(exceptions.APIException):
    """Error de dominio con código estable (p. ej., `INVALID_CREDENTIALS`, `LAST_OWNER`)."""

    def __init__(self, code: str, status_code: int, message: str | None = None) -> None:
        super().__init__(detail=message or code, code=code)
        self.status_code = status_code
        self.code, self.message = code, message


def code_for(status_code: int) -> str:
    """El código de un estado HTTP sin excepción propia, igual en DRF y en el middleware."""
    return BY_STATUS.get(status_code) or (INTERNAL_ERROR if status_code >= 500 else "ERROR")


def _errors(detail: Any) -> Any:
    """`detail` de DRF con la forma del contrato: cada hoja es `[{code, message}]`, los errores
    generales van en `_`, un serializador anidado es un objeto y una lista, un objeto por índice."""
    if isinstance(detail, dict):
        out: dict[str, Any] = {}
        for key, value in detail.items():
            name = "_" if key == "non_field_errors" else str(key)
            item = _errors(value)
            both_lists = isinstance(out.get(name), list) and isinstance(item, list)
            out[name] = out[name] + item if both_lists else item
        return out
    if isinstance(detail, list | tuple):
        if all(isinstance(item, exceptions.ErrorDetail) for item in detail):
            return [{"code": item.code or "invalid", "message": str(item)} for item in detail]
        return {str(index): _errors(item) for index, item in enumerate(detail) if item}
    return [{"code": getattr(detail, "code", None) or "invalid", "message": str(detail)}]


def _fields(detail: Any) -> dict[str, Any]:
    out = _errors(detail)
    return out if isinstance(out, dict) else {"_": out}


def exception_handler(exc: Exception, context: dict[str, Any]) -> Response | None:
    """`EXCEPTION_HANDLER` de DRF. Lo que no conoce devuelve None: Django responde 500 y el
    middleware de la API lo convierte en `INTERNAL_ERROR`, sin detalle. En una ruta de tenant,
    quien deshace la transacción ante un error es `TenantResolutionMiddleware`. En una ruta de
    tenant, una fila que no existe (`DoesNotExist` de un comando: inexistente, ajena o fuera del
    alcance) es el mismo 404; fuera de ellas sigue siendo un fallo (500)."""
    if isinstance(exc, Http404) or (
        isinstance(exc, ObjectDoesNotExist) and current_tenant() is not None
    ):
        exc = exceptions.NotFound()
    elif isinstance(exc, DjangoPermissionDenied):
        exc = exceptions.PermissionDenied()
    if not isinstance(exc, exceptions.APIException):
        return None
    headers: dict[str, str] = {}
    if isinstance(exc, exceptions.NotAuthenticated | exceptions.AuthenticationFailed):
        # Sin sesión es siempre 401 (ADR-014 §3). DRF lo baja a 403 si la vista no tiene una
        # clase de autenticación con `authenticate_header`.
        exc.status_code, exc.auth_header = 401, AUTH_SCHEME
    if getattr(exc, "auth_header", None) or exc.status_code == 401:  # todo 401 lleva el esquema
        headers["WWW-Authenticate"] = getattr(exc, "auth_header", None) or AUTH_SCHEME
    if getattr(exc, "wait", None):
        headers["Retry-After"] = f"{int(exc.wait)}"
    if isinstance(exc, ApiError):
        data = body(exc.code, exc.message)
    elif isinstance(exc, exceptions.ValidationError):
        data = body("VALIDATION_ERROR", fields=_fields(exc.detail))
    else:
        code = next((c for kind, c in _BY_EXCEPTION if isinstance(exc, kind)), None)
        data = body(code or code_for(exc.status_code))
    return Response(data, status=exc.status_code, headers=headers)
