"""Servicios de `access`: roles plantilla y cambios de RBAC sin escalada (ADR-003 §5, F2-05C).

Crear, renombrar y borrar un rol, conceder o retirar un permiso de un rol, y asignar o quitar
un rol a una membresía. Cada cambio corre en un savepoint que toma el bloqueo del rol Owner de la
organización, relee los permisos del actor bajo ese bloqueo, comprueba todas las reglas, escribe
y audita. Una denegación no escribe nada. `is_owner_role` solo identifica al rol Owner para
tres restricciones (permisos sensibles, último Owner y que sus concesiones no se editan); por
sí solo no concede nada.

`ensure_can_manage_member` aplica las mismas reglas al estado de una membresía y a su sucursal,
que escribe `organizations` (F2-19, F2-69, ADR-017): comprueba y conserva el bloqueo; no escribe
ni audita.
"""

from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from django.apps import apps
from django.db import transaction
from django.utils.text import slugify

from apps.access import names
from apps.access.catalog import BY_CODE, ROLE_TEMPLATES, Scope
from apps.access.models import MembershipRole, Role, RolePermission
from apps.access.selectors import (
    ACTIVE,
    AccessDenied,
    Denied,
    ExecutionContext,
    definition,
    execution_context,
    require,
)
from apps.audit.services import Entity, record
from core.tenancy.context import TenantContext
from core.tenancy.scope import require_scope

# Un alcance cubre a otro si es igual o más amplio. TEAM y BRANCH no se contienen entre sí.
_COVERED_BY: dict[Scope, frozenset[Scope]] = {
    Scope.OWN: frozenset(Scope),
    Scope.TEAM: frozenset({Scope.TEAM, Scope.ORGANIZATION}),
    Scope.BRANCH: frozenset({Scope.BRANCH, Scope.ORGANIZATION}),
    Scope.ORGANIZATION: frozenset({Scope.ORGANIZATION}),
}


def covers(held: Iterable[Scope | None], wanted: Scope | None) -> bool:
    """¿Lo que tiene el actor alcanza para delegar `wanted`? (`None`: permiso sin alcance)."""
    if wanted is None:
        return None in held
    return any(scope in _COVERED_BY[wanted] for scope in held)


@dataclass(frozen=True, slots=True)
class _Actor:
    """Quien cambia el RBAC, con sus permisos releídos bajo el bloqueo."""

    alias: str
    ectx: ExecutionContext
    owner: Role
    is_owner: bool  # tiene asignado el rol Owner: condición extra, nunca una concesión

    def must_cover(self, grants: Iterable[tuple[str, str | None]]) -> None:
        """Nadie delega lo que no tiene, ni con más alcance; lo sensible, solo un Owner."""
        wanted = list(grants)
        for code, scope in wanted:  # un código retirado del catálogo tampoco se delega
            held = self.ectx.permissions.get(code, frozenset())
            if code not in BY_CODE or not covers(held, Scope(scope) if scope else None):
                raise AccessDenied(Denied.ESCALATION)
        if not self.is_owner and any(BY_CODE[code].is_sensitive for code, _ in wanted):
            raise AccessDenied(Denied.SENSITIVE)

    def holds(self, role: Role) -> bool:
        assigned = MembershipRole.objects.using(self.alias)
        return assigned.filter(membership_id=self.ectx.membership_id, role=role).exists()


def _lock_owner_role(alias: str) -> Role:
    """Bloqueo por organización: serializa los cambios de RBAC hasta el COMMIT del scope."""
    roles = Role.objects.using(alias).select_for_update(no_key=True)
    owner: Role | None = roles.filter(is_owner_role=True).first()
    if owner is None:  # sin rol Owner no se puede garantizar el invariante: se deniega
        raise AccessDenied(Denied.LAST_OWNER)
    return owner


def _actor(ctx: TenantContext, alias: str, code: str) -> ExecutionContext:
    """Permisos releídos ahora (nunca una foto anterior) y permiso de gestión."""
    ectx = execution_context(ctx)
    require(ectx, code)
    return ectx


