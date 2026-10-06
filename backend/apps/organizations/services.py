"""Servicios de `organizations`: el estado de una membresía (F2-19, ADR-017) y su sucursal
(F2-69).

No comprueban permisos ni las reglas de RBAC: `organizations` no importa `access`. Solo los
llama `apps.members`, después de `access.services.ensure_can_manage_member` y en el mismo
`tenant_scope` (contrato de import-linter).
"""

from typing import NamedTuple
from uuid import UUID

from django.db import transaction

from apps.audit.services import Entity, record
from apps.organizations.models import Branch, OrganizationMembership
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
