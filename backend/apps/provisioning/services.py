"""Aprovisionamiento (F2-06, E01-04): alta de una organización utilizable.

Orquesta `accounts`, `organizations` y `access`, que no se importan entre sí, a través de la
frontera de alta de cada uno (`bootstrap.py`). Es una operación de plataforma: la lanza un
operador (comando `bootstrap_organization`), no un usuario del CRM.

Todo lo que crea (usuario, organización, membresía, roles y su auditoría) va en una sola
transacción, la del `tenant_scope` de la organización nueva: si algo falla no queda nada. La
auditoría de plataforma (ADR-013 §5) lleva una fila de intención antes y una de resultado después.
"""

import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from django.core.exceptions import ValidationError
from django.db import connection

from apps.access.bootstrap import install_initial_owner
from apps.accounts.bootstrap import owner_account
from apps.accounts.emails import canonical_email
from apps.audit import platform
from apps.audit.services import Entity, Result, record
from apps.organizations.bootstrap import clean, create_organization, organization_exists
from apps.provisioning.seeds import run_seeds
from core.ids import new_id
from core.tenancy.context import TenantContext, TenantContextError
from core.tenancy.scope import tenant_scope

logger = logging.getLogger(__name__)
# En caracteres. Con estos topes la traza cabe en los metadatos de la auditoría de plataforma
# incluso en su fila más larga y con texto fuera del plano básico (12 bytes por carácter).
REASON_MAX, OPERATOR_MAX = 200, 64
STARTED, DONE = "organization.bootstrap.started", "organization.bootstrapped"


@dataclass(frozen=True, slots=True)
class Bootstrapped:
    organization_id: UUID
    slug: str
    owner_user_id: UUID
    membership_id: UUID
    user_created: bool


def checked_trail(operator: str, reason: str) -> tuple[str, str]:
    """El operador y el motivo de una operación de plataforma, limpios y dentro de sus topes."""
    reason, operator = reason.strip(), operator.strip()
    if not reason or not operator:
        raise ValidationError("El motivo y el operador son obligatorios", code="required")
    if len(reason) > REASON_MAX or len(operator) > OPERATOR_MAX:
        limits = f"motivo, {REASON_MAX} caracteres; operador, {OPERATOR_MAX}"
        raise ValidationError(f"Texto demasiado largo ({limits})", code="too_long")
    if not (reason + operator).isprintable():  # saltos de línea, nulos, sustitutos sueltos
        raise ValidationError("El motivo y el operador van en una línea de texto", code="invalid")
    return operator, reason


def bootstrap_organization(
    *,
    slug: str,
    name: str,
    owner_email: str,
    owner_password: str | None = None,
    operator: str,
    reason: str,
) -> Bootstrapped:
    """Crea la organización con su Owner inicial y sus roles plantilla.

    Con `owner_password` el Owner es una cuenta nueva; sin ella, una que ya existe y conserva la
    suya. La contraseña nunca se registra ni se devuelve.
    """
    operator, reason = checked_trail(operator, reason)
    name, email = clean(slug, name), canonical_email(owner_email)
    if connection.in_atomic_block:  # antes de la intención: no quedaría en una transacción ajena
        raise TenantContextError("El alta abre su propia transacción: no admite una exterior")
    organization_id = new_id()
    entity = Entity("organization", organization_id)
    trail = {"operator": operator, "reason": reason, "slug": slug}

    def audit(action: str, extra: dict[str, Any], result: Result = Result.SUCCESS) -> None:
        platform.record(
            action,
            actor_type=platform.Actor.SYSTEM,
            entity=entity,
            metadata={**trail, **extra},
            result=result,
        )

    audit(STARTED, {})  # intención: si no se puede auditar, el alta no empieza
    ctx = TenantContext(organization_id, "command")  # actor SYSTEM, sin usuario
    done: dict[str, Any] | None = None
    late: dict[str, Any] = {}
    try:
        with tenant_scope(ctx):
            user_id, created = owner_account(email, owner_password)
            membership = create_organization(ctx, slug=slug, name=name, owner_user_id=user_id)
            install_initial_owner(ctx, membership_id=membership.pk)
            run_seeds(ctx)  # lo que cada módulo de negocio necesita desde el primer día
            done = {"owner_user_id": user_id, "user_created": created}
            record(
                ctx,
                "organization.created",
                Entity("organization", organization_id, slug),
                metadata={**trail, **done},
            )
    except Exception as error:
        # Un error al cerrar la transacción puede llegar con el COMMIT ya hecho. Si el cuerpo
        # terminó, el resultado se decide por lo que quedó en la base, no por la excepción.
        kind = type(error).__name__
        try:
            committed = done is not None and organization_exists(organization_id)
            if not committed:
                audit(DONE, {"error": kind}, Result.FAILED)
        except Exception:  # sin fila de resultado (ADR-013 §5): manda el error original
            logger.exception("alta de organización: no se pudo registrar el resultado")
            raise error from None
        if done is None or not committed:
            raise
        logger.warning("alta de organización confirmada pese a un error al cerrar", exc_info=True)
        late = {"commit_error": kind}
    audit(DONE, {**done, **late})
    return Bootstrapped(organization_id, slug, user_id, membership.pk, created)
