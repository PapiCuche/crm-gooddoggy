"""Lectores de presentación de `access`: lo que una pantalla de administración enseña de los
roles (F2-22). Nunca deciden: la autorización sale de `selectors`, que no decide con nada de esto.

Como los selectores, solo valen dentro del `tenant_scope` del contexto; RLS filtra las filas.
Quién puede verlas lo decide el permiso de la vista (`HasPermission` y `ScopeFilter`).
"""

from collections.abc import Iterable
from typing import Any
from uuid import UUID

from django.db.models import Count, QuerySet

from apps.access.models import MembershipRole, Role, RolePermission
from apps.access.selectors import ExecutionContext, bound
from core.tenancy.scope import require_scope


def roles(ectx: ExecutionContext) -> QuerySet[Role]:
    """Los roles de la organización."""
    bound(ectx)
    return Role.objects.using(require_scope(ectx.tenant))


def grants_by_role(ectx: ExecutionContext, role_ids: Iterable[UUID]) -> dict[UUID, list[Any]]:
    """Concesiones de varios roles en una consulta, tal como están guardadas: también las de un
    código que el catálogo ya no tiene (el motor las ignora; aquí se ven). Por código, en el
    orden de Python y no en el de la intercalación de la base, como `…/me/`."""
    bound(ectx)
    rows = RolePermission.objects.using(require_scope(ectx.tenant)).filter(role_id__in=role_ids)
    found: dict[UUID, list[Any]] = {}
    for role_id, code, scope in sorted(rows.values_list("role_id", "permission_id", "scope")):
        found.setdefault(role_id, []).append({"code": code, "scope": scope})
    return found


def members_by_role(ectx: ExecutionContext, role_ids: Iterable[UUID]) -> dict[UUID, int]:
    """Cuántas membresías tienen asignado cada rol, en cualquier estado. Una consulta."""
    bound(ectx)
    held = MembershipRole.objects.using(require_scope(ectx.tenant)).filter(role_id__in=role_ids)
    return dict(held.values_list("role_id").annotate(Count("id")))
