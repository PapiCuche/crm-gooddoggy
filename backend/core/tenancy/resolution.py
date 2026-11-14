"""Resolución slug → TenantContext compartida por HTTP, WebSockets y comandos (§2, §4, §7).

La URL solo **selecciona**: la autorización sale de la membresía. El selector de
organizaciones y el resolvedor de membresías se inyectan por settings (el kernel no importa
módulos superiores). El resolvedor real vive en `apps.organizations.selectors` (F2-02). Al
final, la política de segundo factor (`second_factor.admit`, ADR-038): HTTP y WebSocket la pasan
por aquí, así que ninguna entrada se la salta.
"""

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from django.conf import settings
from django.utils.module_loading import import_string

from core.tenancy.context import ActorType, TenantContext
from core.tenancy.second_factor import admit

ACCESSIBLE_STATUSES = frozenset({"ACTIVE", "TRIAL"})
NO_ORGANIZATION = UUID(int=0)  # ninguna organización tiene este id (los reales son UUIDv7)


@dataclass(frozen=True, slots=True)
class OrganizationRef:
    id: UUID
    status: str


class TenantNotFound(Exception):
    """Organización inexistente o sin membresía activa: indistinguibles para el cliente."""


class OrganizationSuspended(Exception):
    """Miembro de una organización cuyo estado no permite el acceso (403 ORG_SUSPENDED)."""


def no_memberships(user: Any, organization_id: UUID) -> UUID | None:
    """Resolvedor que niega todo (fail-closed), para contextos sin membresías."""
    return None


def organization_by_slug(slug: str) -> OrganizationRef | None:
    selector = import_string(settings.TENANCY_ORGANIZATION_SELECTOR)
    return selector(slug)  # type: ignore[no-any-return]


def resolve_tenant(
    slug: str, user: Any, source: str, correlation_id: str | None = None
) -> TenantContext:
    org = organization_by_slug(slug)
    membership = import_string(settings.TENANCY_MEMBERSHIP_RESOLVER)
    # Se consulta siempre, exista o no el slug: el tiempo de respuesta no delata qué
    # organizaciones existen (OBS-F2-09-1).
    organization_id = org.id if org is not None else NO_ORGANIZATION
    user_id = membership(user, organization_id) if user is not None else None
    if org is None or user_id is None:
        raise TenantNotFound  # antes que el estado: un no miembro no aprende nada
    if org.status not in ACCESSIBLE_STATUSES:
        raise OrganizationSuspended
    ctx = TenantContext(org.id, source, user_id, ActorType.USER, user_id, correlation_id)
    admit(ctx)  # después del 404 y del 403: solo un miembro sabe que le falta (ADR-038)
    return ctx
