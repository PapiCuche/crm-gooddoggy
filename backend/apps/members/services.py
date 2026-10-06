"""Administración de miembros (F2-19, E01-07, ADR-017): suspender y reactivar una membresía,
y asignarle una sucursal (F2-69).

Orquesta `access` (las reglas de RBAC), `organizations` (la membresía) y `accounts` (las
sesiones), que no se importan entre sí. Primero las reglas, que toman el bloqueo de RBAC de la
organización; después la escritura, bajo ese bloqueo; y al suspender, la revocación de las
sesiones del usuario (ADR-003 §2). Todo en un savepoint del `tenant_scope` de quien llama.
"""

from uuid import UUID

from django.db import transaction

from apps.access.services import ensure_can_manage_member
from apps.accounts.services import revoke_sessions
from apps.organizations.services import BranchRef, set_membership_branch, set_membership_status
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
    alias = require_scope(ctx)
    with transaction.atomic(using=alias):
        ensure_can_manage_member(ctx, membership_id=membership_id, leaving=status != ACTIVE)
        user_id = set_membership_status(ctx, membership_id=membership_id, status=status)
        if user_id is not None and status == SUSPENDED:
            revoke_sessions(user_id, using=alias)  # ADR-003 §2: en la misma transacción
        return user_id is not None


def set_member_branch(
    ctx: TenantContext, *, membership_id: UUID, branch_id: UUID | None
) -> BranchRef | None:
    """Deja a otra membresía con esa sucursal, o sin ninguna. Devuelve la que queda.

    Las reglas son las de suspender: `users.manage`, nadie cambia la suya y el actor cubre los
    roles del miembro, porque su sucursal es lo que alcanza una concesión con alcance `BRANCH`.
    Lanza `AccessDenied`, `DoesNotExist` (la membresía no es de esta organización) o
    `UnknownBranch` (la sucursal no lo es). Si lanza, no escribe.
    """
    alias = require_scope(ctx)
    with transaction.atomic(using=alias):
        ensure_can_manage_member(ctx, membership_id=membership_id, leaving=False)
        return set_membership_branch(ctx, membership_id=membership_id, branch_id=branch_id)
