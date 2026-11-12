"""Reservas de stock (F3-33, E04-05 y E04-08): apartar unidades de una celda variante × almacén
sin venderlas dos veces. Reservar sube `reserved` con un movimiento `RESERVE`; liberar (o vencer)
lo baja con un `RELEASE`. `reserved` es siempre la suma de las reservas ACTIVE de la celda y nunca
pasa de `on_hand` (CHECK `inventory_levels_stock_ck`).

No comprueba permisos: los declarará su API (F3-35), la única que podrá importarlo (contrato de
import-linter). Concurrencia: el mismo orden de bloqueo que `stock.move` (`stock.lock_cell`): la
variante `FOR NO KEY UPDATE`, el almacén `FOR KEY SHARE`, el saldo `FOR UPDATE` y, después, las
reservas de la celda `FOR UPDATE`, releyendo su estado. Dos reservas de la última unidad van de una
en una y la segunda ve lo que dejó la primera. Una operación con varias variantes (Fase 4) las
bloqueará por `id` ascendente, como la importación y el catálogo.

Claves: la del cliente va en la reserva (única por organización); sus movimientos llevan las
derivadas `R/<reserva>/reserve` y `R/<reserva>/release`, con una `/` que las claves de cliente no
admiten: el índice único del ledger impide que una reserva se libere (o venza) dos veces.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.audit.services import Entity, record
from apps.catalog.models import ProductVariant
from apps.inventory.models import InventoryLevel, InventoryMovement, InventoryReservation
from apps.inventory.stock import (
    QUANTITY_MAX,
    InsufficientStock,
    KeyReused,
    WarehouseInactive,
    idempotency_key,
    lock_cell,
    reason,
)
from core.tenancy.context import ActorType, TenantContext
from core.tenancy.scope import require_scope

Status, Type = InventoryReservation.Status, InventoryMovement.Type
EXPIRES_MAX = timedelta(days=30)  # a confirmar por el mantenedor (issue #342)
EXPIRED = "Reserva vencida"
KEY_TAKEN = "inventory_res_key_uq"
_SYSTEM: tuple[str, UUID | None] = (ActorType.SYSTEM, None)


class VariantInactive(Exception):
    """La variante no está ACTIVE: no se reserva."""


class ReservationNotActive(Exception):
    """La reserva venció o se consumió: ya no se libera."""

    def __init__(self, status: str) -> None:
        super().__init__(status)
        self.status = status


@dataclass(frozen=True, slots=True)
class Wanted:
    """La reserva que se pide: `quantity` unidades de la celda hasta `expires_at`."""

    variant_id: UUID
    warehouse_id: UUID
    quantity: int
    expires_at: datetime
    reason: str
    idempotency_key: str


def _now() -> datetime:
    return timezone.now()


def _check(wanted: Wanted) -> None:
    quantity = wanted.quantity
    if isinstance(quantity, bool) or not isinstance(quantity, int):
        raise ValueError("quantity: un entero")
    if not 1 <= quantity <= QUANTITY_MAX:
        raise ValueError(f"quantity: un entero entre 1 y {QUANTITY_MAX}")
    reason(wanted.reason)
    idempotency_key(wanted.idempotency_key)
    expires = wanted.expires_at
    if expires.tzinfo is None:
        raise ValueError("expires_at: con zona horaria")
    now = _now()
    if not now < expires <= now + EXPIRES_MAX:
        raise ValueError(f"expires_at: en el futuro y dentro de {EXPIRES_MAX.days} días")


def _actor(ctx: TenantContext) -> tuple[str, UUID | None]:
    return ctx.actor_type, ctx.user_id if ctx.actor_type == ActorType.USER else None


def _same(found: InventoryReservation, wanted: Wanted) -> bool:
    """Si la reserva guardada con la clave es la que se pide ahora (la misma petición)."""
    return (
        found.variant_id == wanted.variant_id
        and found.warehouse_id == wanted.warehouse_id
        and found.quantity == wanted.quantity
        and found.expires_at == wanted.expires_at
        and found.reason == reason(wanted.reason)
    )


def _replay(alias: str, wanted: Wanted) -> InventoryReservation | None:
    rows = InventoryReservation.objects.using(alias).filter(idempotency_key=wanted.idempotency_key)
    found: InventoryReservation | None = rows.first()
    if found is not None and not _same(found, wanted):
        raise KeyReused(wanted.idempotency_key)
    return found


def _movement(
    alias: str,
    reservation: InventoryReservation,
    level: InventoryLevel,
    delta: int,
    text: str,
    actor: tuple[str, UUID | None],
) -> None:
    """Cambia `reserved` de la celda bloqueada y deja su línea en el ledger (`RESERVE` o
    `RELEASE`, con la clave derivada de la reserva)."""
    level.reserved += delta
    level.version += 1
    level.save(using=alias, update_fields=["reserved", "version", "updated_at"])
    kind = Type.RESERVE if delta > 0 else Type.RELEASE
    InventoryMovement.objects.using(alias).create(
        variant_id=reservation.variant_id,
        warehouse_id=reservation.warehouse_id,
        movement_type=kind,
        quantity=delta,
        on_hand_after=level.on_hand,
        reserved_after=level.reserved,
        reference_type=InventoryMovement.Reference.RESERVATION,
        reference_id=reservation.pk,
        reason=text,
        idempotency_key=f"R/{reservation.pk}/{kind.lower()}",
        created_by_type=actor[0],
        created_by_user_id=actor[1],
    )


def _close(
    alias: str,
    reservation: InventoryReservation,
    level: InventoryLevel,
    status: str,
    text: str,
    actor: tuple[str, UUID | None],
) -> None:
    """Cierra una reserva ACTIVE ya bloqueada: solo escribe las columnas del cierre (las únicas
    que el runtime puede cambiar) y devuelve sus unidades a la celda."""
    reservation.status, reservation.ended_at, reservation.end_reason = status, _now(), text
    reservation.ended_by_type, reservation.ended_by_user_id = actor
    closing = ["status", "ended_at", "ended_by_type", "ended_by_user_id", "end_reason"]
    reservation.save(using=alias, update_fields=closing)
    _movement(alias, reservation, level, -reservation.quantity, text, actor)


def _expire_due(alias: str, ctx: TenantContext, level: InventoryLevel, label: str) -> None:
    """Vence, en esta transacción, las reservas ACTIVE de la celda (ya bloqueada) cuyo plazo pasó:
    una reserva vencida libera su unidad aunque el job (F3-34) no haya pasado. Las vence el
    sistema, como lo hará el job."""
    due = (
        InventoryReservation.objects.using(alias)
        .select_for_update()
        .filter(
            variant_id=level.variant_id,
            warehouse_id=level.warehouse_id,
            status=Status.ACTIVE,
            expires_at__lte=_now(),
        )
    )
    for reservation in due.order_by("pk"):
        before = level.reserved
        _close(alias, reservation, level, Status.EXPIRED, EXPIRED, _SYSTEM)
        entity = Entity("inventory_reservation", reservation.pk, label)
        changes = {"reserved": [before, level.reserved], "status": [Status.ACTIVE, Status.EXPIRED]}
        record(ctx, "inventory.reservation_expired", entity, changes, _about(reservation))


def _about(reservation: InventoryReservation) -> dict[str, object]:
    return {
        "variant_id": str(reservation.variant_id),
        "warehouse_id": str(reservation.warehouse_id),
        "quantity": reservation.quantity,
    }


def reserve(ctx: TenantContext, wanted: Wanted) -> tuple[InventoryReservation, bool]:
    """Aparta `quantity` unidades disponibles de la celda, con su movimiento `RESERVE` y su
    auditoría, en la misma transacción. Devuelve la reserva y si es nueva: la misma clave con lo
    mismo devuelve la de entonces sin escribir. `BadReference`, `VariantInactive`,
    `WarehouseInactive`, `InsufficientStock` o `KeyReused` sin escribir nada; `ValueError` si lo
    pedido no es válido."""
    alias = require_scope(ctx)
    _check(wanted)
    try:
        with transaction.atomic(using=alias):
            variant, warehouse, level = lock_cell(
                alias, ctx, wanted.variant_id, wanted.warehouse_id
            )
            if (done := _replay(alias, wanted)) is not None:
                return done, False
            if variant.status != ProductVariant.Status.ACTIVE:
                raise VariantInactive(wanted.variant_id)
            if not warehouse[0]:
                raise WarehouseInactive(wanted.warehouse_id)
            label = f"{variant.sku} @ {warehouse[1]}"
            _expire_due(alias, ctx, level, label)
            if wanted.quantity > level.on_hand - level.reserved:
                raise InsufficientStock(wanted.variant_id)
            actor = _actor(ctx)
            reservation = InventoryReservation.objects.using(alias).create(
                variant_id=wanted.variant_id,
                warehouse_id=wanted.warehouse_id,
                quantity=wanted.quantity,
                expires_at=wanted.expires_at,
                reason=reason(wanted.reason),
                idempotency_key=wanted.idempotency_key,
                created_by_type=actor[0],
                created_by_user_id=actor[1],
            )
            before = level.reserved
            _movement(alias, reservation, level, wanted.quantity, reservation.reason, actor)
            entity = Entity("inventory_reservation", reservation.pk, label)
            metadata = {**_about(reservation), "expires_at": wanted.expires_at.isoformat()}
            changes = {"reserved": [before, level.reserved]}
            record(ctx, "inventory.reserved", entity, changes, metadata)
    except IntegrityError as error:
        if KEY_TAKEN not in str(error):
            raise
        done = _replay(alias, wanted)  # la misma clave, a la vez, para otra celda
        assert done is not None  # noqa: S101 — el índice único acaba de verla
        return done, False
    return reservation, True


def release(
    ctx: TenantContext, reservation_id: UUID, why: str
) -> tuple[InventoryReservation, bool]:
    """Libera una reserva ACTIVE: devuelve sus unidades a la celda con un movimiento `RELEASE` y
    la audita, también en un almacén desactivado. Devuelve la reserva y si se liberó ahora: una ya
    liberada se devuelve sin escribir. `ReservationNotActive` si venció o se consumió;
    `InventoryReservation.DoesNotExist` si no es de la organización."""
    alias = require_scope(ctx)
    text = reason(why)
    with transaction.atomic(using=alias):
        rows = InventoryReservation.objects.using(alias)
        seen: InventoryReservation = rows.get(pk=reservation_id)
        if seen.status != Status.ACTIVE:  # un estado terminal ya no cambia (trigger)
            return _ended(seen)
        variant, warehouse, level = lock_cell(alias, ctx, seen.variant_id, seen.warehouse_id)
        seen = rows.select_for_update().get(pk=reservation_id)  # releída, ya bloqueada
        if seen.status != Status.ACTIVE:
            return _ended(seen)
        before = level.reserved
        _close(alias, seen, level, Status.RELEASED, text, _actor(ctx))
        entity = Entity("inventory_reservation", seen.pk, f"{variant.sku} @ {warehouse[1]}")
        changes = {"reserved": [before, level.reserved], "status": [Status.ACTIVE, Status.RELEASED]}
        record(ctx, "inventory.reservation_released", entity, changes, _about(seen))
    return seen, True


def _ended(reservation: InventoryReservation) -> tuple[InventoryReservation, bool]:
    if reservation.status != Status.RELEASED:
        raise ReservationNotActive(reservation.status)
    return reservation, False
