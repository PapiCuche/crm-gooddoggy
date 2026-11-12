"""Reservas de stock (F3-33, E04-05 y E04-08): `RESERVE` sube `reserved` de la celda variante ×
almacén y liberar o vencer lo baja con un `RELEASE`. Sin permisos: los exige quien lo llama, su
API (F3-35, `inventory.manage`) y, en la Fase 4, el servicio de cotizaciones tras su propio
permiso. Bloqueo como `stock.move` (`stock.lock_cell`) y después la reserva `FOR UPDATE`,
releída. La clave del cliente va en la reserva; los movimientos llevan `R/<reserva>/reserve|release`
(la `/` no cabe en una clave de cliente): no se libera dos veces.
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
MAX_ACTIVE_PER_CELL = 500  # ACTIVE por variante × almacén (F3-35); a confirmar por el mantenedor
EXPIRED = "Reserva vencida"
KEY_TAKEN, EXPIRES_CK = "inventory_res_key_uq", "inventory_res_expires_ck"
_SYSTEM: tuple[str, UUID | None] = (ActorType.SYSTEM, None)


class VariantInactive(Exception):
    """La variante no está ACTIVE: no se reserva."""


class TooManyReservations(Exception):
    """La celda ya tiene `MAX_ACTIVE_PER_CELL` reservas ACTIVE: libera o espera a que venzan."""


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


def _same(found: InventoryReservation, wanted: Wanted, actor: tuple[str, UUID | None]) -> bool:
    """Si la reserva guardada con la clave es la que se pide ahora: la misma petición de la misma
    persona (otra no recibe la reserva ajena; F3-35)."""
    return (
        (found.created_by_type, found.created_by_user_id) == actor
        and found.variant_id == wanted.variant_id
        and found.warehouse_id == wanted.warehouse_id
        and found.quantity == wanted.quantity
        and found.expires_at == wanted.expires_at
        and found.reason == stock.reason(wanted.reason)
    )


def _replay(
    alias: str, wanted: Wanted, actor: tuple[str, UUID | None]
) -> InventoryReservation | None:
    rows = InventoryReservation.objects.using(alias).filter(idempotency_key=wanted.idempotency_key)
    found: InventoryReservation | None = rows.first()
    if found is not None and not _same(found, wanted, actor):
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
    """Cambia `reserved` de la celda bloqueada y deja su `RESERVE` o `RELEASE` en el ledger."""
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
    """Cierra una reserva ACTIVE bloqueada (solo las columnas del cierre) y libera sus unidades."""
    reservation.status, reservation.ended_at, reservation.end_reason = status, _now(), text
    reservation.ended_by_type, reservation.ended_by_user_id = actor
    closing = ["status", "ended_at", "ended_by_type", "ended_by_user_id", "end_reason"]
    reservation.save(using=alias, update_fields=closing)
    _movement(alias, reservation, level, -reservation.quantity, text, actor)


def _expire_due(alias: str, ctx: TenantContext, level: InventoryLevel, label: str) -> None:
    """Vence las reservas ACTIVE de la celda (bloqueada) cuyo plazo pasó, como el sistema: libera su
    unidad aunque el job (F3-34) no haya pasado."""
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
        _expire(alias, ctx, reservation, level, label)


def _expire(
    alias: str, ctx: TenantContext, reservation: InventoryReservation, level: InventoryLevel,
    label: str,
) -> None:  # fmt: skip
    """Vence una reserva ACTIVE bloqueada, como el sistema: el camino de reservar y el del job."""
    _close(alias, reservation, level, Status.EXPIRED, EXPIRED, _SYSTEM)
    _audit(ctx, "reservation_expired", reservation, label, level, _SYSTEM)


def _audit(
    ctx: TenantContext,
    action: str,
    reservation: InventoryReservation,
    label: str,
    level: InventoryLevel,
    actor: tuple[str, UUID | None] | None = None,
) -> None:
    """`inventory.<action>` de la reserva: `reserved` antes y después, y su estado si se cerró."""
    closed = reservation.status != Status.ACTIVE
    before = level.reserved + (reservation.quantity if closed else -reservation.quantity)
    changes: dict[str, list[object]] = {"reserved": [before, level.reserved]}
    if closed:
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
    nueva (la misma clave con lo mismo y de la misma persona devuelve la de entonces).
    `BadReference`, `VariantInactive`, `ValueError` o, de `stock`, `WarehouseInactive`,
    `InsufficientStock`, `KeyReused`; `TooManyReservations`."""
    alias = require_scope(ctx)
    _check(wanted)
    actor = _actor(ctx)
    try:
        with transaction.atomic(using=alias):
            variant, warehouse, level = stock.lock_cell(
                alias, ctx, wanted.variant_id, wanted.warehouse_id
            )
            if (done := _replay(alias, wanted, actor)) is not None:
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
            open_ones = InventoryReservation.objects.using(alias).filter(
                variant_id=wanted.variant_id, warehouse_id=wanted.warehouse_id, status=Status.ACTIVE
            )
            if open_ones.count() >= MAX_ACTIVE_PER_CELL:  # con la celda bloqueada
                raise TooManyReservations(wanted.variant_id)
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
            _movement(alias, reservation, level, wanted.quantity, reservation.reason, actor)
            _audit(ctx, "reserved", reservation, label, level)
    except IntegrityError as error:
        violated = getattr(getattr(error.__cause__, "diag", None), "constraint_name", None)
        if violated == EXPIRES_CK:  # el plazo pasó entre la comprobación y el INSERT
            raise ValueError("expires_at: en el futuro") from None
        if violated != KEY_TAKEN:  # por su nombre, no por el texto: una clave puede decirlo
            raise
        done = _replay(alias, wanted, actor)  # la misma clave, a la vez, para otra celda
        assert done is not None  # noqa: S101 — el índice único acaba de verla
        return done, False
    return reservation, True


