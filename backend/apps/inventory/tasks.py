"""Tareas de inventario (F3-34, E04-05): el vencimiento automático de las reservas.

`inventory.expire_reservations_sweep` (beat, cada 5 minutos) es de plataforma: lee las
organizaciones activas (`organizations` no tiene RLS) y encola `inventory.expire_reservations`
por cada una; bajo RLS no ve reservas. Esa tarea abre un `tenant_scope` corto por paso: uno para
listar las vencidas (ACTIVE con `expires_at <= now()`, por `variant_id, id`, en lotes) y **uno por
reserva**, que la vence con `reservations.expire` (el camino de liberar: variante → almacén →
saldo → reserva `FOR UPDATE`, releída). Así nunca hay más de una variante bloqueada y cada una
confirma sola. Si el `tenant_scope` de un `@tenant_task` envolviera todo el recorrido, sería una
sola transacción con todas las celdas bloqueadas hasta el final: por eso esta tarea es de
plataforma, como `conversations.dispatch_message`: el barredor saca el id de la organización de la
tabla y la tarea comprueba que sea una activa (el mensaje no basta). Una reserva espera hasta
`LOCK_TIMEOUT_MS` por cada uno de sus bloqueos (hasta cuatro), nunca más de la cuarta parte de lo
que le queda a la pasada: pasado `RUN_SECONDS` la pasada termina aunque queden filas. Una reserva
que no consigue un bloqueo deja fuera de la pasada el resto de su celda; otro error salta solo esa
fila. Las dos se reintentan en el siguiente barrido. Una pasada encadenada sigue tras el cursor de
la anterior, así que unas filas que fallan siempre al principio no tapan las de detrás (F3-34b).
El barrido rota el orden de las organizaciones en cada tick: con más cola que `QUEUE_EXPIRES`, las
últimas no se pierden siempre. Un fallo en una reserva o en una organización no para el resto.
Los registros llevan ids y contadores, nunca datos de personas.
"""

import time
from collections import Counter
from datetime import datetime
from math import gcd
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
LOCKS_PER_ROW = 4  # variante, almacén, saldo y reserva: cada uno espera su `lock_timeout`
LOCK_NOT_AVAILABLE = "55P03"  # `lock_timeout` agotado: la celda está ocupada
SWEEP_SECONDS = 300  # el intervalo de beat: un tick del barrido
Key = tuple[UUID, UUID]  # (variante, reserva): el orden y el cursor
Cell = tuple[UUID, UUID]  # (variante, almacén)


def _due(
    ctx: TenantContext, cutoff: datetime, after: Key | None, limit: int, failed: set[Cell]
) -> list[tuple[UUID, UUID, UUID]]:
    """(variante, almacén, reserva) de las vencidas a `cutoff`, por `variant_id, id`, después de
    `after` y fuera de las celdas `failed`."""
    with tenant_scope(ctx):
        rows = InventoryReservation.objects.filter(status="ACTIVE", expires_at__lte=cutoff)
        if after is not None:
            rows = rows.filter(Q(variant_id__gt=after[0]) | Q(variant_id=after[0], pk__gt=after[1]))
        for variant_id, warehouse_id in failed:  # a lo sumo una por intento de la pasada
            rows = rows.exclude(variant_id=variant_id, warehouse_id=warehouse_id)
        rows = rows.order_by("variant_id", "pk")
        return list(rows.values_list("variant_id", "warehouse_id", "pk")[:limit])


def _lock_wait_ms(started: float) -> int:
    """`LOCK_TIMEOUT_MS` o la parte de lo que queda de la pasada que cabe en los bloqueos de una
    fila, lo menor; al menos 1 ms (0 quitaría el límite)."""
    left = RUN_SECONDS - (time.monotonic() - started)
    return max(1, min(LOCK_TIMEOUT_MS, int(left * 1000 / LOCKS_PER_ROW)))


def expire_due(
    ctx: TenantContext, batch_size: int, max_per_run: int, after: Key | None = None
) -> tuple[Counter[str], Key | None, bool]:
    """Vence las reservas vencidas de la organización de `ctx` tras `after`, cada una en su
    `tenant_scope`. Cuenta `expired`, `skipped` (otro la cerró antes), `failed` y `capped`
    (quedan más tras el cursor, fuera de las celdas ocupadas). Devuelve también el cursor y si la
    pasada terminó por tiempo."""
    done: Counter[str] = Counter(expired=0, skipped=0, failed=0, capped=0)
    started, cutoff, taken = time.monotonic(), timezone.now(), 0
    failed: set[Cell] = set()
    while taken < max_per_run:
        batch = _due(ctx, cutoff, after, min(batch_size, max_per_run - taken), failed)
        if not batch:
            break
        for variant_id, warehouse_id, pk in batch:
            if (variant_id, warehouse_id) in failed:  # falló antes en este lote
                continue
            after, taken = (variant_id, pk), taken + 1
            try:
                with tenant_scope(ctx) as tenant:
                    with connection.cursor() as cursor:  # solo esta transacción
                        wait = f"{_lock_wait_ms(started)}ms"
                        cursor.execute("SELECT set_config('lock_timeout', %s, true)", [wait])
                    done["expired" if reservations.expire(tenant, pk) else "skipped"] += 1
            except Exception as error:  # noqa: BLE001 — una reserva no detiene el lote
                logger.error(
                    "inventory.reservation_expiry_failed",
                    organization_id=str(ctx.organization_id),
                    reservation_id=str(pk),
                    error=type(error).__name__,
                )
                done["failed"] += 1
                if getattr(error.__cause__, "sqlstate", None) == LOCK_NOT_AVAILABLE:
                    failed.add((variant_id, warehouse_id))  # ocupada: el resto, al siguiente
            if time.monotonic() - started > RUN_SECONDS:  # dentro del lote; la pasada siempre
                break  # hace una fila
        if time.monotonic() - started > RUN_SECONDS:
            break
    done["capped"] = int(bool(_due(ctx, cutoff, after, 1, failed)))
    return done, after, time.monotonic() - started > RUN_SECONDS


