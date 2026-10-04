"""Servicios de `organizations`: el estado de una membresía (F2-19, ADR-017).

No comprueban permisos ni las reglas de RBAC: `organizations` no importa `access`. Solo los
llama `apps.members`, después de `access.services.ensure_can_manage_member` y en el mismo
`tenant_scope` (contrato de import-linter).
"""

from uuid import UUID

from django.db import transaction

from apps.audit.services import Entity, record
from apps.organizations.models import OrganizationMembership
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
