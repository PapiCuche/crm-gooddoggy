"""Alcances por tipo de recurso (ADR-003 §5).

Cada modelo tenant-owned declara una sola vez qué columnas lo atan a una persona, un equipo o
una sucursal. De esa única declaración salen el filtro de listado y la verificación por objeto,
así que no pueden divergir. `ORGANIZATION` lo resuelve el motor (`selectors`), no la política.
"""

from dataclasses import dataclass
from functools import reduce
from operator import or_
from typing import TYPE_CHECKING, Protocol
from uuid import UUID

from django.core.exceptions import ImproperlyConfigured
from django.db.models import Model, Q

from apps.access.catalog import Scope
from core.db.models import TenantModel

if TYPE_CHECKING:
    from apps.access.selectors import ExecutionContext


class ScopePolicyMissing(LookupError):
    """El tipo de recurso no declaró su política: se falla cerrado, también para ORGANIZATION."""


class ScopePolicy(Protocol):
    def q(self, scope: Scope, ectx: ExecutionContext) -> Q: ...
    def allows(self, scope: Scope, ectx: ExecutionContext, obj: Model) -> bool: ...


@dataclass(frozen=True, slots=True)
class FieldScopes:
    """Política por columnas: `own` (ids de usuario), y opcionalmente equipo y sucursal."""

    own: tuple[str, ...]
    team: str | None = None
    branch: str | None = None

    def _pairs(self, scope: Scope, ectx: ExecutionContext) -> list[tuple[str, frozenset[UUID]]]:
        user = ectx.tenant.user_id
        me = frozenset([user]) if user else frozenset[UUID]()
        pairs = [(column, me) for column in self.own]  # TEAM y BRANCH incluyen OWN (ADR-003 §5)
        if scope == Scope.TEAM and self.team:
            pairs.append((self.team, ectx.team_ids))
        if scope == Scope.BRANCH and self.branch:
            pairs.append((self.branch, ectx.branch_ids))
        return pairs

    def q(self, scope: Scope, ectx: ExecutionContext) -> Q:
        return reduce(or_, (Q(**{f"{c}__in": ids}) for c, ids in self._pairs(scope, ectx)))

    def allows(self, scope: Scope, ectx: ExecutionContext, obj: Model) -> bool:
        return any(  # NULL nunca casa, igual que `IN` en SQL
            (value := getattr(obj, column)) is not None and value in ids
            for column, ids in self._pairs(scope, ectx)
        )


@dataclass(frozen=True, slots=True)
class Everyone:
    """Configuración de la organización (las etiquetas, los pipelines): con el permiso de lectura,
    cualquier alcance la ve toda. El alcance decide qué registros ajenos se tocan, no ésta."""

    def q(self, scope: Scope, ectx: ExecutionContext) -> Q:
        return Q()

    def allows(self, scope: Scope, ectx: ExecutionContext, obj: Model) -> bool:
        return True


_REGISTRY: dict[type[Model], ScopePolicy] = {}


def register(model: type[Model], policy: ScopePolicy) -> None:
    """Declara la política de un modelo (desde el `ready()` de su app). Valida al registrar."""
    if not issubclass(model, TenantModel) or model._meta.abstract:
        raise ImproperlyConfigured(f"{model.__name__} no es un modelo tenant-owned concreto")
    if isinstance(policy, FieldScopes):
        if not policy.own:  # sin columnas propias el OR quedaría vacío y casaría con todo
            raise ImproperlyConfigured(f"{model.__name__}: la política necesita columnas `own`")
        columns = {field.attname for field in model._meta.concrete_fields}
        for column in (*policy.own, policy.team, policy.branch):
            if column is not None and column not in columns:  # ni relaciones ni nombres de FK
                raise ImproperlyConfigured(f"{model.__name__}: `{column}` no es una columna de id")
    if _REGISTRY.setdefault(model, policy) != policy:
        raise ImproperlyConfigured(f"{model.__name__} ya tiene otra política de alcance")


def policy_for(model: type[Model]) -> ScopePolicy:
    try:
        return _REGISTRY[model]
    except KeyError:
        raise ScopePolicyMissing(model.__name__) from None
