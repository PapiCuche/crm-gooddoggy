"""El servicio de inventario (F3-09, E04-02): ingresos, salidas y ajustes de una variante en un
almacén. Es lo único que cambia `inventory_levels`, y siempre junto a su movimiento.

No comprueba permisos: los declara la API (`inventory.adjust`), la única que lo importa
(contrato de import-linter). Concurrencia: la fila del saldo se crea si falta (`ON CONFLICT DO
NOTHING`) y se bloquea (`FOR UPDATE`) antes de leerla, así que dos movimientos de la misma celda
van de uno en uno y el segundo ve lo que dejó el primero. La clave de idempotencia se busca con
la fila ya bloqueada; si la misma clave llega a la vez para otra celda, la decide el índice único.
"""

import re
from dataclasses import dataclass
from uuid import UUID

from django.db import IntegrityError, connections, transaction

from apps.audit.services import Entity, record
from apps.catalog.selectors import live_variants
from apps.inventory.models import InventoryLevel, InventoryMovement
from core.tenancy.context import ActorType, TenantContext
from core.tenancy.scope import require_scope
from core.text import line

Type = InventoryMovement.Type
KINDS = (Type.RECEIPT, Type.ISSUE, Type.ADJUSTMENT)  # las que registra una persona (F3-09)
QUANTITY_MAX, REASON_MAX = 1_000_000, 255
KEY = re.compile(r"[A-Za-z0-9._:-]{8,100}")
KEY_TAKEN = "inventory_movements_key_uq"
ACTIONS = {Type.RECEIPT: "received", Type.ISSUE: "issued", Type.ADJUSTMENT: "adjusted"}
_WAREHOUSE = (  # el almacén vivo, con `FOR KEY SHARE`: no se borra mientras se mueve su stock
    "SELECT is_active, code FROM warehouses "
    "WHERE id = %s AND deleted_at IS NULL AND organization_id = app_current_tenant() "
    "FOR KEY SHARE"
)


