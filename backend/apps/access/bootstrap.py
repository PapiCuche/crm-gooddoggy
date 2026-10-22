"""Frontera de alta de `access` (F2-06): el Owner inicial de una organización nueva.

Solo la importa `apps.provisioning` (contrato de import-linter). No es una excepción de
`assign_role` ni de `grant_permission`, que siguen exigiendo un actor con permisos.
"""

from uuid import UUID

from django.apps import apps
from django.db import transaction

from apps.access.models import MembershipRole, Role
from apps.access.selectors import ACTIVE, AccessDenied, Denied
from apps.access.services import _audit_assignment, clone_role_templates
from core.tenancy.context import ActorType, TenantContext
from core.tenancy.scope import require_scope


class AlreadyBootstrapped(Exception):
    """La organización ya tiene roles u otros miembros: su RBAC cambia con los servicios."""


def install_initial_owner(ctx: TenantContext, *, membership_id: UUID) -> Role:
    """Clona las plantillas y da el rol Owner a la única membresía de una organización sin roles.

    El actor SYSTEM es el de cualquier contexto que no viene de HTTP, así que no prueba nada por
    sí solo. Lo que acota la frontera es el estado (sin roles, sin otra membresía, usuario
    activo) y quién puede importarla. Dos llamadas simultáneas sobre la misma organización las
    serializan los índices únicos de `roles`; en el alta no ocurre, porque la organización aún
    no está confirmada.
    """
    alias = require_scope(ctx)
    if (ctx.actor_type, ctx.user_id, ctx.actor_id) != (ActorType.SYSTEM, None, None):
        raise AccessDenied(Denied.MEMBERSHIP)
    memberships = apps.get_model("organizations", "OrganizationMembership")._default_manager
    with transaction.atomic(using=alias):
        others = memberships.using(alias).exclude(pk=membership_id)
        if Role.objects.using(alias).exists() or others.exists():
            raise AlreadyBootstrapped
        clone_role_templates(ctx)
        owner: Role = Role.objects.using(alias).get(is_owner_role=True)
        # De esta organización, y con un usuario que cuenta como Owner activo (`_owner_remains`).
        memberships.using(alias).get(pk=membership_id, status=ACTIVE, user__is_active=True)
        MembershipRole.objects.using(alias).create(membership_id=membership_id, role=owner)
        _audit_assignment(ctx, "membership.role_assigned", membership_id, owner, [None, owner.pk])
    return owner


def holds_owner_role(ctx: TenantContext, *, user_id: UUID) -> bool:
    """¿Tiene esa cuenta el rol Owner en la organización del contexto? Lo localiza el flag."""
    alias = require_scope(ctx)
    return (
        MembershipRole.objects.using(alias)
        .filter(role__is_owner_role=True, membership__user_id=user_id)
        .exists()
    )


def grant_template_role(ctx: TenantContext, *, membership_id: UUID, template: str) -> bool:
    """Da a una membresía el rol clonado de la plantilla `template` (ADR-026 §4); `True` si lo
    añadió. Solo el rol de plantilla (`is_system`) con ese código: nunca uno propio que se llame
    igual, y nunca por el nombre. No quita nada. Lo usan las cuentas de prueba, en nombre del
    sistema.
    """
    alias = require_scope(ctx)
    if (ctx.actor_type, ctx.user_id, ctx.actor_id) != (ActorType.SYSTEM, None, None):
        raise AccessDenied(Denied.MEMBERSHIP)
    with transaction.atomic(using=alias):
        role: Role = Role.objects.using(alias).get(is_system=True, code=template)
        assignments = MembershipRole.objects.using(alias)
        if assignments.filter(membership_id=membership_id, role=role).exists():
            return False
        assignments.create(membership_id=membership_id, role=role)
        _audit_assignment(ctx, "membership.role_assigned", membership_id, role, [None, role.pk])
    return True


def template_role_name(ctx: TenantContext, *, membership_id: UUID, template: str) -> str | None:
    """El nombre del rol de plantilla `template` si esa membresía lo tiene, o `None`. Solo para
    enseñarlo: no autoriza nada (ADR-026 §5)."""
    alias = require_scope(ctx)
    name: str | None = (
        MembershipRole.objects.using(alias)
        .filter(membership_id=membership_id, role__is_system=True, role__code=template)
        .values_list("role__name", flat=True)
        .first()
    )
    return name
