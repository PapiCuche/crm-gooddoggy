"""La política de segundo factor por membresía (ADR-023 §9, ADR-038): la sirve `members`, que
une `access.second_factor` (qué membresía lo exige) y `accounts.selectors` (quién tiene un
dispositivo confirmado), dos módulos de la misma capa que no se importan entre sí. El kernel la
llama por `TENANCY_SECOND_FACTOR_POLICY` (`core.tenancy.second_factor`), dentro del
`tenant_scope`.
"""

from apps.access import second_factor as access
from apps.accounts.selectors import has_second_factor
from core.tenancy.context import TenantContext
from core.tenancy.second_factor import SecondFactor


def membership_second_factor(ctx: TenantContext) -> SecondFactor:
    required = access.required(ctx)  # sin usuario en `ctx`, `AccessDenied`: no entra
    assert ctx.user_id is not None  # noqa: S101 — `access.required` ya lo comprobó
    return SecondFactor(required=required, enrolled=has_second_factor(ctx.user_id))
