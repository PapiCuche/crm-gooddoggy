"""Los topes mensual y diario de IA de la organización (F8-05b3; ADR-034 §5): leerlos y fijarlos.
Cada comando comprueba `ai.manage` antes de leer nada; el segundo factor reciente y el límite de
tasa los ponen las rutas, como en las de la clave. La organización es la del contexto: la fila se
inserta y se cambia como `crm_app` bajo RLS, sin función SECURITY DEFINER (0005). Bajar un tope por
debajo de lo gastado o reservado vale: la siguiente reserva no cabe (corte duro) y las reservas
abiertas se ajustan igual. La auditoría (`ai.limits.changed`) lleva solo el antes y el después de
los dos topes, con seis decimales; ningún registro ni error lleva importes."""

from decimal import Decimal

from django.db import connections, transaction

from apps.ai_gateway.accounts import _allowed
from apps.ai_gateway.models import AIBudget
from apps.audit.services import Entity, record
from core.tenancy.context import TenantContext
from core.tenancy.scope import require_scope

PLACES, MAX = Decimal("0.000001"), Decimal("1000000")  # numeric(12, 6): menos de un millón
_CREATE = (  # la fila nueva; si ya existe (o otra petición la crea a la vez), nada
    "INSERT INTO public.ai_budgets (organization_id, monthly_usd, daily_usd) VALUES (%s, %s, %s)"
    " ON CONFLICT (organization_id) DO NOTHING RETURNING id"
)


class InvalidCap(ValueError):
    """Un tope no válido; lleva solo el nombre del campo, nunca el importe."""

    def __init__(self, field: str) -> None:
        super().__init__(field)
        self.field = field


def usd(value: Decimal | None) -> str | None:
    return None if value is None else format(value, ".6f")


def _check(field: str, value: Decimal) -> None:
    """Finito, mayor que cero, por debajo de un millón y con seis decimales como mucho: la base
    redondearía el séptimo en silencio."""
    if not (value.is_finite() and 0 < value < MAX and value == value.quantize(PLACES)):
        raise InvalidCap(field)


def read(ctx: TenantContext) -> AIBudget | None:
    """Los topes de la organización, o `None` si no tiene. `AccessDenied` sin `ai.manage`."""
    alias = require_scope(ctx)
    _allowed(ctx)
    found: AIBudget | None = AIBudget.objects.using(alias).first()
    return found


def set_caps(ctx: TenantContext, *, monthly_usd: Decimal, daily_usd: Decimal) -> AIBudget:
    """Fija los dos topes y lo audita. `InvalidCap` (el diario por encima del mensual cuenta como
    del diario), `AccessDenied`."""
    alias = require_scope(ctx)
    _allowed(ctx)
    _check("monthly_usd", monthly_usd)
    _check("daily_usd", daily_usd)
    if daily_usd > monthly_usd:
        raise InvalidCap("daily_usd")
    budgets = AIBudget.objects.using(alias)
    with transaction.atomic(using=alias):
        with connections[alias].cursor() as cursor:
            cursor.execute(_CREATE, [ctx.organization_id, monthly_usd, daily_usd])
            created = cursor.fetchone() is not None
        before: tuple[Decimal | None, Decimal | None] = (None, None)
        if not created:
            locked = budgets.select_for_update().get()
            before = (locked.monthly_usd, locked.daily_usd)
            budgets.filter(pk=locked.pk).update(monthly_usd=monthly_usd, daily_usd=daily_usd)
        budget: AIBudget = budgets.get()
        changes = {
            "monthly_usd": [usd(before[0]), usd(monthly_usd)],
            "daily_usd": [usd(before[1]), usd(daily_usd)],
        }
        record(ctx, "ai.limits.changed", Entity("ai_budget", budget.pk), changes)
    return budget
