"""Administración de miembros (F2-19, E01-07, ADR-017): suspender y reactivar una membresía,
asignarle una sucursal (F2-69), ponerla en un equipo o quitarla (F2-58, F2-59, D-F2-13),
invitar a alguien a la organización (F2-80, ADR-020) o renovar y reenviar su invitación (F2-130e),
revocarla (F2-82) y aceptarla (F2-130b).

Orquesta `access` (las reglas de RBAC), `organizations` (la membresía) y `accounts` (las
sesiones), que no se importan entre sí. Primero las reglas, que toman el bloqueo de RBAC de la
organización; después la escritura, bajo ese bloqueo; y al suspender, la revocación de las
sesiones del usuario (ADR-003 §2). Todo en un savepoint del `tenant_scope` de quien llama.
"""

from collections.abc import Collection
from typing import Any
from uuid import UUID

from django.core.exceptions import ValidationError
from django.db import transaction

from apps.access.selectors import AccessDenied
from apps.access.services import (
    UnknownRole,
    ensure_can_invite,
    ensure_can_manage_member,
    ensure_can_move_member,
    ensure_can_place_in_team,
    ensure_can_revoke_invitation,
    ensure_inviter_can_grant,
    grant_invited_roles,
    lock_rbac,
)
from apps.accounts.emails import canonical_email
from apps.accounts.services import StaffAccount, invited_account, revoke_sessions
from apps.organizations import services as organizations
from apps.organizations import teams
from apps.organizations.services import (
    AlreadyMember,
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
    roles del miembro, porque su sucursal es lo que alcanza una concesión con alcance `BRANCH`;
    cada una de esas, en la sucursal de origen y en la de destino (D-F2-13).
    Lanza `AccessDenied`, `DoesNotExist` (la membresía no es de esta organización) o
    `UnknownBranch` (la sucursal no lo es). Si lanza, no escribe.
    """
    alias = require_scope(ctx)
    with transaction.atomic(using=alias):
        ensure_can_move_member(ctx, membership_id=membership_id, branch_id=branch_id)
        return set_membership_branch(ctx, membership_id=membership_id, branch_id=branch_id)


def put_team_member(
    ctx: TenantContext, *, team_id: UUID, membership_id: UUID, **fields: Any
) -> tuple[Any, bool]:
    """Pone a otra membresía en un equipo, o cambia su papel en él (F2-58). Devuelve el
    integrante y si es nuevo, como `organizations.teams.put_team_member`.

    Primero las reglas (D-F2-13): `teams.manage` y `users.view` releídos bajo el bloqueo de RBAC,
    y el actor cubre sobre ese equipo cada concesión con alcance `TEAM` del miembro. Lanza
    `AccessDenied` y lo que lanza el comando (`OwnTeamMembership`, `DoesNotExist`, `ValueError`).
    Si lanza, no escribe.
    """
    alias = require_scope(ctx)
    with transaction.atomic(using=alias):
        ensure_can_place_in_team(ctx, team_id=team_id, membership_id=membership_id)
        return teams.put_team_member(ctx, team_id=team_id, membership_id=membership_id, **fields)


def remove_team_member(ctx: TenantContext, *, team_id: UUID, membership_id: UUID) -> None:
    """Quita a otra membresía de un equipo (F2-59). Las reglas son las de ponerla: salir del
    equipo reduce lo que alcanza, y quitar exige lo mismo que conceder (PO-1)."""
    alias = require_scope(ctx)
    with transaction.atomic(using=alias):
        ensure_can_place_in_team(ctx, team_id=team_id, membership_id=membership_id)
        teams.remove_team_member(ctx, team_id=team_id, membership_id=membership_id)


class RoleGone(Exception):
    """Un rol de la invitación que se reenvía ya no existe: se invita otra vez con otros roles."""


def invite(
    ctx: TenantContext, *, email: str, role_ids: Collection[UUID]
) -> tuple[InvitationRef, bool]:
    """Deja creada la invitación de `email` con esos roles (ADR-020), o renueva la pendiente
    (§1, F2-130e). Devuelve la invitación y si se renovó.

    El correo se guarda en la forma canónica de las cuentas (D-F2-3) y solo si `core.mail`
    podría entregarlo: `InvalidEmail`. Las reglas son las de asignar esos roles, y además el
    permiso `users.invite`; renovar, también las de revocar con los que tenía (más estricto que
    ADR-020 §1: cambiarlos los quita). Lanza `AccessDenied`, `UnknownRole` (un rol no es de esta
    organización) y lo que lanzan `create_invitation` y `renew_invitation`. Si lanza, no escribe.
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
        pending = organizations.pending_invitation(ctx, email=address)
        if pending is None:
            return create_invitation(ctx, email=address, role_ids=role_ids), False
        had = invitation_role_ids(ctx, invitation_id=pending)
        ensure_can_revoke_invitation(ctx, role_ids=had)
        return organizations.renew_invitation(ctx, invitation_id=pending, role_ids=role_ids), True


def resend_invitation(ctx: TenantContext, *, invitation_id: UUID) -> InvitationRef:
    """Reenvía una invitación pendiente, caducada o no: la renueva con sus mismos roles y las
    reglas de invitar con ellos (ADR-020 §1), en el orden de revocar: permisos y bloqueo de RBAC,
    después la fila. Lanza `AccessDenied`, `RoleGone`, `DoesNotExist` (no es de esta
    organización) y lo que lanza `renew_invitation`. Si lanza, no escribe."""
    alias = require_scope(ctx)
    with transaction.atomic(using=alias):
        ensure_can_revoke_invitation(ctx, role_ids=())
        roles = invitation_role_ids(ctx, invitation_id=invitation_id)
        try:
            ensure_can_invite(ctx, role_ids=roles)
        except UnknownRole:
            raise RoleGone from None
        return organizations.renew_invitation(ctx, invitation_id=invitation_id, role_ids=roles)


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


class NotAcceptable(Exception):
    """La invitación ya no se puede aceptar (ADR-020 §2 y §4): `args`, la razón para la auditoría
    y, si es de quien invitó, la denegación."""


def accept_invitation(
    ctx: TenantContext, invitation_id: UUID, token_hash: str, **account: Any
) -> tuple[str, bool]:
    """Acepta la invitación que encontró el hash de su enlace, en el scope `SYSTEM` de su
    organización (ADR-020 §4). Devuelve el nombre de la organización y si se creó la cuenta.
    Orden de invitar y revocar: bloqueo de RBAC, fila releída y bloqueada, reglas de quien invitó
    y ninguna membresía; después, la cuenta (`invited_account`). Lanza `NotAcceptable`,
    `AccountExists` o `ValidationError` (cuenta nueva). Si lanza, no escribe.
    """
    with transaction.atomic(using=require_scope(ctx)):
        try:
            lock_rbac(ctx)
            found = organizations.locked_invitation(ctx, invitation_id, token_hash)
            if found is None:
                raise NotAcceptable("stale")
            name, invitation = found
            ensure_inviter_can_grant(ctx, invitation.invited_by_id, invitation.role_ids)
        except AccessDenied as denied:
            raise NotAcceptable("inviter", denied.reason) from None
        except UnknownRole:
            raise NotAcceptable("role") from None
        if organizations.has_membership(ctx, email=invitation.email):
            raise NotAcceptable("member")
        try:
            user_id, created = invited_account(invitation.email, **account)
            membership_id = organizations.accept_invitation(ctx, invitation, user_id, created)
        except StaffAccount:
            raise NotAcceptable("account") from None
        except AlreadyMember:
            raise NotAcceptable("member") from None
        grant_invited_roles(ctx, membership_id=membership_id, role_ids=invitation.role_ids)
    return name, created
