"""El barrido de reservas huérfanas de IA (F8-05b5, #962; ADR-034 §5, ADR-044). Una reserva sin
ajustar de más de `ORPHAN_AFTER` (proceso caído o excepción inesperada) se cierra como **gastada**
con `ai_budget_settle(…, 'ORPHANED', NULL, …)`: cuenta la reserva entera y nunca la libera. El
presupuesto se equivoca hacia arriba.

Descubre las organizaciones con una función SECURITY DEFINER de solo lectura que devuelve solo ids
(`ai_budget_orphan_organizations`) y trabaja en el scope de cada una, como `crm_app` bajo RLS: la
organización sale de la base, nunca de un payload. La base vuelve a comprobar el plazo y que la
reserva siga abierta: dos barridos a la vez (o un ajuste tardío) no la cierran dos veces. Los logs
propios del barrido llevan solo cuentas y tipos de error; cada cierre pasa además por
`gateway.settle`, que registra `ai.llm_call` con el id de la reserva y el resultado (como cualquier
ajuste), nunca importes.

`crm_app` no crea particiones (sin EXECUTE sobre `ai_llm_calls_ensure_partitions`): el horizonte lo
mantiene el `post_migrate` de `apps.py`. El barrido solo avisa si falta la partición de dentro de
dos meses, antes de que `ai_budget_reserve` empiece a negarse."""

import datetime as dt
from collections import Counter
from uuid import UUID

from django.db import connection
from django.db.models.functions import Now

from apps.ai_gateway import gateway
from apps.ai_gateway.llm import Outcome
from apps.ai_gateway.models import AIBudgetReservation
from core.observability.logging import get_logger
from core.tenancy.context import TenantContext
from core.tenancy.scope import require_scope, tenant_scope

logger = get_logger(__name__)
ORPHAN_AFTER = dt.timedelta(minutes=15)  # el plazo de la base (0007); una llamada dura ~1 min
DISCOVER = "SELECT public.ai_budget_orphan_organizations(%s)"
HORIZON = (  # la partición (mes UTC) de dentro de dos meses
    "SELECT to_regclass('public.ai_llm_calls_y' || to_char(now() AT TIME ZONE 'UTC'"
    " + interval '2 months', 'YYYY\"m\"MM')) IS NOT NULL"
)


def _open(ctx: TenantContext, batch_size: int) -> list[UUID]:
    """Las reservas huérfanas de la organización de `ctx`, las más viejas primero."""
    with tenant_scope(ctx):
        rows = AIBudgetReservation.objects.using(require_scope(ctx)).filter(
            settled_at__isnull=True, created_at__lte=Now() - ORPHAN_AFTER
        )
        return list(rows.order_by("created_at").values_list("pk", flat=True)[:batch_size])


def close(*, max_orgs: int = 50, batch_size: int = 100) -> dict[str, int]:
    """Cierra como gastadas las reservas huérfanas. Devuelve cuántas cerró (`closed`), cuántas ya
    estaban cerradas o eran aún jóvenes para la base (`skipped`) y cuántas fallaron (`failed`). Un
    fallo no detiene a las demás."""
    done: Counter[str] = Counter(closed=0, skipped=0, failed=0)
    with connection.cursor() as cursor:
        cursor.execute(DISCOVER, [max_orgs])
        organizations = [UUID(str(row[0])) for row in cursor.fetchall()]
    for organization_id in organizations:
        ctx = TenantContext(organization_id, "celery")
        try:
            held = _open(ctx, batch_size)
        except Exception as error:  # noqa: BLE001 — una organización no detiene a las demás
            logger.error("ai.orphan_close_failed", error=type(error).__name__)
            done["failed"] += 1
            continue
        for reservation_id in held:
            try:
                closed = gateway.settle(ctx, reservation_id, Outcome.ORPHANED, None)
            except Exception as error:  # noqa: BLE001
                logger.error("ai.orphan_close_failed", error=type(error).__name__)
                done["failed"] += 1
                continue
            done["closed" if closed else "skipped"] += 1
    with connection.cursor() as cursor:
        cursor.execute(HORIZON)
        if not cursor.fetchone()[0]:
            logger.error("ai.llm_calls_horizon_short")
    if any(done.values()):
        logger.info("ai.orphans_closed", **done)
    return dict(done)