@contextmanager
def _change(ctx: TenantContext, code: str) -> Iterator[_Actor]:
    """Savepoint, bloqueo, permisos releídos y permiso de gestión; en ese orden."""
    alias = require_scope(ctx)
    with transaction.atomic(using=alias):
        try:
            owner = _lock_owner_role(alias)
        except AccessDenied:
            _actor(ctx, alias, code)  # quien no puede gestionar no aprende que falta el rol Owner
            raise
        ectx = _actor(ctx, alias, code)  # después del bloqueo: ve lo ya confirmado por otros
        assigned = MembershipRole.objects.using(alias)
        is_owner = assigned.filter(membership_id=ectx.membership_id, role=owner).exists()
        yield _Actor(alias, ectx, owner, is_owner)


def _owner_remains(alias: str, owner: Role, without_membership_id: UUID) -> None:
    others = MembershipRole.objects.using(alias).filter(
        role=owner, membership__status=ACTIVE, membership__user__is_active=True
    )
    if not others.exclude(membership_id=without_membership_id).exists():
        raise AccessDenied(Denied.LAST_OWNER)


def ensure_owner_remains(ctx: TenantContext, *, without_membership_id: UUID) -> None:
    """Siempre queda un Owner activo sin contar a esa membresía. Solo esa garantía, sin reglas
    de escalada: suspender una membresía pasa por `ensure_can_manage_member` (F2-19).

    Toma el bloqueo del rol Owner: debe llamarse en el mismo `tenant_scope` y antes de escribir.
    No abre savepoint: el bloqueo dura hasta el final de ese scope, también si deniega.
    """
    alias = require_scope(ctx)
    _owner_remains(alias, _lock_owner_role(alias), without_membership_id)


def ensure_can_manage_member(ctx: TenantContext, *, membership_id: UUID, leaving: bool) -> None:
    """Reglas para suspender o reactivar otra membresía (F2-19, ADR-017) y para cambiar su
    sucursal (F2-69, con `leaving=False`). Para `apps.members`.

    Permiso `users.manage`; nadie cambia la suya; el actor cubre todas las concesiones de todos
    los roles del miembro, como para quitárselos (PO-1); y si `leaving` y el miembro es un Owner
    activo, queda otro Owner activo. Una membresía de otra organización no existe: `DoesNotExist`.

    Si no deniega, el bloqueo del rol Owner dura hasta el final del `tenant_scope`: quien llama
    escribe a continuación, en ese mismo scope. Una denegación lo libera y no escribe nada.
    """
    membership_id = UUID(str(membership_id))
    with _change(ctx, "users.manage") as actor:
        if membership_id == actor.ectx.membership_id:
            raise AccessDenied(Denied.SELF)
        status = _member(actor.alias, membership_id)
        held = RolePermission.objects.using(actor.alias)
        grants = held.filter(role__assignments__membership_id=membership_id)
        actor.must_cover(grants.values_list("permission_id", "scope"))
        assigned = MembershipRole.objects.using(actor.alias)
        holds = assigned.filter(membership_id=membership_id, role=actor.owner).exists()
        if leaving and status == ACTIVE and holds:  # si ya no está activa, no deja de contar
            _owner_remains(actor.alias, actor.owner, membership_id)


def _member(alias: str, membership_id: UUID) -> str:
    """Estado de la membresía. Es de esta organización (la filtra RLS), o no existe."""
    memberships = apps.get_model("organizations", "OrganizationMembership")._default_manager
    status: str = memberships.using(alias).values_list("status", flat=True).get(pk=membership_id)
    return status


NAME_MAX = Role._meta.get_field("name").max_length or 0
DESCRIPTION_MAX = Role._meta.get_field("description").max_length or 0


class RoleNameTaken(Exception):
    """Ya hay en la organización un rol cuyo nombre se lee igual (`names.key`)."""


def role_name(name: str) -> str:
    """El nombre tal como se guarda, o `ValueError` si no sirve. Lo usa también la API, para
    responder 400 con el mismo criterio."""
    name = names.spaced(name)
    printable = name.strip().isprintable()  # antes de limpiar: un salto de línea no es un espacio
    name = names.clean(name)
    if not (name and printable and len(name) <= NAME_MAX and names.legible(name)):
        raise ValueError(f"nombre obligatorio, legible e imprimible, hasta {NAME_MAX} caracteres")
    return name


