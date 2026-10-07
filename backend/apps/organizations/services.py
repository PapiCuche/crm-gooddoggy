"""Servicios de `organizations`: el estado de una membresía (F2-19, ADR-017), su sucursal
(F2-69) y las invitaciones (F2-80, ADR-020).

No comprueban permisos ni las reglas de RBAC: `organizations` no importa `access`. Solo los
llama `apps.members`, después de las reglas de `access.services` y en el mismo `tenant_scope`
(contrato de import-linter).
"""

from collections.abc import Collection
from datetime import datetime, timedelta
from typing import NamedTuple
from uuid import UUID

from django.db import transaction
from django.utils import timezone

from apps.audit.services import Entity, record
from apps.organizations.models import (
    INVITATION_MAX_ROLES,
    Branch,
    OrganizationMembership,
    UserInvitation,
)
from core.tenancy.context import TenantContext
from core.tenancy.scope import require_scope

Status = OrganizationMembership.Status
# Estado pedido → (único estado desde el que se llega, acción de auditoría).
_SWITCH = {
    Status.SUSPENDED: (Status.ACTIVE, "membership.suspended"),
    Status.ACTIVE: (Status.SUSPENDED, "membership.reactivated"),
}


class InvalidTransition(Exception):
    """La membresía no está en un estado desde el que se pueda suspender o reactivar."""


class UnknownBranch(Exception):
    """La sucursal pedida no es de esta organización."""


# ADR-020 §2 y §3: constantes del código.
INVITATION_LIFETIME = timedelta(days=7)
INVITATIONS_PENDING_MAX = 50
INVITATIONS_DAILY_MAX = 100
# Para quien no importa los modelos del módulo (ADR-017).
INVITATION_ROLES_MAX = INVITATION_MAX_ROLES
INVITATION_STATUSES = UserInvitation.Status.choices


class AlreadyMember(Exception):
    """El correo ya tiene membresía en esta organización, en el estado que sea."""


class InvitationPending(Exception):
    """El correo ya tiene una invitación pendiente en esta organización."""


class TooManyPending(Exception):
    """La organización llegó a su tope de invitaciones pendientes."""


class TooManyInvitations(Exception):
    """La organización llegó a su tope de invitaciones creadas en 24 horas."""


class InvitationRef(NamedTuple):
    """Una invitación, para quien no importa los modelos del módulo (ADR-017)."""

    id: UUID
    email: str
    role_ids: list[UUID]
    status: str
    expires_at: datetime


class BranchRef(NamedTuple):
    """La sucursal de una membresía, para quien no importa los modelos del módulo (ADR-017)."""

    id: UUID
    code: str
    name: str


def set_membership_status(ctx: TenantContext, *, membership_id: UUID, status: str) -> UUID | None:
    """Suspende (`ACTIVE` → `SUSPENDED`) o reactiva (`SUSPENDED` → `ACTIVE`) una membresía de
    la organización de `ctx`, y lo audita. Devuelve el usuario de la membresía si cambió, y
    `None` si ya estaba así: repetirlo no escribe nada.

    `INVITED` y `DEACTIVATED` no se tocan: `InvalidTransition`. Una membresía de otra
    organización no existe: `DoesNotExist`.
    """
    alias = require_scope(ctx)
    if status not in _SWITCH:
        raise ValueError(f"estado no admitido: {status!r}")
    wanted = Status(status)
    origin, action = _SWITCH[wanted]
    with transaction.atomic(using=alias):  # savepoint: el cambio y su auditoría, o ninguno
        rows = OrganizationMembership.objects.using(alias).select_for_update(no_key=True)
        membership = rows.get(pk=membership_id)
        if membership.status == wanted:
            return None
        if membership.status != origin:
            raise InvalidTransition(membership.status)
        membership.status = wanted
        membership.save(using=alias, update_fields=["status", "updated_at"])
        changes = {"status": [origin.value, wanted.value]}
        record(ctx, action, Entity("membership", membership.pk), changes)
    user_id: UUID = membership.user_id
    return user_id


