"""Servicios de `organizations`: el estado de una membresía (F2-19, ADR-017), su sucursal
(F2-69) y las invitaciones (F2-80, ADR-020), con lo que su correo lee y escribe (F2-130d).

No comprueban permisos ni las reglas de RBAC: `organizations` no importa `access`. Solo los
llama `apps.members`, después de las reglas de `access.services` y en el mismo `tenant_scope`
(contrato de import-linter).
"""

import math
from collections.abc import Collection
from datetime import datetime, timedelta
from typing import NamedTuple
from uuid import UUID

from django.db import IntegrityError, connection, transaction
from django.db.models import F, Max
from django.db.models.functions import Now
from django.utils import timezone

from apps.audit.services import Entity, record
from apps.organizations.models import (
    INVITATION_MAX_ROLES,
    INVITATION_MAX_SENDS,
    Branch,
    Organization,
    OrganizationMembership,
    UserInvitation,
)
from core import outbox
from core.tenancy.context import TenantContext
from core.tenancy.resolution import ACCESSIBLE_STATUSES
from core.tenancy.scope import require_scope

Status = OrganizationMembership.Status
# Estado pedido → (único estado desde el que se llega, acción de auditoría).
_SWITCH = {
    Status.SUSPENDED: (Status.ACTIVE, "membership.suspended"),
    Status.ACTIVE: (Status.SUSPENDED, "membership.reactivated"),
}


class InvalidTransition(Exception):
    """La membresía, o la invitación, no está en un estado desde el que se pueda cambiar."""


class UnknownBranch(Exception):
    """La sucursal pedida no es de esta organización."""


# ADR-020 §2 y §3: constantes del código.
INVITATION_LIFETIME = timedelta(days=7)
INVITATIONS_PENDING_MAX = 50
INVITATIONS_DAILY_MAX = 100
INVITATION_SEND_GAP = timedelta(minutes=15)  # entre dos envíos al mismo correo (§3)
SEND_REQUESTED = "invitation.send_requested"  # evento del outbox: solo identificadores
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


class SendTooSoon(Exception):
    """La organización envió una invitación a ese correo hace menos de `INVITATION_SEND_GAP`.
    `wait`: los segundos que faltan."""

    def __init__(self, wait: int) -> None:
        super().__init__(wait)
        self.wait = wait


class InvitationRef(NamedTuple):
    """Una invitación, para quien no importa los modelos del módulo (ADR-017)."""

    id: UUID
    email: str
    role_ids: list[UUID]
    status: str
    expires_at: datetime
    invited_by_id: UUID
    created_at: datetime


def shown_status(status: str, expires_at: datetime) -> str:
    """El estado que se enseña de una invitación (ADR-020 §3): una pendiente cuya fecha pasó
    está caducada, aunque ningún proceso lo escriba en la tabla."""
    if status == UserInvitation.Status.PENDING and expires_at <= timezone.now():
        return UserInvitation.Status.EXPIRED
    return status


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

    Con ella, el evento que pide su correo (`SEND_REQUESTED`, F2-130d).
    `email` llega ya en su forma canónica y los roles, ya comprobados: aquí no se miran. Lanza
    `AlreadyMember`, `InvitationPending`, `SendTooSoon`, `TooManyPending` o `TooManyInvitations`,
    y entonces no escribe. Los topes se cuentan sobre la tabla: quien llama tiene el bloqueo de
    RBAC de la organización, y dos invitaciones no los cuentan a la vez. Una pendiente caducada
    cuenta como pendiente, también para su correo: sigue ocupando su sitio hasta que se revoque.
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
        if wait := _send_wait(alias, email):
            raise SendTooSoon(wait)
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
        outbox.emit(ctx, SEND_REQUESTED, aggregate_type="invitation", aggregate_id=invitation.pk)
    return InvitationRef(
        invitation.pk,
        email,
        invitation.role_ids,
        invitation.status,
        invitation.expires_at,
        invitation.invited_by_id,
        invitation.created_at,
    )


def invitation_role_ids(ctx: TenantContext, *, invitation_id: UUID) -> list[UUID]:
    """Los roles que daría una invitación de la organización de `ctx`. La de otra organización
    no existe: `DoesNotExist`."""
    rows = UserInvitation.objects.using(require_scope(ctx))
    roles: list[UUID] = rows.values_list("role_ids", flat=True).get(pk=invitation_id)
    return roles


