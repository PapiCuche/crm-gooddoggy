"""El motor de autorización en DRF (ADR-003 §5, frontera B2). Denegación por defecto.

`HasPermission` y `ScopeFilter` son los valores por defecto de DRF (`config.settings.base`).
Una vista de tenant declara el permiso de cada método en `required_permissions` (un código, o
una tupla de códigos si los exige todos); vista sin declaración, método sin declarar, declaración
que no se entiende o petición sin contexto de tenant: denegado.

El contexto es el que resolvió el middleware (`core.tenancy.context`), nunca `request.user`
ni datos del cliente. Las vistas de plataforma son las rutas fuera de `/api/v1/o/<slug>/`:
otra frontera, con sus propias `permission_classes` y `filter_backends`. Se excluyen por la
ruta, no por el actor: ser staff de plataforma no abre ninguna ruta de tenant.
"""

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

from django.core.exceptions import ObjectDoesNotExist
from django.db.models import Model, QuerySet
from rest_framework.exceptions import NotFound
from rest_framework.filters import BaseFilterBackend
from rest_framework.permissions import BasePermission

from apps.access.selectors import (
    AccessDenied,
    Denied,
    ExecutionContext,
    can,
    execution_context,
    has_permission,
    scoped,
)
from core.api.errors import ApiError
from core.tenancy import context


def required_codes(request: Any, view: Any) -> tuple[str, ...]:
    """Permisos que la vista declara para este método (HEAD usa los de GET): un código, o una
    tupla si exige varios a la vez (F2-56). Vacío: denegar. Una lista, una tupla vacía o un
    valor que no es texto no declaran nada."""
    declared = getattr(view, "required_permissions", None)
    method = "GET" if request.method == "HEAD" else request.method
    wanted = declared.get(method) if isinstance(declared, Mapping) else None
    codes = (wanted,) if isinstance(wanted, str) else wanted
    if isinstance(codes, tuple) and all(isinstance(code, str) for code in codes):
        return codes
    return ()


def request_context(request: Any) -> ExecutionContext | None:
    """Permisos efectivos de la petición: se leen una vez, dentro de su `tenant_scope`.

    `None` fuera de una ruta de tenant. Sin membresía activa responde 404, el mismo estado
    que da el middleware ante una organización inexistente.
    """
    tenant = context.current()
    if tenant is None:
        return None
    holder = getattr(request, "_request", request)  # DRF clona su Request (OPTIONS): una foto
    cached: ExecutionContext | None = getattr(holder, "_access_context", None)
    if cached is None:
        try:
            cached = execution_context(tenant)
        except AccessDenied:
            raise NotFound from None
        holder._access_context = cached  # vive lo que la petición; el motor lo liga al scope
    return cached


class HasPermission(BasePermission):
    def has_permission(self, request: Any, view: Any) -> bool:
        ectx, codes = request_context(request), required_codes(request, view)
        if ectx is None or not codes:
            return False
        held = [has_permission(ectx, code) for code in codes]  # un código desconocido falla
        return all(held)  # todos, no alguno

    def has_object_permission(self, request: Any, view: Any, obj: Model) -> bool:
        ectx, codes = request_context(request), required_codes(request, view)
        if ectx is None or not codes:
            return False
        if not all(can(ectx, code, obj) for code in codes):
            raise NotFound  # nunca 403; red de seguridad: la vista debe consultar con scoped()
        return True


class IsMember(BasePermission):
    """Cualquier membresía activa, sin permiso del catálogo. Solo para leer el contexto propio
    (`GET …/me/`): la vista que la use no puede servir datos del tenant más allá de la propia
    membresía ni aceptar escrituras, y su ruta figura en `MEMBER` de la auditoría del URLconf.

    La membresía se mira primero: sin ella la respuesta es 404 con cualquier método, como en
    el resto de las rutas de tenant. Con ella solo pasan `GET` y su `HEAD`."""

    def has_permission(self, request: Any, view: Any) -> bool:
        return request_context(request) is not None and request.method in ("GET", "HEAD")


class ScopeFilter(BaseFilterBackend):
    """Listados y `get_object()`: solo las filas dentro del alcance, filtradas en SQL."""

    def filter_queryset(self, request: Any, queryset: QuerySet[Any], view: Any) -> QuerySet[Any]:
        ectx, codes = request_context(request), required_codes(request, view)
        if ectx is None or not codes:
            return queryset.none()
        for code in codes:  # las filas que todos los permisos dejan ver
            queryset = scoped(ectx, code, queryset)
        return queryset


@contextmanager
def rbac_errors() -> Iterator[None]:
    """Lo que lanza un cambio de RBAC o de membresía, con la forma del contrato (ADR-014 §1,
    ADR-017 §4). Lo que no es de esta organización no existe: 404. Un actor que dejó de ser
    miembro mientras esperaba el bloqueo, también. `LAST_OWNER` es un 409 con su código. El
    resto de negativas (permiso, uno mismo, escalada, sensible) sigue siendo un 403 sin motivo.
    """
    try:
        yield
    except ObjectDoesNotExist:
        raise NotFound from None
    except AccessDenied as denied:
        if denied.reason is Denied.MEMBERSHIP:
            raise NotFound from None
        if denied.reason is not Denied.LAST_OWNER:
            raise
        message = "Debe quedar al menos un Owner activo en la organización."
        raise ApiError("LAST_OWNER", 409, message) from None