def release(ctx: TenantContext, pk: UUID, why: str) -> tuple[InventoryReservation, bool]:
    """Libera una reserva ACTIVE con su `RELEASE` y su auditoría, también en un almacén desactivado.
    Devuelve la reserva y si se liberó ahora (una ya liberada, sin escribir). `ReservationNotActive`
    si venció o se consumió; `InventoryReservation.DoesNotExist` si no es de la organización."""
    alias = require_scope(ctx)
    text = stock.reason(why)
    with transaction.atomic(using=alias):
        rows = InventoryReservation.objects.using(alias)
        seen: InventoryReservation = rows.get(pk=pk)
        if seen.status != Status.ACTIVE:  # un estado terminal ya no cambia (trigger)
            return _ended(seen)
        variant, warehouse, level = stock.lock_cell(alias, ctx, seen.variant_id, seen.warehouse_id)
        seen = rows.select_for_update().get(pk=pk)  # releída, ya bloqueada
        if seen.status != Status.ACTIVE:
            return _ended(seen)
        if _due(seen):  # venció: la cierra el job (o la próxima reserva de la celda), no se libera
            raise ReservationNotActive(Status.EXPIRED)
        _close(alias, seen, level, Status.RELEASED, text, _actor(ctx))
        _audit(ctx, "reservation_released", seen, f"{variant.sku} @ {warehouse[1]}", level)
    return seen, True


def expire(ctx: TenantContext, pk: UUID) -> bool:
    """Vence una reserva ACTIVE cuyo plazo ya pasó (el job de F3-34), en su transacción y por el
    mismo camino que liberar: celda bloqueada y reserva `FOR UPDATE` releída, con su estado y su
    plazo. Devuelve si la venció ahora; una ya cerrada, aún vigente o de otra organización no
    escribe nada (dos ejecuciones a la vez, o contra una liberación, dejan un solo `RELEASE`)."""
    alias = require_scope(ctx)
    with transaction.atomic(using=alias):
        rows = InventoryReservation.objects.using(alias)
        seen = rows.filter(pk=pk).first()
        if seen is None or not _due(seen):
            return False
        variant, warehouse, level = stock.lock_cell(alias, ctx, seen.variant_id, seen.warehouse_id)
        seen = rows.select_for_update().filter(pk=pk).first()  # releída, ya bloqueada
        if seen is None or not _due(seen):
            return False
        _expire(alias, ctx, seen, level, f"{variant.sku} @ {warehouse[1]}")
    return True


def _due(reservation: InventoryReservation) -> bool:
    return reservation.status == Status.ACTIVE and reservation.expires_at <= _now()


def _ended(reservation: InventoryReservation) -> tuple[InventoryReservation, bool]:
    if reservation.status != Status.RELEASED:
        raise ReservationNotActive(reservation.status)
    return reservation, False
