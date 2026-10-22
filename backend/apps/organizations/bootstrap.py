"""Frontera de alta de `organizations` (F2-06): la organización nace con su primera membresía.

Solo la importa `apps.provisioning` (contrato de import-linter). La gestión de membresías llega
con E01-06 y E01-07, con sus propios servicios. La única membresía sin invitación, aparte de la
del Owner inicial, es la de las cuentas de prueba (ADR-026 §4).
"""

import re
from uuid import UUID

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction

from apps.organizations.models import Organization, OrganizationMembership
from core.tenancy.context import ActorType, TenantContext
from core.tenancy.scope import require_scope

# Minúsculas, dígitos y guiones; empieza y acaba en alfanumérico. La URL lo usa tal cual.
SLUG = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
NAME_MAX = Organization._meta.get_field("name").max_length or 0


class SlugTaken(Exception):
    """Ya existe una organización con ese slug."""


def clean(slug: str, name: str) -> str:
    """Valida el slug y devuelve el nombre sin espacios exteriores."""
    if not SLUG.fullmatch(slug) or "--" in slug:
        raise ValidationError("Slug inválido: minúsculas, dígitos y guiones", code="invalid_slug")
    name = name.strip()
    if not name or len(name) > NAME_MAX or not name.isprintable():
        raise ValidationError(f"Nombre obligatorio, de hasta {NAME_MAX} caracteres", code="name")
    return name


def create_organization(
    ctx: TenantContext, *, slug: str, name: str, owner_user_id: UUID
) -> OrganizationMembership:
    """Crea la organización cuyo id es el de `ctx` y, en la misma llamada, su membresía ACTIVE.

    No hay otra forma de añadir una "primera" membresía: solo la tiene la organización que nace
    aquí, en la transacción de su `tenant_scope`.
    """
    alias = require_scope(ctx)
    if (ctx.actor_type, ctx.user_id, ctx.actor_id) != (ActorType.SYSTEM, None, None):
        raise PermissionDenied("El alta de una organización no se hace en nombre de un actor")
    organization = Organization(id=ctx.organization_id, slug=slug, name=clean(slug, name))
    organization.full_clean(validate_unique=False)
    try:
        with transaction.atomic(using=alias):  # savepoint: el scope sigue utilizable si falla
            organization.save(using=alias, force_insert=True)
    except IntegrityError as error:
        raise SlugTaken(slug) from error
    membership: OrganizationMembership = OrganizationMembership.objects.using(alias).create(
        user_id=owner_user_id, status=OrganizationMembership.Status.ACTIVE
    )
    return membership


def organization_exists(organization_id: UUID) -> bool:
    """Sin tenant (`organizations` es platform-owned): ¿llegó a confirmarse el alta?"""
    return Organization.objects.filter(pk=organization_id).exists()


def add_test_membership(ctx: TenantContext, *, user_id: UUID) -> tuple[UUID, bool]:
    """La membresía de una cuenta de prueba, como `(id, creada)` (ADR-026 §4).

    Solo en la organización de pruebas configurada (`TEST_ACCOUNTS_ORGANIZATION`) y en nombre
    del sistema. Si ya existe se devuelve tal cual, con el estado que tenga: un operador pudo
    suspenderla, y volver a provisionar no la reactiva.
    """
    alias = require_scope(ctx)
    if (ctx.actor_type, ctx.user_id, ctx.actor_id) != (ActorType.SYSTEM, None, None):
        raise PermissionDenied("Las cuentas de prueba no se añaden en nombre de un actor")
    test_slug = settings.TEST_ACCOUNTS_ORGANIZATION
    organization = Organization.objects.using(alias).get(pk=ctx.organization_id)
    if not test_slug or organization.slug != test_slug:
        raise PermissionDenied("Solo la organización de pruebas admite membresías sin invitación")
    memberships = OrganizationMembership.objects.using(alias)
    with transaction.atomic(using=alias):
        existing = memberships.filter(user_id=user_id).values_list("pk", flat=True).first()
        if existing is not None:
            return existing, False
        created = memberships.create(user_id=user_id, status=OrganizationMembership.Status.ACTIVE)
    return created.pk, True


def active_membership_id(ctx: TenantContext, *, user_id: UUID) -> UUID | None:
    """La membresía ACTIVE de esa cuenta en la organización del contexto, o `None`."""
    alias = require_scope(ctx)
    found: UUID | None = (
        OrganizationMembership.objects.using(alias)
        .filter(user_id=user_id, status=OrganizationMembership.Status.ACTIVE)
        .values_list("pk", flat=True)
        .first()
    )
    return found
