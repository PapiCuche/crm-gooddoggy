"""Selectores públicos (docs/architecture/module-dependencies.md §3).

Los de plataforma se ejecutan sin tenant activo. Las membresías se leen dentro de
`user_scope`: la política SELECT de `organization_memberships` solo deja ver las del propio
usuario (ADR-002 §3.2).
`branches`, `teams` y `team_members` son de tenant: se leen dentro de un `tenant_scope`.
"""

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from django.db.models import QuerySet

from apps.organizations.models import (
    Branch,
    Organization,
    OrganizationMembership,
    Team,
    TeamMember,
    UserInvitation,
)
from core.tenancy.resolution import ACCESSIBLE_STATUSES, OrganizationRef
from core.tenancy.scope import user_scope

ACTIVE = OrganizationMembership.Status.ACTIVE


@dataclass(frozen=True, slots=True)
class OrganizationSummary:
    id: UUID
    slug: str
    name: str


def organization_by_slug(slug: str) -> OrganizationRef | None:
    row = Organization.objects.filter(slug=slug).values_list("id", "status").first()
    return OrganizationRef(*row) if row else None


def _active_user_id(user: Any) -> UUID | None:
    """Identidad no es autorización: un usuario anónimo o inactivo no tiene membresías."""
    user_id = getattr(user, "pk", None)
    return user_id if isinstance(user_id, UUID) and getattr(user, "is_active", False) else None


def active_membership(user: Any, organization_id: UUID) -> UUID | None:
    """Resolvedor de tenancy (`TENANCY_MEMBERSHIP_RESOLVER`): fail-closed.

    Devuelve el id del usuario solo si está activo y tiene una membresía `ACTIVE` en esa
    organización. Ser staff de plataforma no cuenta: no hay membresías implícitas.
    """
    user_id = _active_user_id(user)
    if user_id is None:
        return None
    with user_scope(user_id):
        found = OrganizationMembership.for_user.filter(
            organization_id=organization_id, user_id=user_id, status=ACTIVE
        ).exists()
    return user_id if found else None


def organizations_for_user(user: Any) -> list[OrganizationSummary]:
    """Organizaciones accesibles del usuario (membresía `ACTIVE` y organización no suspendida)."""
    user_id = _active_user_id(user)
    if user_id is None:
        return []
    with user_scope(user_id):
        ids = list(
            OrganizationMembership.for_user.filter(user_id=user_id, status=ACTIVE).values_list(
                "organization_id", flat=True
            )
        )
    rows = Organization.objects.filter(id__in=ids, status__in=ACCESSIBLE_STATUSES)
    return [
        OrganizationSummary(*row)
        for row in rows.order_by("name", "slug").values_list("id", "slug", "name")
    ]


def branches() -> QuerySet[Branch]:
    """Sucursales de la organización del `tenant_scope` activo, activas e inactivas. Filtra por
    organización, no por permiso: el permiso lo exige quien las sirve (`HasPermission`)."""
    return Branch.objects.all()


def teams() -> QuerySet[Team]:
    """Equipos de la organización del `tenant_scope` activo, activos e inactivos. Filtra por
    organización, no por permiso: el permiso lo exige quien los sirve (`HasPermission`)."""
    return Team.objects.all()


def invitations() -> QuerySet[UserInvitation]:
    """Invitaciones de la organización del `tenant_scope` activo, en cualquier estado
    (ADR-020). Filtra por organización, no por permiso: el permiso lo exige quien las sirve."""
    return UserInvitation.objects.all()


def team_members(team_id: UUID) -> QuerySet[TeamMember]:
    """Integrantes de un equipo de la organización del `tenant_scope` activo, cada uno con su
    membresía y su usuario ya cargados (una consulta). Filtra por organización, no por permiso;
    un equipo de otra organización no tiene filas aquí."""
    rows = TeamMember.objects.filter(team_id=team_id)
    return rows.select_related("membership", "membership__user")
