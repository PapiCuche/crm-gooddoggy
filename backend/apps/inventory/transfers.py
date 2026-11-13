"""Transferencias entre almacenes (F3-30, E04-04): unidades de una variante salen de un almacén y
entran en otro en una sola transacción, con dos movimientos (`TRANSFER_OUT` en el origen y
`TRANSFER_IN` en el destino) del mismo `transfer_group_id`, o ninguno.

Sin permisos: los exige su API (F3-31, `inventory.transfer`), la única que lo importa (contrato
de import-linter). Bloqueo, el de `stock` extendido: la variante (`FOR NO KEY UPDATE`; todo lo que
escribe `inventory_levels` la bloquea antes, así que las transferencias de una variante van de una
en una y A→B y B→A a la vez no se interbloquean), los dos almacenes (`FOR KEY SHARE`) por `id`
ascendente, los dos saldos (creados si faltan, `FOR UPDATE`) por `warehouse_id` ascendente, la
clave de idempotencia y después las reglas. La clave del cliente va en la pata `TRANSFER_OUT`; la
`TRANSFER_IN` lleva `T/<transfer_group_id>/in`, con una `/` que no cabe en una clave de cliente.
"""

from dataclasses import dataclass
from uuid import UUID

from django.db import IntegrityError, transaction

from apps.audit.services import Entity, record
from apps.inventory import stock
from apps.inventory.models import InventoryLevel, InventoryMovement
from core.ids import new_id
from core.tenancy.context import ActorType, TenantContext
from core.tenancy.scope import require_scope

Type = InventoryMovement.Type
Legs = tuple[InventoryMovement, InventoryMovement]  # (TRANSFER_OUT, TRANSFER_IN)
SIDES = ("from_warehouse_id", "to_warehouse_id")


class WarehouseInactive(stock.WarehouseInactive):
    """Uno de los dos almacenes está desactivado; `field` dice cuál."""

    def __init__(self, field: str) -> None:
        super().__init__(field)
        self.field = field


@dataclass(frozen=True, slots=True)
class Wanted:
    """`quantity` unidades de la variante del almacén `from_warehouse_id` al `to_warehouse_id`."""

    variant_id: UUID
    from_warehouse_id: UUID
    to_warehouse_id: UUID
    quantity: int
    reason: str
    idempotency_key: str

    def __post_init__(self) -> None:  # ids como UUID: la misma petición con `str` no es otra
        for name in ("variant_id", *SIDES):
            try:
                object.__setattr__(self, name, UUID(str(getattr(self, name))))
            except ValueError:
                raise ValueError(f"{name}: un UUID") from None


def _check(wanted: Wanted) -> None:
    if type(wanted.quantity) is not int or not 1 <= wanted.quantity <= stock.QUANTITY_MAX:
        raise ValueError(f"quantity: un entero entre 1 y {stock.QUANTITY_MAX}")
    stock.reason(wanted.reason)
    stock.idempotency_key(wanted.idempotency_key)
    if wanted.from_warehouse_id == wanted.to_warehouse_id:
        raise ValueError("to_warehouse_id: distinto del almacén de origen")


def _in_key(group: UUID) -> str:
    return f"T/{group}/in"


def _replay(alias: str, wanted: Wanted) -> Legs | None:
    """Las dos patas ya registradas con esa clave, si son lo que se pide; `KeyReused` si la clave
    es de otra transferencia o de otro movimiento."""
    rows = InventoryMovement.objects.using(alias)
    out: InventoryMovement | None = rows.filter(idempotency_key=wanted.idempotency_key).first()
    if out is None:
        return None
    entry = None
    if out.movement_type == Type.TRANSFER_OUT and out.transfer_group_id is not None:
        entry = rows.filter(idempotency_key=_in_key(out.transfer_group_id)).first()
    if entry is None or (
        out.variant_id,
        out.warehouse_id,
        entry.warehouse_id,
        -out.quantity,
        out.reason,
    ) != (
        wanted.variant_id,
        wanted.from_warehouse_id,
        wanted.to_warehouse_id,
        wanted.quantity,
        stock.reason(wanted.reason),
    ):
        raise stock.KeyReused(wanted.idempotency_key)
    return out, entry


