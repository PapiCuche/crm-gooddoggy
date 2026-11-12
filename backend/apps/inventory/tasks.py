"""Tareas de inventario (F3-34, E04-05): el vencimiento automático de las reservas.

`inventory.expire_reservations_sweep` (beat, cada 5 minutos) es de plataforma: lee las
organizaciones activas (`organizations` no tiene RLS) y encola `inventory.expire_reservations`
por cada una; bajo RLS no ve reservas. Esa tarea abre un `tenant_scope` corto por paso: uno para
listar las vencidas (ACTIVE con `expires_at <= now()`, por `variant_id, id`, en lotes) y **uno por
reserva**, que la vence con `reservations.expire` (el camino de liberar: variante → almacén →
saldo → reserva `FOR UPDATE`, releída). Así nunca hay más de una variante bloqueada y cada una
confirma sola. Si el `tenant_scope` de un `@tenant_task` envolviera todo el recorrido, sería una
sola transacción con todas las celdas bloqueadas hasta el final: por eso esta tarea es de
plataforma, como `conversations.dispatch_message`, y el id de la organización lo pone el barredor
(sale de la tabla, no de un payload). Una reserva espera una celda ocupada `LOCK_TIMEOUT_MS`;
si no, falla y se reintenta en el siguiente barrido. Un fallo en una reserva o en una organización
no para el resto. Los registros llevan ids y contadores, nunca datos de personas.
"""

import time
from collections import Counter
from datetime import datetime
from uuid import UUID

from django.db import connection
from django.db.models import Q
from django.utils import timezone

from apps.inventory import reservations
from apps.inventory.models import InventoryReservation
from apps.organizations.models import Organization
from core.observability.logging import get_logger
from core.tenancy.celery import platform_task
from core.tenancy.context import TenantContext
from core.tenancy.resolution import ACCESSIBLE_STATUSES
from core.tenancy.scope import tenant_scope

logger = get_logger(__name__)
BATCH = 100  # vencidas que se listan de una vez
MAX_PER_RUN = 500  # tope por organización y ejecución: el resto, en la siguiente (se encadena)
RUN_SECONDS = 120.0  # lo máximo que una ejecución ocupa un worker
QUEUE_EXPIRES = 300  # segundos: sin workers no se acumulan barridos viejos
LOCK_TIMEOUT_MS = 10_000  # lo que una reserva espera una celda ocupada: luego falla y se reintenta


def _due(
    ctx: TenantContext, cutoff: datetime, after: tuple[UUID, UUID] | None, limit: int
) -> list[tuple[UUID, UUID]]:
    """(variante, reserva) de las vencidas a `cutoff`, por `variant_id, id`, después de `after`."""
    with tenant_scope(ctx):
        rows = InventoryReservation.objects.filter(status="ACTIVE", expires_at__lte=cutoff)
        if after is not None:
            rows = rows.filter(Q(variant_id__gt=after[0]) | Q(variant_id=after[0], pk__gt=after[1]))
        return list(rows.order_by("variant_id", "pk").values_list("variant_id", "pk")[:limit])


def expire_due(ctx: TenantContext, batch_size: int, max_per_run: int) -> Counter[str]:
    """Vence las reservas vencidas de la organización de `ctx`, cada una en su `tenant_scope`.
    Cuenta `expired`, `skipped` (otro la cerró antes), `failed` y `capped` (quedaron más)."""
    done: Counter[str] = Counter(expired=0, skipped=0, failed=0, capped=0)
    started, cutoff, after, taken = time.monotonic(), timezone.now(), None, 0
    while taken < max_per_run:
        batch = _due(ctx, cutoff, after, min(batch_size, max_per_run - taken))
        if not batch:
            break
        for variant_id, pk in batch:
            after, taken = (variant_id, pk), taken + 1
            try:
                with tenant_scope(ctx) as tenant:
                    with connection.cursor() as cursor:  # solo esta transacción
                        cursor.execute(
                            "SELECT set_config('lock_timeout', %s, true)", [f"{LOCK_TIMEOUT_MS}ms"]
                        )
                    done["expired" if reservations.expire(tenant, pk) else "skipped"] += 1
            except Exception as error:  # noqa: BLE001 — una reserva no detiene el lote
                logger.error(
                    "inventory.reservation_expiry_failed",
                    organization_id=str(ctx.organization_id),
                    reservation_id=str(pk),
                    error=type(error).__name__,
                )
                done["failed"] += 1
        if time.monotonic() - started > RUN_SECONDS:
            break
    done["capped"] = int(bool(_due(ctx, cutoff, after, 1)))
    return done


@platform_task(name="inventory.expire_reservations", ignore_result=True)
def expire_reservations(
    organization_id: str, batch_size: int = BATCH, max_per_run: int = MAX_PER_RUN
) -> dict[str, int]:
    """Vence las reservas vencidas de una organización (la pone el barredor). Si quedan más y esta
    pasada venció alguna, encola la siguiente: cada ejecución es corta y un atraso grande no espera
    a los 5 minutos. Sin progreso (todo falló) no se encadena."""
    ctx = TenantContext(UUID(str(organization_id)), "celery")
    done = expire_due(ctx, batch_size, max_per_run)
    if done["capped"] and done["expired"]:
        try:
            kwargs = {"organization_id": str(ctx.organization_id), "batch_size": batch_size}
            expire_reservations.apply_async(
                kwargs={**kwargs, "max_per_run": max_per_run}, expires=QUEUE_EXPIRES
            )
        except Exception as error:  # noqa: BLE001 — el barredor lo repite en 5 minutos
            logger.error(
                "inventory.expire_chain_failed",
                organization_id=str(ctx.organization_id),
                error=type(error).__name__,
            )
    logger.info("inventory.reservations_expired", organization_id=str(ctx.organization_id), **done)
    return dict(done)


@platform_task(name="inventory.expire_reservations_sweep", ignore_result=True)
def expire_reservations_sweep() -> dict[str, int]:
    """Encola `inventory.expire_reservations` por cada organización activa (ACTIVE o TRIAL). Un
    fallo al encolar una no detiene las demás. Solo lee `organizations`: ve ids, no reservas."""
    found = Organization.objects.filter(status__in=ACCESSIBLE_STATUSES).order_by("pk")
    done = {"organizations": 0, "enqueued": 0, "failed": 0}
    for organization_id in list(found.values_list("pk", flat=True)):
        done["organizations"] += 1
        try:
            expire_reservations.apply_async(
                kwargs={"organization_id": str(organization_id)}, expires=QUEUE_EXPIRES
            )
            done["enqueued"] += 1
        except Exception as error:  # noqa: BLE001
            logger.error(
                "inventory.expire_enqueue_failed",
                organization_id=str(organization_id),
                error=type(error).__name__,
            )
            done["failed"] += 1
    logger.info("inventory.reservations_expiry_swept", **done)
    return done