def role_description(description: str) -> str:
    """La descripción tal como se guarda, o `ValueError` si no sirve."""
    description = description.strip()
    if len(description) > DESCRIPTION_MAX or not description.isprintable():
        raise ValueError(f"descripción imprimible y de hasta {DESCRIPTION_MAX} caracteres")
    return description


def _name_is_free(roles: Any, name: str) -> None:
    """Ningún rol de `roles` se lee igual que `name` (`names.key`), o `RoleNameTaken`."""
    wanted = names.key(name)
    if any(names.key(other) == wanted for other in roles.values_list("name", flat=True)):
        raise RoleNameTaken(name)


def _free_code(roles: Any, name: str) -> str:
    """Código para un rol nuevo: del nombre, con un sufijo si ya está tomado. Lo genera el
    servidor; se llama bajo el bloqueo de RBAC, que serializa las altas de la organización.
    El corte a 40 deja sitio al sufijo dentro de los 50 de la columna."""
    base = slugify(name)[:40].strip("-_") or "rol"
    taken = set(roles.filter(code__startswith=base).values_list("code", flat=True))
    return next(
        c for n in range(1, len(taken) + 2) if (c := base if n == 1 else f"{base}-{n}") not in taken
    )


def create_role(ctx: TenantContext, *, name: str, description: str = "") -> Role:
    """Crea un rol propio de la organización, vacío: sin concesiones y sin miembros (F2-29).

    Exige `roles.manage`. El nombre es obligatorio y no puede leerse igual que el de otro rol
    de la organización (`RoleNameTaken`). Un rol vacío no concede nada: no hay nada que cubrir.
    """
    name, description = role_name(name), role_description(description)
    with _change(ctx, "roles.manage") as actor:
        roles = Role.objects.using(actor.alias)
        _name_is_free(roles, name)
        role = Role(code=_free_code(roles, name), name=name, description=description)
        role.save(using=actor.alias)
        changes = {"name": [None, name], "description": [None, description]}
        record(
            ctx, "role.created", Entity("role", role.pk, role.name), changes, {"role": role.code}
        )
        return role


class RoleInUse(Exception):
    """El rol tiene miembros, en cualquier estado: primero se les quita."""


class RoleIsSystem(Exception):
    """Un rol de plantilla no se borra (modelo de datos §E.3): se edita."""


def _editable(actor: _Actor, role_id: UUID) -> Role:
    """El rol, si este actor puede cambiarlo o borrarlo: ni uno que tiene asignado (PO-2), ni
    el rol Owner, que no se edita sea quien sea el actor."""
    role: Role = Role.objects.using(actor.alias).get(pk=role_id)
    if actor.holds(role):
        raise AccessDenied(Denied.SELF)
    if role.pk == actor.owner.pk:
        raise AccessDenied(Denied.OWNER_ROLE)
    return role


def update_role(
    ctx: TenantContext, *, role_id: UUID, name: str | None = None, description: str | None = None
) -> Role:
    """Cambia el nombre o la descripción de un rol (F2-38); `None` deja el campo como está.

    Exige `roles.manage` y cubrir las concesiones del rol, como para borrarlo: quien asigna un
    rol lo elige por su nombre, y la respuesta de la ruta enseña esas concesiones. El nombre sigue
    las reglas del alta y no puede leerse igual que el de otro rol (`RoleNameTaken`); el código
    no cambia. Lo que no cambia nada no escribe ni audita.
    """
    name = None if name is None else role_name(name)
    description = None if description is None else role_description(description)
    with _change(ctx, "roles.manage") as actor:
        role = _editable(actor, role_id)
        held = RolePermission.objects.using(actor.alias).filter(role=role)
        actor.must_cover(held.values_list("permission_id", "scope"))
        changes: dict[str, list[str]] = {}
        if name is not None and name != role.name:
            _name_is_free(Role.objects.using(actor.alias).exclude(pk=role.pk), name)
            changes["name"], role.name = [role.name, name], name
        if description is not None and description != role.description:
            changes["description"] = [role.description, description]
            role.description = description
        if changes:
            role.save(using=actor.alias, update_fields=[*changes, "updated_at"])
            entity = Entity("role", role.pk, role.name)
            record(ctx, "role.updated", entity, changes, {"role": role.code})
        return role


