"""Administración de miembros (F2-19, E01-07, ADR-017): suspender y reactivar una membresía.

Orquesta `access` (las reglas de RBAC) y `organizations` (la membresía), que no se importan
entre sí. Primero las reglas, que toman el bloqueo de RBAC de la organización; después la
escritura, bajo ese bloqueo. Todo en un savepoint del `tenant_scope` de quien llama.
"""

from uuid import UUID

from django.db import transaction

from apps.access.services import ensure_can_manage_member
from apps.organizations.services import set_membership_status
from core.tenancy.context import TenantContext
from core.tenancy.scope import require_scope

ACTIVE, SUSPENDED = "ACTIVE", "SUSPENDED"


def set_member_status(ctx: TenantContext, *, membership_id: UUID, status: str) -> bool:
    """Deja la membresía en `ACTIVE` o `SUSPENDED`. Devuelve si cambió.

    Lanza `AccessDenied` (permiso, uno mismo, escalada, último Owner), `DoesNotExist` (no es de
    esta organización) o `InvalidTransition` (invitada o dada de baja). Si lanza, no escribe.
    """
    if status not in (ACTIVE, SUSPENDED):
        raise ValueError(f"estado no admitido: {status!r}")
    with transaction.atomic(using=require_scope(ctx)):
        ensure_can_manage_member(ctx, membership_id=membership_id, leaving=status != ACTIVE)
        return set_membership_status(ctx, membership_id=membership_id, status=status)
