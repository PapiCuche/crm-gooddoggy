"""Alta de un miembro por un operador (F2-127, D-F2-14 opción B): `manage.py add_member`.

Orquesta las fronteras de alta de `accounts`, `organizations` y `access`, como el alta de una
organización. Es una operación de plataforma que entra en un tenant (ADR-013 §5): una fila de
intención antes y una de resultado después en la auditoría de plataforma; la cuenta, la membresía,
su rol y `membership.created` van en una sola transacción, la del `tenant_scope` de la
organización del slug. Idempotente: si la cuenta ya es miembro no cambia nada.
"""

import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from django.db import connection

from apps.access.bootstrap import grant_template_role
from apps.access.catalog import ROLE_TEMPLATES
from apps.accounts.bootstrap import account_exists, member_account
from apps.accounts.emails import canonical_email
from apps.audit import platform
from apps.audit.services import Entity, Result, record
from apps.organizations.bootstrap import active_membership_id, add_operator_membership
from apps.provisioning.services import checked_trail
from core.tenancy.context import TenantContext, TenantContextError
from core.tenancy.resolution import organization_by_slug
from core.tenancy.scope import tenant_scope

logger = logging.getLogger(__name__)
STARTED, DONE = "membership.add.started", "membership.added"
# Los roles de plantilla del catálogo, menos el Owner (por su flag, nunca por el código).
OWNER_ROLES = frozenset(t.code for t in ROLE_TEMPLATES if t.is_owner_role)
ROLES = frozenset(t.code for t in ROLE_TEMPLATES if not t.is_owner_role)


class OwnerRole(Exception):
    """El rol Owner no se da con este comando: solo lo tiene el Owner inicial del alta."""


class UnknownRole(Exception):
    """El código no es el de un rol de plantilla del catálogo."""


class UnknownOrganization(Exception):
    """No hay ninguna organización con ese slug."""


@dataclass(frozen=True, slots=True)
class Added:
    organization_id: UUID
    membership_id: UUID
    user_id: UUID
    user_created: bool
    added: bool  # `False`: ya era miembro y no se cambió nada


def check_role(role: str) -> None:
    if role in OWNER_ROLES:
        raise OwnerRole
    if role not in ROLES:
        raise UnknownRole


def needs_password(email: str) -> bool:
    """¿Hará falta una contraseña? Solo si la cuenta no existe. `email` ya es canónico."""
    return not account_exists(email)


def _stored(ctx: TenantContext, user_id: UUID, membership_id: UUID) -> bool:
    """¿Quedó confirmada la membresía? En una transacción nueva del mismo tenant."""
    with tenant_scope(ctx):
        return active_membership_id(ctx, user_id=user_id) == membership_id


def add_member(
    *, slug: str, email: str, role: str, password: str | None, operator: str, reason: str
) -> Added:
    """Añade la cuenta de `email` a la organización `slug` con el rol de plantilla `role`.

    Si la cuenta no existe se crea con `password`, que no abre sesión hasta que la cuenta elija la
    suya. Si ya es miembro, con el estado que sea, no cambia nada. La contraseña nunca se registra
    ni se devuelve.
    """
    operator, reason = checked_trail(operator, reason)
    email = canonical_email(email)
    check_role(role)
    found = organization_by_slug(slug)  # el slug del operador: la única entrada del tenant
    if found is None:
        raise UnknownOrganization(slug)
    if connection.in_atomic_block:  # antes de la intención: no quedaría en una transacción ajena
        raise TenantContextError("El alta de un miembro abre su propia transacción")
    entity = Entity("organization", found.id)
    trail = {"operator": operator, "reason": reason, "slug": slug, "role": role}

    def audit(extra: dict[str, Any], result: Result = Result.SUCCESS) -> None:
        platform.record(
            DONE,
            actor_type=platform.Actor.SYSTEM,
            entity=entity,
            metadata={**trail, **extra},
            result=result,
        )

    platform.record(STARTED, actor_type=platform.Actor.SYSTEM, entity=entity, metadata=trail)
    ctx = TenantContext(found.id, "command")  # actor SYSTEM, sin usuario
    done: dict[str, Any] | None = None
    late: dict[str, Any] = {}
    try:
        with tenant_scope(ctx):
            user_id, created = member_account(email, password)
            membership_id, added = add_operator_membership(ctx, user_id=user_id)
            if added:
                record(  # en la transacción: sin su fila, no hay membresía
                    ctx,
                    "membership.created",
                    Entity("membership", membership_id),
                    metadata={**trail, "user_id": user_id, "user_created": created},
                )
                grant_template_role(ctx, membership_id=membership_id, template=role)
            done = {"user_id": user_id, "membership_id": membership_id, "user_created": created}
    except Exception as error:
        # Como el alta de una organización (OBS-F2-06-5): un error al cerrar la transacción puede
        # llegar con el COMMIT hecho. Si el cuerpo terminó, decide lo que quedó en la base.
        kind = type(error).__name__
        try:
            committed = done is not None and (not added or _stored(ctx, user_id, membership_id))
            if not committed:
                audit({"error": kind}, Result.FAILED)
        except Exception:  # sin fila de resultado (ADR-013 §5): manda el error original
            logger.exception("alta de un miembro: no se pudo registrar el resultado")
            raise error from None
        if done is None or not committed:
            raise
        logger.warning("alta de un miembro confirmada pese a un error al cerrar", exc_info=True)
        late = {"commit_error": kind}
    audit({**done, "added": added, **late})
    return Added(found.id, membership_id, user_id, created, added)