def delete_role(ctx: TenantContext, *, role_id: UUID) -> None:
    """Borra un rol y sus concesiones (F2-38). Exige lo mismo que retirárselas una a una (como
    PO-1): cubrirlas todas y, si alguna es sensible, ser Owner. Un rol de plantilla no se borra
    (`RoleIsSystem`), y uno con miembros tampoco (`RoleInUse`): no se deja a nadie sin un rol.
    """
    with _change(ctx, "roles.manage") as actor:
        role = _editable(actor, role_id)
        held = RolePermission.objects.using(actor.alias).filter(role=role)
        grants = sorted(held.values_list("permission_id", "scope"), key=lambda grant: grant[0])
        actor.must_cover(grants)
        if role.is_system:  # una regla del producto, no una autorización: no decide quién puede
            raise RoleIsSystem(role.name)
        if MembershipRole.objects.using(actor.alias).filter(role=role).exists():
            raise RoleInUse(role.name)
        Role.objects.using(actor.alias).filter(pk=role.pk).delete()  # y, con él, sus concesiones
        changes = {"name": [role.name, None], "description": [role.description, None]}
        metadata = {"role": role.code, "permissions": [list(grant) for grant in grants]}
        record(ctx, "role.deleted", Entity("role", role.pk, role.name), changes, metadata)


class ScopeMismatch(ValueError):
    """El alcance pedido no corresponde al permiso: sobra, o falta."""


def grant_permission(ctx: TenantContext, *, role_id: UUID, code: str, scope: str | None) -> None:
    """Deja el rol con `code` y ese alcance: lo concede, o cambia el alcance que tenía (F2-31).

    Repetir la misma concesión no hace nada. Cambiar un alcance exige cubrir el que había y el
    nuevo: reducirlo es retirar parte de una concesión, y retirar exige lo mismo que conceder
    (como PO-1). Las concesiones del rol Owner no se editan, sea quien sea el actor.
    """
    wanted = Scope(scope) if scope is not None else None
    if (wanted is not None) != definition(code).supports_scope:
        raise ScopeMismatch(f"{code}: el alcance no corresponde a este permiso")
    with _change(ctx, "roles.manage") as actor:
        role = Role.objects.using(actor.alias).get(pk=role_id)
        if actor.holds(role):  # cambiar las concesiones de un rol propio es modificarse a uno mismo
            raise AccessDenied(Denied.SELF)
        if role.pk == actor.owner.pk:
            raise AccessDenied(Denied.OWNER_ROLE)
        grants = RolePermission.objects.using(actor.alias).filter(role=role, permission_id=code)
        held = list(grants.values_list("scope", flat=True))
        actor.must_cover([(code, wanted), *((code, old) for old in held)])
        if held == [wanted]:
            return
        entity = Entity("role", role.pk, role.name)
        if held:
            grants.update(scope=wanted)
            changes = {"permission": [code, code], "scope": [held[0], wanted]}
            record(ctx, "role.permission_scope_changed", entity, changes)
            return
        grants.create(
            role=role, permission_id=code, supports_scope=wanted is not None, scope=wanted
        )
        changes = {"permission": [None, code], "scope": [None, wanted]}
        record(ctx, "role.permission_granted", entity, changes)


