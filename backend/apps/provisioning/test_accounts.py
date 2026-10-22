"""Cuentas de prueba (ADR-026): una organización sintética con un Owner, un Administrador, un
Supervisor y un Vendedor, con los roles de plantilla de su nombre.

Solo con `TEST_ACCOUNTS_ORGANIZATION` activo, que `production` rechaza. Idempotente: crea lo que
falta y deja lo que hay. Nunca toca una organización cuyo Owner no sea la cuenta sintética.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from django.conf import settings
from django.core.exceptions import ValidationError

from apps.access.bootstrap import grant_template_role, holds_owner_role, template_role_name
from apps.accounts.bootstrap import (
    account_exists,
    active_account_id,
    check_password_for,
    test_account,
)
from apps.audit import platform
from apps.audit.services import Entity, Result, record
from apps.organizations.bootstrap import active_membership_id, add_test_membership
from apps.provisioning.services import bootstrap_organization, checked_trail
from core.tenancy.context import TenantContext
from core.tenancy.resolution import ACCESSIBLE_STATUSES, organization_by_slug
from core.tenancy.scope import tenant_scope

logger = logging.getLogger(__name__)
STARTED, DONE = "test_accounts.provision.started", "test_accounts.provisioned"
NAME = "Good Doggy · Pruebas"
# (parte local del correo, código de la plantilla de rol), en el orden en que se enseñan.
PROFILES = (
    ("owner", "owner"),
    ("administrador", "admin"),
    ("supervisor", "supervisor"),
    ("vendedor", "seller"),
)


class Disabled(Exception):
    """`TEST_ACCOUNTS_ORGANIZATION` está vacío: no hay accesos de prueba en este entorno."""


class NotTestOrganization(Exception):
    """El slug es de una organización que no es la de pruebas: no se toca."""


@dataclass
class Provisioned:
    slug: str
    organization_created: bool = False
    accounts_created: list[str] = field(default_factory=list)
    memberships_added: list[str] = field(default_factory=list)
    roles_added: list[str] = field(default_factory=list)


def email_for(slug: str, local: str) -> str:
    """El correo sintético de un perfil: dominio `.test`, reservado (RFC 6761)."""
    return f"{local}@{slug}.gooddoggy.test"


def emails(slug: str) -> list[str]:
    return [email_for(slug, local) for local, _ in PROFILES]


def missing_accounts() -> list[str]:
    """Los correos de prueba que aún no tienen cuenta: los que necesitarán contraseña."""
    slug = _slug()
    return [email for email in emails(slug) if not account_exists(email)]


def _slug() -> str:
    slug: str = settings.TEST_ACCOUNTS_ORGANIZATION
    if not slug:
        raise Disabled
    return slug


def provision(*, password: str | None, operator: str, reason: str) -> Provisioned:
    """Deja la organización de pruebas con sus cuatro cuentas. `password` es la de las cuentas
    que falten: se valida contra todas ellas antes de escribir nada, así que o valen todas o no se
    crea ninguna. Solo se guarda su hash."""
    slug = _slug()
    operator, reason = checked_trail(operator, reason)
    result = Provisioned(slug)
    owner_email = email_for(slug, "owner")
    missing = missing_accounts()
    if missing:
        if password is None:
            raise ValidationError("Faltan cuentas de prueba: hace falta su contraseña")
        check_password_for(password, missing)

    def secret() -> str:
        if password is None:  # una cuenta borrada entre la comprobación y el alta
            raise ValidationError("Faltan cuentas de prueba: hace falta su contraseña")
        return password

    found = organization_by_slug(slug)
    organization_id = found.id if found else None
    if organization_id is None:
        made = bootstrap_organization(
            slug=slug,
            name=NAME,
            owner_email=owner_email,
            owner_password=None if account_exists(owner_email) else secret(),
            operator=operator,
            reason=reason,
        )
        organization_id = made.organization_id
        result.organization_created = True
        if made.user_created:
            result.accounts_created.append(owner_email)

    entity = Entity("organization", organization_id, slug)
    trail = {"operator": operator, "reason": reason, "slug": slug}
    platform.record(STARTED, actor_type=platform.Actor.SYSTEM, entity=entity, metadata=trail)
    ctx = TenantContext(organization_id, "command")  # actor SYSTEM, sin usuario
    try:
        with tenant_scope(ctx):
            owner = test_account(owner_email, None) if account_exists(owner_email) else None
            if owner is None or not holds_owner_role(ctx, user_id=owner[0]):
                raise NotTestOrganization(slug)
            for local, template in PROFILES[1:]:
                _profile(ctx, email_for(slug, local), template, secret, result, trail)
    except Exception as error:
        try:
            platform.record(
                DONE,
                actor_type=platform.Actor.SYSTEM,
                entity=entity,
                metadata={**trail, "error": type(error).__name__},
                result=Result.FAILED,
            )
        except Exception:  # sin fila de resultado (ADR-013 §5): manda el error original
            logger.exception("cuentas de prueba: no se pudo registrar el resultado")
        raise
    platform.record(
        DONE,
        actor_type=platform.Actor.SYSTEM,
        entity=entity,
        metadata={**trail, **_summary(result)},
    )
    return result


def _profile(
    ctx: TenantContext,
    email: str,
    template: str,
    secret: Callable[[], str],
    result: Provisioned,
    trail: dict[str, str],
) -> None:
    user_id, created = test_account(email, None if account_exists(email) else secret())
    if created:
        result.accounts_created.append(email)
    membership_id, added = add_test_membership(ctx, user_id=user_id)
    if added:
        result.memberships_added.append(email)
        record(  # en la transacción: sin su fila, no hay membresía
            ctx,
            "membership.activated",
            Entity("membership", membership_id),
            metadata={**trail, "via": "test_accounts"},
        )
    if grant_template_role(ctx, membership_id=membership_id, template=template):
        result.roles_added.append(email)


def _summary(result: Provisioned) -> dict[str, object]:
    return {
        "organization_created": result.organization_created,
        "accounts_created": len(result.accounts_created),
        "memberships_added": len(result.memberships_added),
        "roles_added": len(result.roles_added),
    }


@dataclass(frozen=True)
class Listed:
    email: str
    role: str


def listed() -> list[Listed]:
    """Las cuentas de prueba que se pueden usar ahora (ADR-026 §5): activas, con membresía activa
    en la organización de pruebas y con su rol de plantilla, en el orden de `PROFILES`. Vacía si
    la organización no existe, está suspendida o no es la de pruebas (su `owner@` no tiene el rol
    Owner). Solo
    correos y nombres de rol: nada que sirva para entrar."""
    slug = _slug()
    found = organization_by_slug(slug)
    if found is None or found.status not in ACCESSIBLE_STATUSES:  # suspendida: nadie entraría
        return []
    ctx = TenantContext(found.id, "http")  # actor SYSTEM; solo consultas
    shown: list[Listed] = []
    with tenant_scope(ctx):
        owner = active_account_id(email_for(slug, "owner"))
        if owner is None or not holds_owner_role(ctx, user_id=owner):
            return []
        for local, template in PROFILES:
            email = email_for(slug, local)
            user_id = active_account_id(email)
            membership_id = active_membership_id(ctx, user_id=user_id) if user_id else None
            role = (
                template_role_name(ctx, membership_id=membership_id, template=template)
                if membership_id
                else None
            )
            if role is not None:
                shown.append(Listed(email, role))
    return shown