def revoke_invitation(ctx: TenantContext, *, invitation_id: UUID) -> InvitationRef:
    """Revoca una invitación pendiente, caducada o no, de la organización de `ctx`, borra el
    hash de su enlace y lo audita (ADR-020). Devuelve la invitación como queda. Una ya revocada
    se devuelve tal cual: repetirlo no escribe nada.

    Una aceptada o anotada como caducada no se revoca: `InvalidTransition`. La de otra
    organización no existe: `DoesNotExist`. No se borra ninguna fila: revocar es un estado.
    """
    alias = require_scope(ctx)
    status = UserInvitation.Status
    with transaction.atomic(using=alias):  # savepoint: el cambio y su auditoría, o ninguno
        rows = UserInvitation.objects.using(alias).select_for_update(no_key=True)
        invitation = rows.get(pk=invitation_id)
        if invitation.status == status.PENDING:
            invitation.status, invitation.token_hash = status.REVOKED, None
            invitation.save(using=alias, update_fields=["status", "token_hash", "updated_at"])
            changes = {"status": [status.PENDING.value, status.REVOKED.value]}
            entity = Entity("invitation", invitation.pk)
            record(ctx, "membership.invitation_revoked", entity, changes)
        elif invitation.status != status.REVOKED:
            raise InvalidTransition(invitation.status)
    return InvitationRef(
        invitation.pk,
        invitation.email,
        invitation.role_ids,
        invitation.status,
        invitation.expires_at,
        invitation.invited_by_id,
        invitation.created_at,
    )


