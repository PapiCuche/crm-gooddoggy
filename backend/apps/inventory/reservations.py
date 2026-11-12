"""Reservas de stock (F3-33, E04-05 y E04-08): `RESERVE` sube `reserved` de la celda variante ×
almacén y liberar o vencer lo baja con un `RELEASE`. Sin permisos: los exigirá su API (F3-35), la
única que podrá importarlo. Bloqueo como `stock.move` (`stock.lock_cell`) y después la reserva
`FOR UPDATE`, releída. La clave del cliente va en la reserva; los movimientos llevan
`R/<reserva>/reserve|release` (la `/` no cabe en una clave de cliente): no se libera dos veces.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.audit.services import Entity, record
from apps.catalog.models import ProductVariant
from apps.inventory import stock
from apps.inventory.models import InventoryLevel, InventoryMovement, InventoryReservation
from core.tenancy.context import ActorType, TenantContext
from core.tenancy.scope import require_scope

Status, Type = InventoryReservation.Status, InventoryMovement.Type
EXPIRES_MAX = timedelta(days=30)  # a confirmar por el mantenedor (issue #342)
EXPIRED = "Reserva vencida"
KEY_TAKEN, EXPIRES_CK = "inventory_res_key_uq", "inventory_res_expires_ck"
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

    def __post_init__(self) -> None:  # ids como UUID: la misma petición con `str` no es otra
        for name in ("variant_id", "warehouse_id"):
            try:
                object.__setattr__(self, name, UUID(str(getattr(self, name))))
            except ValueError:
                raise ValueError(f"{name}: un UUID") from None


def _now() -> datetime:
    return timezone.now()


def _check(wanted: Wanted) -> None:
    if type(wanted.quantity) is not int or not 1 <= wanted.quantity <= stock.QUANTITY_MAX:
        raise ValueError(f"quantity: un entero entre 1 y {stock.QUANTITY_MAX}")
    stock.reason(wanted.reason)
    stock.idempotency_key(wanted.idempotency_key)  # `expires_at`, con la celda bloqueada


def _check_expiry(expires: datetime) -> None:
    # se repite con la celda bloqueada: el plazo puede pasar mientras se espera
    if not isinstance(expires, datetime) or expires.tzinfo is None:
        raise ValueError("expires_at: fecha y hora con zona horaria")
    now = _now()
    if not now < expires <= now + EXPIRES_MAX:
        raise ValueError(f"expires_at: en el futuro y dentro de {EXPIRES_MAX.days} días")


def _actor(ctx: TenantContext) -> tuple[str, UUID | None]:
    if ctx.actor_type != ActorType.USER:
        return ctx.actor_type, None
    if ctx.user_id is None or ctx.actor_id != ctx.user_id:  # la auditoría lee `actor_id`
        raise ValueError("un contexto de persona necesita user_id igual a actor_id")
    return ctx.actor_type, ctx.user_id


def _same(found: InventoryReservation, wanted: Wanted) -> bool:
    """Si la reserva guardada con la clave es la que se pide ahora (la misma petición)."""
    return (
        found.variant_id == wanted.variant_id
        and found.warehouse_id == wanted.warehouse_id
        and found.quantity == wanted.quantity
        and found.expires_at == wanted.expires_at
        and found.reason == stock.reason(wanted.reason)
    )


def _replay(alias: str, wanted: Wanted) -> InventoryReservation | None:
    rows = InventoryReservation.objects.using(alias).filter(idempotency_key=wanted.idempotency_key)
    found: InventoryReservation | None = rows.first()
    if found is not None and not _same(found, wanted):
        raise stock.KeyReused(wanted.idempotency_key)
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
        _audit(ctx, "reservation_expired", reservation, label, before, level, _SYSTEM)


def _audit(
    ctx: TenantContext,
    action: str,
    reservation: InventoryReservation,
    label: str,
    before: int,
    level: InventoryLevel,
    actor: tuple[str, UUID | None] | None = None,
) -> None:
    """`inventory.<action>` de la reserva: `reserved` antes y después, y su estado si se cerró."""
    changes: dict[str, list[object]] = {"reserved": [before, level.reserved]}
    if reservation.status != Status.ACTIVE:
        changes["status"] = [Status.ACTIVE, reservation.status]
    about = {
        "variant_id": str(reservation.variant_id),
        "warehouse_id": str(reservation.warehouse_id),
        "quantity": reservation.quantity,
        "expires_at": reservation.expires_at.isoformat(),
    }
    entity = Entity("inventory_reservation", reservation.pk, label)
    record(ctx, f"inventory.{action}", entity, changes, about, actor=actor)


def reserve(ctx: TenantContext, wanted: Wanted) -> tuple[InventoryReservation, bool]:
    """Aparta unidades disponibles con su `RESERVE` y su auditoría. Devuelve la reserva y si es
    nueva (la misma clave con lo mismo devuelve la de entonces). `BadReference`, `VariantInactive`,
    `ValueError` o, de `stock`, `WarehouseInactive`, `InsufficientStock`, `KeyReused`."""
    alias = require_scope(ctx)
    _check(wanted)
    try:
        with transaction.atomic(using=alias):
            variant, warehouse, level = stock.lock_cell(
                alias, ctx, wanted.variant_id, wanted.warehouse_id
            )
            if (done := _replay(alias, wanted)) is not None:
                return done, False
            _check_expiry(wanted.expires_at)
            if variant.status != ProductVariant.Status.ACTIVE:
                raise VariantInactive(wanted.variant_id)
            if not warehouse[0]:
                raise stock.WarehouseInactive(wanted.warehouse_id)
            label = f"{variant.sku} @ {warehouse[1]}"
            _expire_due(alias, ctx, level, label)
            if wanted.quantity > level.on_hand - level.reserved:
                raise stock.InsufficientStock(wanted.variant_id)
            actor = _actor(ctx)
            reservation = InventoryReservation.objects.using(alias).create(
                variant_id=wanted.variant_id,
                warehouse_id=wanted.warehouse_id,
                quantity=wanted.quantity,
                expires_at=wanted.expires_at,
                reason=stock.reason(wanted.reason),
                idempotency_key=wanted.idempotency_key,
                created_by_type=actor[0],
                created_by_user_id=actor[1],
            )
            before = level.reserved
            _movement(alias, reservation, level, wanted.quantity, reservation.reason, actor)
            _audit(ctx, "reserved", reservation, label, before, level)
    except IntegrityError as error:
        if EXPIRES_CK in str(error):  # el plazo pasó entre la comprobación y el INSERT
            raise ValueError("expires_at: en el futuro") from None
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
    text = stock.reason(why)
    with transaction.atomic(using=alias):
        rows = InventoryReservation.objects.using(alias)
        seen: InventoryReservation = rows.get(pk=reservation_id)
        if seen.status != Status.ACTIVE:  # un estado terminal ya no cambia (trigger)
            return _ended(seen)
        variant, warehouse, level = stock.lock_cell(alias, ctx, seen.variant_id, seen.warehouse_id)
        seen = rows.select_for_update().get(pk=reservation_id)  # releída, ya bloqueada
        if seen.status != Status.ACTIVE:
            return _ended(seen)
        before = level.reserved
        _close(alias, seen, level, Status.RELEASED, text, _actor(ctx))
        _audit(ctx, "reservation_released", seen, f"{variant.sku} @ {warehouse[1]}", before, level)
    return seen, True


def _ended(reservation: InventoryReservation) -> tuple[InventoryReservation, bool]:
    if reservation.status != Status.RELEASED:
        raise ReservationNotActive(reservation.status)
    return reservation, False