def revoke_permission(ctx: TenantContext, *, role_id: UUID, code: str) -> None:
    """Retira `code` a un rol (F2-33). Exige lo mismo que concederlo (como PO-1): cubrir la
    concesión con su alcance y, si es sensible, ser Owner. Las concesiones del rol Owner no se
    editan. Si el rol no tiene esa concesión (también al repetir la llamada), `DoesNotExist`.
    """
    with _change(ctx, "roles.manage") as actor:
        role = Role.objects.using(actor.alias).get(pk=role_id)
        if actor.holds(role):  # PO-2: también al quitar
            raise AccessDenied(Denied.SELF)
        if role.pk == actor.owner.pk:
            raise AccessDenied(Denied.OWNER_ROLE)
        if "\x00" in code:  # PostgreSQL no admite NUL en un texto: esa concesión no existe
            raise RolePermission.DoesNotExist
        grant = RolePermission.objects.using(actor.alias).get(role=role, permission_id=code)
        actor.must_cover([(code, grant.scope)])
        grant.delete(using=actor.alias)
        changes = {"permission": [code, None], "scope": [grant.scope, None]}
        record(ctx, "role.permission_revoked", Entity("role", role.pk, role.name), changes)


def assign_role(ctx: TenantContext, *, membership_id: UUID, role_id: UUID) -> None:
    """Asigna un rol a otra membresía de la organización. Repetirlo no hace nada."""
    membership_id = UUID(str(membership_id))  # también si llega como texto: SELF compara UUID
    with _change(ctx, "users.manage") as actor:
        if membership_id == actor.ectx.membership_id:
            raise AccessDenied(Denied.SELF)
        role = Role.objects.using(actor.alias).get(pk=role_id)
        _member(actor.alias, membership_id)
        actor.must_cover(role.grants.using(actor.alias).values_list("permission_id", "scope"))
        _, created = MembershipRole.objects.using(actor.alias).get_or_create(
            membership_id=membership_id, role=role
        )
        if created:
            _audit_assignment(ctx, "membership.role_assigned", membership_id, role, [None, role.pk])


def remove_role(ctx: TenantContext, *, membership_id: UUID, role_id: UUID) -> None:
    """Quita un rol a otra membresía. Exige lo mismo que asignarlo, y que quede un Owner activo.

    Si la membresía no tiene ese rol (también al repetir la llamada), lanza `DoesNotExist`.
    """
    membership_id = UUID(str(membership_id))
    with _change(ctx, "users.manage") as actor:
        if membership_id == actor.ectx.membership_id:
            raise AccessDenied(Denied.SELF)
        assignment = MembershipRole.objects.using(actor.alias).select_related("role")
        found = assignment.get(membership_id=membership_id, role_id=role_id)
        role = found.role
        actor.must_cover(role.grants.using(actor.alias).values_list("permission_id", "scope"))
        if role.pk == actor.owner.pk:
            _owner_remains(actor.alias, actor.owner, membership_id)
        found.delete(using=actor.alias)
        _audit_assignment(ctx, "membership.role_removed", membership_id, role, [role.pk, None])


def _audit_assignment(
    ctx: TenantContext, action: str, membership_id: UUID, role: Role, change: list[UUID | None]
) -> None:
    record(ctx, action, Entity("membership", membership_id), {"role": change}, {"role": role.code})


def clone_role_templates(ctx: TenantContext) -> list[Role]:
    """Crea en la organización de `ctx` los roles plantilla que falten (idempotente).

    Un rol cuyo código ya existe no se toca: ni su nombre ni sus concesiones, de modo que las
    ediciones de la organización se conservan. El rol Owner se reconoce por `is_owner_role`,
    no por su código. Devuelve solo los roles creados en esta llamada.
    """
    alias = require_scope(ctx)  # misma transacción y GUC que el tenant_scope activo
    roles = Role.objects.using(alias)
    existing = set(roles.values_list("code", flat=True))
    has_owner = roles.filter(is_owner_role=True).exists()
    created = []
    for template in ROLE_TEMPLATES:
        if template.code in existing or (template.is_owner_role and has_owner):
            continue
        role = Role(
            code=template.code,
            name=template.name,
            is_system=True,
            is_owner_role=template.is_owner_role,
        )
        role.save(using=alias)
        RolePermission.objects.using(alias).bulk_create(
            RolePermission(
                organization_id=ctx.organization_id,
                role=role,
                permission_id=code,
                supports_scope=BY_CODE[code].supports_scope,
                scope=scope,
            )
            for code, scope in template.grants.items()
        )
        created.append(role)
    return created
