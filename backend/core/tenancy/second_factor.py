"""Segundo factor por membresía al entrar a una organización (ADR-023 §9, ADR-038).

El kernel no sabe qué membresía lo exige ni quién tiene un dispositivo confirmado: lo responde
la política que nombra `TENANCY_SECOND_FACTOR_POLICY`, servida por un módulo de capa superior
(como `TENANCY_MEMBERSHIP_RESOLVER`). Aquí solo se decide qué hacer con la respuesta según
`MFA_ENFORCEMENT`: con `enforce`, `admit` niega la entrada a quien la necesita y no la tiene;
con `warn`, no mira nada. Solo añade una exigencia: nunca abre lo que la membresía no abre.
"""

from dataclasses import dataclass

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.utils.module_loading import import_string

from core.tenancy.context import TenantContext
from core.tenancy.scope import require_scope, tenant_scope

WARN, ENFORCE = "warn", "enforce"
MODES = (WARN, ENFORCE)
MFA_REQUIRED = "MFA_REQUIRED"  # el código de la respuesta: la pantalla lleva al alta
MESSAGE = (
    "Para entrar a esta organización tu cuenta necesita el segundo factor. Actívalo en la "
    "seguridad de tu cuenta."
)


@dataclass(frozen=True, slots=True)
class SecondFactor:
    required: bool  # la membresía lo exige (ADR-023 §9)
    enrolled: bool  # la cuenta tiene un dispositivo confirmado

    @property
    def missing(self) -> bool:
        return self.required and not self.enrolled


class SecondFactorRequired(Exception):
    """Miembro de una organización que exige segundo factor, sin dispositivo confirmado."""


def enforcing() -> bool:
    """¿Se aplica la política? Un valor que no es `warn` ni `enforce` falla (nunca es `warn`)."""
    mode = settings.MFA_ENFORCEMENT
    if mode not in MODES:
        raise ImproperlyConfigured("MFA_ENFORCEMENT debe ser warn o enforce")
    return mode == ENFORCE


def evaluate(ctx: TenantContext) -> SecondFactor:
    """Lo que dice la política de la membresía de `ctx`, dentro de su `tenant_scope` activo."""
    require_scope(ctx)
    found = import_string(settings.TENANCY_SECOND_FACTOR_POLICY)(ctx)
    if type(found) is not SecondFactor or not all(  # una política que no responde no deja pasar
        type(value) is bool for value in (found.required, found.enrolled)
    ):
        raise TypeError("TENANCY_SECOND_FACTOR_POLICY no devolvió un SecondFactor")
    return found


def admit(ctx: TenantContext) -> None:
    """Con `enforce`, `SecondFactorRequired` si la membresía lo exige y la cuenta no lo tiene.
    Lee en su propio `tenant_scope` (los roles son de tenant, con RLS). Con `warn`, nada."""
    if not enforcing():
        return
    with tenant_scope(ctx):
        missing = evaluate(ctx).missing
    if missing:
        raise SecondFactorRequired
