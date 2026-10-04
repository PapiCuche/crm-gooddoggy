"""Motor de autorización (ADR-003 §5, frontera B2): de la membresía a la decisión.

La decisión sale solo de permisos y alcances: unión de las concesiones de todos los roles de
la membresía. Nada lee aquí el código o el nombre de un rol, su marca de Owner, ni si el
usuario es staff de plataforma. Todo se evalúa dentro del `tenant_scope` del propio contexto;
RLS sigue siendo la segunda barrera. Ante cualquier duda se deniega o se falla.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from functools import reduce
from operator import or_
from types import MappingProxyType
from typing import Any
from uuid import UUID

from django.apps import apps
from django.core.exceptions import PermissionDenied
from django.db.models import F, Model, QuerySet

from apps.access.catalog import BY_CODE, PermissionDef, Scope
from apps.access.models import Role, RolePermission
from apps.access.scopes import policy_for
from core.db.models import TenantModel
from core.tenancy.context import ActorType, TenantContext, TenantContextError
from core.tenancy.scope import require_scope, scope_token

ACTIVE = "ACTIVE"  # OrganizationMembership.Status.ACTIVE (access no importa organizations)


class Denied(StrEnum):
    MEMBERSHIP = "membership"
    PERMISSION = "permission"
    SCOPE = "scope"
    ESCALATION = "escalation"  # concede lo que no tiene, o con más alcance (F2-05C)
    SENSITIVE = "sensitive"  # permiso sensible: solo lo delega un Owner
    SELF = "self"  # nadie modifica sus propios roles
    LAST_OWNER = "last_owner"  # siempre queda un Owner activo


class AccessDenied(PermissionDenied):
    def __init__(self, reason: Denied) -> None:
        super().__init__(reason)
        self.reason = reason


class UnknownPermission(LookupError):
    """Código fuera del catálogo: un error de programación, nunca una concesión."""


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    """Contexto de tenant + membresía + permisos efectivos (foto de la petición actual)."""

    tenant: TenantContext
    scope: object  # transacción en la que se leyó: la foto no sirve en un scope posterior
    membership_id: UUID
    permissions: Mapping[str, frozenset[Scope | None]]  # {None} si el permiso no admite alcance
    team_ids: frozenset[UUID] = frozenset()  # E01-09
    branch_ids: frozenset[UUID] = frozenset()  # E01-09


def definition(code: str) -> PermissionDef:
    try:
        return BY_CODE[code]
    except KeyError:
        raise UnknownPermission(code) from None


def execution_context(ctx: TenantContext) -> ExecutionContext:
    """Dos consultas, sea cual sea el número de roles. Sin membresía activa: `AccessDenied`."""
    alias = require_scope(ctx)
    if ctx.actor_type != ActorType.USER or ctx.user_id is None:
        raise AccessDenied(Denied.MEMBERSHIP)
    memberships = apps.get_model("organizations", "OrganizationMembership")._default_manager
    row = (
        memberships.using(alias)
        .filter(user_id=ctx.user_id)
        .values_list("pk", "status", "user__is_active")
        .first()
    )
    if row is None or row[1] != ACTIVE or not row[2]:
        raise AccessDenied(Denied.MEMBERSHIP)
    grants = RolePermission.objects.using(alias).filter(role__assignments__membership_id=row[0])
    effective: dict[str, set[Scope | None]] = {}
    for code, scope in grants.values_list("permission_id", "scope"):
        known = BY_CODE.get(code)  # concesión de un código retirado del catálogo: se ignora
        if known is not None and (scope is not None) == known.supports_scope:
            effective.setdefault(code, set()).add(Scope(scope) if scope else None)
    frozen = {code: frozenset(scopes) for code, scopes in effective.items()}
    return ExecutionContext(ctx, scope_token(ctx), row[0], MappingProxyType(frozen))


def _bound(ectx: ExecutionContext) -> None:
    """La foto solo vale dentro del mismo `tenant_scope` (misma transacción) en que se leyó."""
    if scope_token(ectx.tenant) is not ectx.scope:
        raise TenantContextError("ExecutionContext de otro tenant_scope: hay que recalcularlo")


def has_permission(ectx: ExecutionContext, code: str) -> bool:
    """Nivel 1: ¿tiene el permiso, con cualquier alcance?"""
    _bound(ectx)
    definition(code)
    return bool(ectx.permissions.get(code))


def can(ectx: ExecutionContext, code: str, obj: Model | None = None) -> bool:
    """Niveles 1 y 2: permiso y, si lo admite, alcance sobre `obj`."""
    _bound(ectx)
    permission = definition(code)
    scopes = ectx.permissions.get(code)
    if not scopes:
        return False
    if obj is not None and getattr(obj, "organization_id", None) != ectx.tenant.organization_id:
        return False  # objeto de otra organización, o sin organización
    if not permission.supports_scope:
        return True
    if obj is None:
        raise TypeError("permiso con alcance: pasa el objeto, o usa has_permission() o scoped()")
    policy = policy_for(type(obj))  # antes del atajo: sin política falla también ORGANIZATION
    if Scope.ORGANIZATION in scopes:
        return True
    return any(policy.allows(scope, ectx, obj) for scope in scopes if scope is not None)


def require(ectx: ExecutionContext, code: str, obj: Model | None = None) -> None:
    if not has_permission(ectx, code):
        raise AccessDenied(Denied.PERMISSION)
    if (obj is not None or definition(code).supports_scope) and not can(ectx, code, obj):
        raise AccessDenied(Denied.SCOPE)


def scoped[M: Model](ectx: ExecutionContext, code: str, queryset: QuerySet[M]) -> QuerySet[M]:
    """Filtro de listado: las mismas filas para las que `can(ectx, code, obj)` es verdadero."""
    _bound(ectx)
    permission = definition(code)
    if not issubclass(queryset.model, TenantModel):
        raise TypeError("scoped() solo filtra modelos tenant-owned")
    scopes = ectx.permissions.get(code)
    if not scopes:
        return queryset.none()
    queryset = queryset.filter(organization_id=ectx.tenant.organization_id)
    if not permission.supports_scope:
        return queryset
    policy = policy_for(queryset.model)
    if Scope.ORGANIZATION in scopes:
        return queryset
    return queryset.filter(reduce(or_, (policy.q(s, ectx) for s in scopes if s is not None)))


def role_names(ectx: ExecutionContext) -> list[dict[str, str]]:
    """Roles de la membresía, para mostrar. Nunca para decidir: eso son los permisos."""
    _bound(ectx)
    roles = Role.objects.using(require_scope(ectx.tenant))
    held = roles.filter(assignments__membership_id=ectx.membership_id)
    return list(held.order_by("name", "code").values("code", "name"))


def organization_of(ectx: ExecutionContext) -> dict[str, object]:
    """Identidad de la organización del contexto (tabla platform-owned, sin datos de negocio)."""
    _bound(ectx)
    organizations = apps.get_model("organizations", "Organization")._default_manager
    found: dict[str, object] = organizations.values("id", "slug", "name").get(
        pk=ectx.tenant.organization_id
    )
    return found


def memberships(ectx: ExecutionContext) -> QuerySet[Any]:
    """Las membresías de la organización, con su usuario. Las filtra RLS; quién puede verlas lo
    decide el permiso de la vista (`ScopeFilter`)."""
    _bound(ectx)
    rows = apps.get_model("organizations", "OrganizationMembership")._default_manager
    found: QuerySet[Any] = rows.using(require_scope(ectx.tenant)).select_related("user")
    return found


def roles_by_membership(ectx: ExecutionContext, members: Iterable[UUID]) -> dict[UUID, list[Any]]:
    """Roles de varias membresías en una consulta, para mostrar. Nunca para decidir."""
    _bound(ectx)
    roles = Role.objects.using(require_scope(ectx.tenant))
    held = roles.filter(assignments__membership_id__in=members).order_by("name", "code")
    found: dict[UUID, list[Any]] = {}
    for row in held.values("code", "name", member=F("assignments__membership_id")):
        found.setdefault(row.pop("member"), []).append(row)
    return found