def set_membership_branch(
    ctx: TenantContext, *, membership_id: UUID, branch_id: UUID | None
) -> BranchRef | None:
    """Deja a una membresía de la organización de `ctx` con esa sucursal, o sin ninguna
    (`None`), y lo audita. Devuelve la sucursal que queda. Repetirlo no escribe nada.

    La sucursal se busca dentro del tenant antes de escribir: una de otra organización no
    existe (`UnknownBranch`), igual que una que nadie creó. Una membresía de otra organización
    tampoco: `DoesNotExist`. No mira el estado de la membresía ni si la sucursal está activa.
    """
    alias = require_scope(ctx)
    if branch_id is not None:
        branch_id = UUID(str(branch_id))  # también si llega como texto: «ya la tiene» compara UUID
    with transaction.atomic(using=alias):  # savepoint: el cambio y su auditoría, o ninguno
        rows = OrganizationMembership.objects.using(alias).select_for_update(no_key=True)
        membership = rows.get(pk=membership_id)
        branch = None
        if branch_id is not None:
            branch = Branch.objects.using(alias).filter(pk=branch_id).first()
            if branch is None:
                raise UnknownBranch(branch_id)
        left = BranchRef(branch.pk, branch.code, branch.name) if branch else None
        before = membership.default_branch_id
        if before == branch_id:
            return left
        membership.default_branch = branch
        membership.save(using=alias, update_fields=["default_branch", "updated_at"])
        changes = {"default_branch": [before, branch_id]}
        named = {"branch": branch.code} if branch else {}
        record(
            ctx, "membership.branch_changed", Entity("membership", membership.pk), changes, named
        )
    return left


def create_invitation(
    ctx: TenantContext, *, email: str, role_ids: Collection[UUID]
) -> InvitationRef:
    """Crea la invitación de `email` a la organización de `ctx` con esos roles, pendiente, sin
    enlace y con su caducidad, y lo audita (ADR-020). Invita el usuario de `ctx`.

    `email` llega ya en su forma canónica y los roles, ya comprobados: aquí no se miran. Lanza
    `AlreadyMember`, `InvitationPending`, `TooManyPending` o `TooManyInvitations`, y entonces no
    escribe. Los topes se cuentan sobre la tabla: quien llama tiene el bloqueo de RBAC de la
    organización, y dos invitaciones no los cuentan a la vez. Una pendiente caducada cuenta
    como pendiente, también para su correo: sigue ocupando su sitio hasta que se revoque.
    """
    alias = require_scope(ctx)
    if ctx.user_id is None:
        raise ValueError("una invitación la crea una persona")
    with transaction.atomic(using=alias):  # savepoint: la invitación y su auditoría, o ninguna
        if OrganizationMembership.objects.using(alias).filter(user__email=email).exists():
            raise AlreadyMember
        rows = UserInvitation.objects.using(alias)
        pending = rows.filter(status=UserInvitation.Status.PENDING)
        if pending.filter(email=email).exists():
            raise InvitationPending
        if pending.count() >= INVITATIONS_PENDING_MAX:
            raise TooManyPending
        now = timezone.now()
        if rows.filter(created_at__gt=now - timedelta(hours=24)).count() >= INVITATIONS_DAILY_MAX:
            raise TooManyInvitations
        invitation = rows.create(
            email=email,
            role_ids=sorted(UUID(str(role_id)) for role_id in role_ids),
            expires_at=now + INVITATION_LIFETIME,
            invited_by_id=ctx.user_id,
        )
        changes = {
            "email": [None, email],
            "role_ids": [None, [str(role_id) for role_id in invitation.role_ids]],
        }
        record(ctx, "membership.invited", Entity("invitation", invitation.pk), changes)
    return InvitationRef(
        invitation.pk, email, invitation.role_ids, invitation.status, invitation.expires_at
    )
