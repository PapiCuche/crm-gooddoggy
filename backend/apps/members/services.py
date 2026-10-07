"""Administración de miembros (F2-19, E01-07, ADR-017): suspender y reactivar una membresía,
asignarle una sucursal (F2-69), invitar a alguien a la organización (F2-80, ADR-020) y
revocar una invitación (F2-82).

Orquesta `access` (las reglas de RBAC), `organizations` (la membresía) y `accounts` (las
sesiones), que no se importan entre sí. Primero las reglas, que toman el bloqueo de RBAC de la
organización; después la escritura, bajo ese bloqueo; y al suspender, la revocación de las
sesiones del usuario (ADR-003 §2). Todo en un savepoint del `tenant_scope` de quien llama.
"""

from collections.abc import Collection
from uuid import UUID

from django.core.exceptions import ValidationError
from django.db import transaction

from apps.access.services import (
    ensure_can_invite,
    ensure_can_manage_member,
    ensure_can_revoke_invitation,
)
from apps.accounts.emails import canonical_email
from apps.accounts.services import revoke_sessions
from apps.organizations import services as organizations
from apps.organizations.services import (
    BranchRef,
    InvitationRef,
    create_invitation,
    invitation_role_ids,
    set_membership_branch,
    set_membership_status,
)
from core import mail
from core.tenancy.context import TenantContext
from core.tenancy.scope import require_scope

ACTIVE, SUSPENDED = "ACTIVE", "SUSPENDED"


class InvalidEmail(ValueError):
    """No es una dirección a la que se pueda invitar."""


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


def invite(ctx: TenantContext, *, email: str, role_ids: Collection[UUID]) -> InvitationRef:
    """Deja creada la invitación de `email` con esos roles (ADR-020). No envía nada.

    El correo se guarda en la forma canónica de las cuentas (D-F2-3) y solo si `core.mail`
    podría entregarlo: `InvalidEmail`. Las reglas son las de asignar esos roles, y además el
    permiso `users.invite`. Lanza `AccessDenied`, `UnknownRole` (un rol no es de esta
    organización) y lo que lanza `create_invitation`. Si lanza, no escribe.
    """
    try:
        address = canonical_email(email)
    except ValidationError:
        address = ""
    if not address or not mail.deliverable(address):
        raise InvalidEmail
    alias = require_scope(ctx)
    with transaction.atomic(using=alias):
        ensure_can_invite(ctx, role_ids=role_ids)
        return create_invitation(ctx, email=address, role_ids=role_ids)


def revoke_invitation(ctx: TenantContext, *, invitation_id: UUID) -> InvitationRef:
    """Revoca una invitación pendiente (ADR-020). Devuelve la invitación como queda.

    Las reglas son las de invitar con los roles que la invitación daría. Primero los permisos
    y el bloqueo de RBAC, después se lee la invitación, ya sin nadie que la cambie, y entonces
    se comprueba que el actor cubre sus roles. Lanza `AccessDenied`, `DoesNotExist` (no es de
    esta organización) o `InvalidTransition` (aceptada, o anotada como caducada). Si lanza, no
    escribe.
    """
    alias = require_scope(ctx)
    with transaction.atomic(using=alias):
        ensure_can_revoke_invitation(ctx, role_ids=())
        roles = invitation_role_ids(ctx, invitation_id=invitation_id)
        ensure_can_revoke_invitation(ctx, role_ids=roles)
        return organizations.revoke_invitation(ctx, invitation_id=invitation_id)
