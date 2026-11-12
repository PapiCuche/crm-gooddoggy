"""Las políticas de descuento de las plantillas de rol (F5-09b4; 03 §H): Administrador sin límite
(el Owner no lleva política: no tiene límite), Supervisor 10 % y Vendedor 3 %, a confirmar con el
dueño. Las organizaciones nuevas las reciben del alta (`apps.provisioning.seeds`). Las que ya
existían no (a diferencia de `deals`, sin migración de datos): un límite nuevo sería abrir
descuentos sin que nadie lo decida; lo fija quien tiene `discount_policies.manage`. Idempotente y
sin permisos: no toca al rol que ya tiene alguna política. No audita: son los valores iniciales,
no un cambio de nadie (la auditoría de `organization.created` no los lista)."""

from decimal import Decimal

from apps.access.models import Role
from apps.quotes.models import DiscountPolicy
from core.tenancy.context import TenantContext
from core.tenancy.scope import require_scope

LIMITS = {"admin": Decimal("100.00"), "supervisor": Decimal("10.00"), "seller": Decimal("3.00")}


def ensure_template_policies(ctx: TenantContext) -> int:
    """Una política «todas las listas» para cada rol plantilla de la organización del contexto que
    no tenga ninguna. Devuelve cuántas creó."""
    alias = require_scope(ctx)
    policies = DiscountPolicy.objects.using(alias)
    held = set(policies.values_list("role_id", flat=True))
    roles = Role.objects.using(alias).filter(is_system=True, code__in=LIMITS)
    made = policies.bulk_create(
        DiscountPolicy(
            organization_id=ctx.organization_id, role_id=r.pk, max_discount_percent=LIMITS[r.code]
        )
        for r in roles
        if r.pk not in held
    )
    return len(made)