def find_invitation(token_hash: str) -> tuple[UUID, UUID] | None:
    """La organización y el id de la invitación pendiente y sin caducar cuyo enlace tiene ese
    hash, o `None` (ADR-020 §3). Sin tenant: `resolve_invitation` es la única lectura de la tabla
    fuera de RLS, y no dice por qué no encontró nada."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT * FROM public.resolve_invitation(%s)", [token_hash])
        row = cursor.fetchone()
    return (row[0], row[1]) if row else None


def previewed_invitation(
    ctx: TenantContext, *, invitation_id: UUID, token_hash: str
) -> tuple[str, str] | None:
    """El nombre de la organización de `ctx` y el correo invitado, si la invitación sigue
    pendiente, sin caducar y con ese hash, y la organización admite acceso; si no, `None`. Lo
    vuelve a comprobar dentro del scope, con el reloj de la base: no escribe nada."""
    alias = require_scope(ctx)
    pending = UserInvitation.objects.using(alias).filter(
        pk=invitation_id,
        token_hash=token_hash,
        status=UserInvitation.Status.PENDING,
        expires_at__gt=Now(),
    )
    email = pending.values_list("email", flat=True).first()
    organizations = Organization.objects.using(alias).filter(
        pk=ctx.organization_id, status__in=ACCESSIBLE_STATUSES
    )
    name = organizations.values_list("name", flat=True).first()
    return (name, email) if email is not None and name is not None else None


def locked_invitation(
    ctx: TenantContext, invitation_id: UUID, token_hash: str
) -> tuple[str, InvitationRef] | None:
    """El nombre de la organización de `ctx` y la invitación, bloqueada hasta el final del scope,
    si sigue pendiente, sin caducar (reloj de la base), con ese hash y la organización admite
    acceso; si no, `None` (ADR-020 §3). Otra aceptación espera antes, en el bloqueo de RBAC; el de
    la fila protege de quien escribe sin él (el envío, 130c: RBAC y después fila, o bloquearla)."""
    alias = require_scope(ctx)
    organizations = Organization.objects.using(alias).filter(
        pk=ctx.organization_id, status__in=ACCESSIBLE_STATUSES
    )
    name = organizations.values_list("name", flat=True).first()
    rows = UserInvitation.objects.using(alias).select_for_update(no_key=True)
    rows = rows.filter(pk=invitation_id, token_hash=token_hash)
    invitation = rows.filter(status=UserInvitation.Status.PENDING, expires_at__gt=Now()).first()
    if name is None or invitation is None:
        return None
    fields = ("pk", "email", "role_ids", "status", "expires_at", "invited_by_id", "created_at")
    return name, InvitationRef(*(getattr(invitation, field) for field in fields))


def has_membership(ctx: TenantContext, *, email: str) -> bool:
    """¿La cuenta de `email` tiene membresía en la organización de `ctx`, en el estado que sea?"""
    memberships = OrganizationMembership.objects.using(require_scope(ctx))
    return memberships.filter(user__email=email).exists()


def accept_invitation(
    ctx: TenantContext, invitation: InvitationRef, user_id: UUID, user_created: bool
) -> UUID:
    """Crea la membresía activa de `user_id`, deja la invitación (ya bloqueada y comprobada)
    `ACCEPTED` sin el hash de su enlace y audita `membership.activated` (ADR-020 §4 y §6).
    Devuelve la membresía. Una que apareció a la vez: `AlreadyMember`."""
    alias = require_scope(ctx)
    try:
        with transaction.atomic(using=alias):
            membership = OrganizationMembership.objects.using(alias).create(
                user_id=user_id, status=Status.ACTIVE
            )
    except IntegrityError:
        raise AlreadyMember from None
    UserInvitation.objects.using(alias).filter(pk=invitation.id).update(
        status=UserInvitation.Status.ACCEPTED, accepted_at=Now(), token_hash=None, updated_at=Now()
    )
    metadata = {
        "via": "invitation",
        "invitation_id": invitation.id,
        "user_id": user_id,
        "user_created": user_created,
        "invited_by": invitation.invited_by_id,
        "role_ids": [str(role_id) for role_id in invitation.role_ids],
    }
    changes = {"status": [None, Status.ACTIVE.value]}
    record(ctx, "membership.activated", Entity("membership", membership.pk), changes, metadata)
    return UUID(str(membership.pk))


def _send_wait(alias: str, email: str) -> int:
    """Segundos que faltan para poder enviar otra vez a `email` (ADR-020 §3): los cuenta el
    último envío de cualquier invitación de la organización a ese correo, en cualquier estado.
    Quien llama tiene el bloqueo de RBAC: los envíos a un correo se cuentan en serie."""
    rows = UserInvitation.objects.using(alias).filter(email=email)
    last = rows.aggregate(last=Max("sent_at"))["last"]
    left = (last + INVITATION_SEND_GAP - timezone.now()).total_seconds() if last else 0
    return max(0, math.ceil(left))


def invitation_to_send(
    ctx: TenantContext, *, invitation_id: UUID, requested_at: datetime
) -> tuple[str, InvitationRef] | str:
    """El nombre de la organización de `ctx` y la invitación, bloqueada hasta el final del scope,
    si ahora se le puede enviar el correo que se pidió en `requested_at` (ADR-020 §3). Si no, la
    razón: `state` (no está pendiente, caducó o la organización no admite acceso), `served` (ese
    pedido ya se envió), `limit` (sus 5 envíos), `too_soon` (15 minutos desde el último envío a
    ese correo) o `member` (el correo ya tiene membresía). Quien llama tiene el bloqueo de RBAC."""
    alias = require_scope(ctx)
    organizations = Organization.objects.using(alias).filter(
        pk=ctx.organization_id, status__in=ACCESSIBLE_STATUSES
    )
    name = organizations.values_list("name", flat=True).first()
    rows = UserInvitation.objects.using(alias).select_for_update(no_key=True)
    rows = rows.filter(pk=invitation_id, status=UserInvitation.Status.PENDING)
    invitation = rows.filter(expires_at__gt=Now()).first()
    if name is None or invitation is None:
        return "state"
    if invitation.sent_at is not None and invitation.sent_at >= requested_at:
        return "served"
    if invitation.send_count >= INVITATION_MAX_SENDS:
        return "limit"
    if _send_wait(alias, invitation.email):
        return "too_soon"
    if has_membership(ctx, email=invitation.email):
        return "member"
    fields = ("pk", "email", "role_ids", "status", "expires_at", "invited_by_id", "created_at")
    return name, InvitationRef(*(getattr(invitation, field) for field in fields))


def store_invitation_link(ctx: TenantContext, *, invitation_id: UUID, token_hash: str) -> None:
    """Guarda el hash del enlace recién generado de una invitación ya bloqueada
    (`invitation_to_send`), con la hora del envío y un envío más. El enlace anterior deja de
    valer. La base no deja pasar de `INVITATION_MAX_SENDS`."""
    UserInvitation.objects.using(require_scope(ctx)).filter(pk=invitation_id).update(
        token_hash=token_hash, sent_at=Now(), send_count=F("send_count") + 1, updated_at=Now()
    )