def transfer(ctx: TenantContext, wanted: Wanted) -> tuple[Legs, bool]:
    """Saca `quantity` del origen y la mete en el destino, deja los dos saldos y sus dos
    movimientos, y lo audita (`inventory.transferred`). Devuelve las patas y si son nuevas: la
    misma clave con lo mismo devuelve las de entonces sin escribir nada. `ValueError`,
    `BadReference`, `WarehouseInactive`, `InsufficientStock`, `StockLimit` o `KeyReused` sin
    escribir nada."""
    alias = require_scope(ctx)
    _check(wanted)
    try:
        with transaction.atomic(using=alias):
            variant = stock.lock_variant(alias, wanted.variant_id)
            sides = dict(
                zip((wanted.from_warehouse_id, wanted.to_warehouse_id), SIDES, strict=True)
            )
            warehouses = {w: stock.lock_warehouse(alias, w, sides[w]) for w in sorted(sides)}
            levels = stock.lock_levels(alias, ctx, wanted.variant_id, *sides)
            if (done := _replay(alias, wanted)) is not None:
                return done, False
            for warehouse_id, field in sides.items():
                if not warehouses[warehouse_id][0]:
                    raise WarehouseInactive(field)
            by_id = {level.warehouse_id: level for level in levels}
            source, target = by_id[wanted.from_warehouse_id], by_id[wanted.to_warehouse_id]
            if source.on_hand - wanted.quantity < source.reserved:  # se mueve lo disponible
                raise stock.InsufficientStock(wanted.variant_id)
            if target.on_hand + wanted.quantity > stock.ON_HAND_MAX:
                raise stock.StockLimit(wanted.variant_id)
            group = new_id()
            legs = (
                _leg(alias, ctx, wanted, source, -wanted.quantity, group),
                _leg(alias, ctx, wanted, target, wanted.quantity, group),
            )
            codes = (warehouses[wanted.from_warehouse_id][1], warehouses[wanted.to_warehouse_id][1])
            changes = {
                "from_on_hand": [source.on_hand + wanted.quantity, source.on_hand],
                "to_on_hand": [target.on_hand - wanted.quantity, target.on_hand],
            }
            metadata = {
                "variant_id": str(variant.pk),
                "from_warehouse_id": str(wanted.from_warehouse_id),
                "to_warehouse_id": str(wanted.to_warehouse_id),
                "quantity": wanted.quantity,
            }
            entity = Entity("inventory_transfer", group, f"{variant.sku}: {codes[0]} → {codes[1]}")
            record(ctx, "inventory.transferred", entity, changes, metadata)
    except IntegrityError as error:
        violated = getattr(getattr(error.__cause__, "diag", None), "constraint_name", None)
        if violated != stock.KEY_TAKEN:  # por su nombre, no por el texto
            raise
        done = _replay(alias, wanted)  # la misma clave, a la vez, para otra variante
        assert done is not None  # noqa: S101 — el índice único acaba de verla
        return done, False
    return legs, True


def _leg(
    alias: str, ctx: TenantContext, wanted: Wanted, level: InventoryLevel, delta: int, group: UUID
) -> InventoryMovement:
    """Cambia `on_hand` del saldo bloqueado y deja su pata en el ledger."""
    level.on_hand += delta
    level.version += 1
    level.save(using=alias, update_fields=["on_hand", "version", "updated_at"])
    out = delta < 0
    movement: InventoryMovement = InventoryMovement.objects.using(alias).create(
        variant_id=wanted.variant_id,
        warehouse_id=level.warehouse_id,
        movement_type=Type.TRANSFER_OUT if out else Type.TRANSFER_IN,
        quantity=delta,
        on_hand_after=level.on_hand,
        reserved_after=level.reserved,
        reason=stock.reason(wanted.reason),
        reference_type=InventoryMovement.Reference.TRANSFER,
        reference_id=group,
        transfer_group_id=group,
        idempotency_key=wanted.idempotency_key if out else _in_key(group),
        created_by_type=ctx.actor_type,
        created_by_user_id=ctx.user_id if ctx.actor_type == ActorType.USER else None,
    )
    return movement