class BadReference(ValueError):
    """La variante o el almacén no existen en la organización, o la variante no lleva stock."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field


class WarehouseInactive(Exception):
    """El almacén está desactivado: no recibe movimientos."""


class InsufficientStock(Exception):
    """El movimiento dejaría el stock en negativo o por debajo de lo reservado."""


class KeyReused(Exception):
    """La clave de idempotencia ya se usó para otro movimiento."""


@dataclass(frozen=True, slots=True)
class Wanted:
    """Lo que se pide. `quantity`: lo que entra o sale, o, en un ajuste, lo contado."""

    variant_id: UUID
    warehouse_id: UUID
    movement_type: str
    quantity: int
    reason: str
    idempotency_key: str


def reason(value: str) -> str:
    text = line(value, REASON_MAX)
    if not text:
        raise ValueError("obligatorio")
    return text


def idempotency_key(value: str) -> str:
    if not KEY.fullmatch(value):
        raise ValueError("de 8 a 100 letras ASCII, cifras y . _ : -")
    return value


def _check(wanted: Wanted) -> None:
    low = 0 if wanted.movement_type == Type.ADJUSTMENT else 1
    if wanted.movement_type not in KINDS:
        raise ValueError(f"movement_type: uno de {', '.join(KINDS)}")
    if not (isinstance(wanted.quantity, int) and low <= wanted.quantity <= QUANTITY_MAX):
        raise ValueError(f"quantity: un entero entre {low} y {QUANTITY_MAX}")
    reason(wanted.reason)
    idempotency_key(wanted.idempotency_key)


def _same(movement: InventoryMovement, wanted: Wanted) -> bool:
    """Si el movimiento guardado es el que se pide ahora (la misma petición, repetida)."""
    asked = -wanted.quantity if wanted.movement_type == Type.ISSUE else wanted.quantity
    amount = (
        movement.on_hand_after if wanted.movement_type == Type.ADJUSTMENT else movement.quantity
    )
    return (
        movement.variant_id == wanted.variant_id
        and movement.warehouse_id == wanted.warehouse_id
        and movement.movement_type == wanted.movement_type
        and amount == asked
        and movement.reason == reason(wanted.reason)
    )


def _replay(alias: str, wanted: Wanted) -> InventoryMovement | None:
    """El movimiento ya registrado con esa clave, si es el mismo; `KeyReused` si no lo es."""
    found = InventoryMovement.objects.using(alias).filter(idempotency_key=wanted.idempotency_key)
    movement: InventoryMovement | None = found.first()
    if movement is not None and not _same(movement, wanted):
        raise KeyReused(wanted.idempotency_key)
    return movement


def _locked_level(alias: str, ctx: TenantContext, wanted: Wanted) -> InventoryLevel:
    cell = {"variant_id": wanted.variant_id, "warehouse_id": wanted.warehouse_id}
    new = InventoryLevel(organization_id=ctx.organization_id, **cell)
    InventoryLevel.objects.using(alias).bulk_create([new], ignore_conflicts=True)
    level: InventoryLevel = InventoryLevel.objects.using(alias).select_for_update().get(**cell)
    return level


def move(ctx: TenantContext, wanted: Wanted) -> tuple[InventoryMovement, bool]:
    """Registra el movimiento y deja el saldo como queda, en la misma transacción, y lo audita.
    Devuelve el movimiento y si es nuevo: la misma clave con lo mismo devuelve el de entonces
    sin escribir nada. `BadReference`, `WarehouseInactive`, `InsufficientStock` o `KeyReused`
    sin escribir nada."""
    alias = require_scope(ctx)
    _check(wanted)
    variant = live_variants().using(alias).filter(pk=wanted.variant_id).first()
    if variant is None:
        raise BadReference("variant_id", "No existe en esta organización.")
    if not variant.product.tracks_inventory:
        raise BadReference("variant_id", "Es un servicio: no lleva stock.")
    try:
        with transaction.atomic(using=alias):
            with connections[alias].cursor() as cursor:
                cursor.execute(_WAREHOUSE, [wanted.warehouse_id])
                warehouse = cursor.fetchone()
            if warehouse is None:
                raise BadReference("warehouse_id", "No existe en esta organización.")
            level = _locked_level(alias, ctx, wanted)
            if (done := _replay(alias, wanted)) is not None:
                return done, False
            if not warehouse[0]:
                raise WarehouseInactive(wanted.warehouse_id)
            movement = _apply(alias, ctx, wanted, level)
            label = f"{variant.sku} @ {warehouse[1]}"
            changes = {"on_hand": [level.on_hand - movement.quantity, level.on_hand]}
            metadata = {"variant_id": str(variant.pk), "warehouse_id": str(wanted.warehouse_id)}
            entity = Entity("inventory_movement", movement.pk, label)
            action = f"inventory.{ACTIONS[Type(wanted.movement_type)]}"
            record(ctx, action, entity, changes, {**metadata, "quantity": movement.quantity})
    except IntegrityError as error:
        if KEY_TAKEN not in str(error):
            raise
        done = _replay(alias, wanted)  # la misma clave, a la vez, para otra celda
        assert done is not None  # noqa: S101 — el índice único acaba de verla
        return done, False
    return movement, True


def _apply(
    alias: str, ctx: TenantContext, wanted: Wanted, level: InventoryLevel
) -> InventoryMovement:
    if wanted.movement_type == Type.ADJUSTMENT:
        delta = wanted.quantity - level.on_hand
    else:
        delta = wanted.quantity if wanted.movement_type == Type.RECEIPT else -wanted.quantity
    if level.on_hand + delta < level.reserved:  # nunca negativo ni por debajo de lo reservado
        raise InsufficientStock(wanted.variant_id)
    level.on_hand += delta
    level.version += 1
    level.save(using=alias, update_fields=["on_hand", "version", "updated_at"])
    user = ctx.user_id if ctx.actor_type == ActorType.USER else None
    movement: InventoryMovement = InventoryMovement.objects.using(alias).create(
        variant_id=wanted.variant_id,
        warehouse_id=wanted.warehouse_id,
        movement_type=wanted.movement_type,
        quantity=delta,
        on_hand_after=level.on_hand,
        reserved_after=level.reserved,
        reason=reason(wanted.reason),
        idempotency_key=wanted.idempotency_key,
        created_by_type=ctx.actor_type,
        created_by_user_id=user,
    )
    return movement