def _clamp(value: int, top: int) -> int:
    return max(1, min(int(value), top))


@platform_task(name="inventory.expire_reservations", ignore_result=True)
def expire_reservations(
    organization_id: str,
    batch_size: int = BATCH,
    max_per_run: int = MAX_PER_RUN,
    after: list[str] | None = None,
) -> dict[str, int]:
    """Vence las reservas vencidas de una organización (la pone el barredor). Si quedan más tras su
    cursor, encola la siguiente con ese cursor (`after`): cada ejecución es corta, un atraso grande
    no espera a los 5 minutos y unas filas que fallan siempre no tapan las de detrás. El cursor
    avanza en cada pasada, así que la cadena termina al llegar al final; el barredor empieza sin
    cursor. Una pasada que se quedó sin tiempo sin vencer ninguna no se encadena (todo espera: un
    bloqueo amplio); la recoge el siguiente barrido. El id llega en el mensaje: se comprueba que la
    organización esté activa y los topes se acotan (un mensaje no los levanta); el cursor solo
    elige por dónde seguir, bajo RLS."""
    org = UUID(str(organization_id))
    cursor = None if after is None else _key(after)
    if not Organization.objects.filter(pk=org, status__in=ACCESSIBLE_STATUSES).exists():
        logger.warning("inventory.expire_org_inactive", organization_id=str(org))
        return dict(Counter(expired=0, skipped=0, failed=0, capped=0))  # un mensaje no la activa
    batch_size, max_per_run = _clamp(batch_size, BATCH), _clamp(max_per_run, MAX_PER_RUN)
    ctx = TenantContext(org, "celery")
    done, cursor, timed_out = expire_due(ctx, batch_size, max_per_run, cursor)
    if done["capped"] and cursor is not None and (done["expired"] or not timed_out):
        try:
            kwargs = {"organization_id": str(ctx.organization_id), "batch_size": batch_size}
            expire_reservations.apply_async(
                kwargs={**kwargs, "max_per_run": max_per_run, "after": [str(k) for k in cursor]},
                expires=QUEUE_EXPIRES,
            )
        except Exception as error:  # noqa: BLE001 — el barredor lo repite en 5 minutos
            logger.error(
                "inventory.expire_chain_failed",
                organization_id=str(ctx.organization_id),
                error=type(error).__name__,
            )
    logger.info("inventory.reservations_expired", organization_id=str(ctx.organization_id), **done)
    return dict(done)


def _key(after: list[str]) -> Key:
    variant_id, pk = after  # dos UUID: si no, `ValueError` sin tocar nada
    return UUID(str(variant_id)), UUID(str(pk))


def _start(tick: int, count: int) -> int:
    """Dónde empieza el barrido `tick` entre `count` organizaciones: avanza un paso cercano a
    `count`/φ y primo con `count`. Límite: en `count` barridos seguidos cada una va primera una vez.
    El paso grande reparte a las que llegan antes de vencer: con k servidas por barrido suele
    bastar un múltiplo pequeño de count/k (no es una garantía)."""
    if count < 2:
        return 0
    step = next(s for s in range(round(count * 0.618), count) if gcd(s, count) == 1)
    return tick * step % count


@platform_task(name="inventory.expire_reservations_sweep", ignore_result=True)
def expire_reservations_sweep() -> dict[str, int]:
    """Encola `inventory.expire_reservations` por cada organización activa (ACTIVE o TRIAL), en un
    orden que rota con el tick (`_start`). Un fallo al encolar una no detiene las demás. Solo lee
    `organizations`: ve ids, no reservas."""
    found = Organization.objects.filter(status__in=ACCESSIBLE_STATUSES).order_by("pk")
    ids = list(found.values_list("pk", flat=True))
    start = _start(int(timezone.now().timestamp()) // SWEEP_SECONDS, len(ids))
    done = {"organizations": 0, "enqueued": 0, "failed": 0}
    for organization_id in ids[start:] + ids[:start]:  # con cola atrasada, no siempre las mismas
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
