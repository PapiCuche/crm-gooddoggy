"""El servicio de inventario (F3-09, E04-02): ingresos, salidas y ajustes de una variante en un
almacén. Es lo único que cambia `inventory_levels`, y siempre junto a su movimiento.

No comprueba permisos: los declara la API (`inventory.adjust`), la única que lo importa
(contrato de import-linter). Concurrencia: la variante se bloquea como al borrarla en el catálogo
(no se borra a mitad de un movimiento; los de una misma variante van de uno en uno). La fila del
saldo se crea si falta (`ON CONFLICT DO NOTHING`) y se bloquea (`FOR UPDATE`) antes de leerla, así
que el segundo movimiento de una celda ve lo que dejó el primero. La clave de idempotencia se
busca con la fila ya bloqueada; si la misma clave llega a la vez para otra celda, la decide el
índice único. Todo lo que escriba `inventory_levels` (reservas y transferencias incluidas) bloquea
antes la variante así; si no, los borrados de catálogo y de almacenes podrían no ver el stock.
Quien bloquea varias variantes en una transacción (la importación, F3-21; el catálogo al borrar o
renombrar un producto) lo hace por id ascendente: si no, dos de ellas se interbloquean.
"""

import re
from dataclasses import dataclass
from uuid import UUID

from django.db import IntegrityError, connections, transaction

from apps.audit.services import Entity, record
from apps.catalog.models import ProductVariant
from apps.catalog.selectors import live_variants
from apps.inventory.models import InventoryLevel, InventoryMovement
from core.tenancy.context import ActorType, TenantContext
from core.tenancy.scope import require_scope
from core.text import line

Type, Reference = InventoryMovement.Type, InventoryMovement.Reference
KINDS = (Type.RECEIPT, Type.ISSUE, Type.ADJUSTMENT)  # las que registra una persona (F3-09)
QUANTITY_MAX, REASON_MAX = 1_000_000, 255
ON_HAND_MAX = 2_147_483_647  # `inventory_levels.on_hand` es INT (F3-12)
KEY = re.compile(r"[A-Za-z0-9._:-]{8,100}")
KEY_TAKEN = "inventory_movements_key_uq"
IMPORT_KEY = "import:"  # el prefijo de las claves que derivan las importaciones (F3-21)
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


class StockLimit(Exception):
    """El movimiento dejaría `on_hand` por encima de `ON_HAND_MAX`: no cabe en la columna."""


class KeyReused(Exception):
    """La clave de idempotencia ya se usó para otro movimiento."""


class Stale(Exception):
    """El saldo de la celda, ya bloqueada, no es `Wanted.expected_on_hand` (F3-21)."""

    def __init__(self, on_hand: int) -> None:
        super().__init__(on_hand)
        self.on_hand = on_hand


@dataclass(frozen=True, slots=True)
class Wanted:
    """Lo que se pide. `quantity`: lo que entra o sale, o, en un ajuste, lo contado."""

    variant_id: UUID
    warehouse_id: UUID
    movement_type: str
    quantity: int
    reason: str
    idempotency_key: str
    expected_on_hand: int | None = None  # la importación (F3-21): si no es el saldo, `Stale`
    reference_type: str = InventoryMovement.Reference.MANUAL  # o IMPORT, con `reference_id`
    reference_id: UUID | None = None


def reason(value: str) -> str:
    text = line(value, REASON_MAX)
    if not text:
        raise ValueError("obligatorio")
    return text


def idempotency_key(value: str) -> str:
    """La clave de quien registra un movimiento: nunca una de las que derivan las importaciones,
    que nadie debe poder ocupar antes (F3-21)."""
    if not KEY.fullmatch(value) or value.startswith(IMPORT_KEY):
        raise ValueError(f"de 8 a 100 letras ASCII, cifras y . _ : -, sin empezar por {IMPORT_KEY}")
    return value


def _check(wanted: Wanted) -> None:
    low = 0 if wanted.movement_type == Type.ADJUSTMENT else 1
    if wanted.reference_type not in (Reference.MANUAL, Reference.IMPORT):
        raise ValueError("reference_type: MANUAL o IMPORT")
    if wanted.movement_type not in KINDS:
        raise ValueError(f"movement_type: uno de {', '.join(KINDS)}")
    if not (isinstance(wanted.quantity, int) and low <= wanted.quantity <= QUANTITY_MAX):
        raise ValueError(f"quantity: un entero entre {low} y {QUANTITY_MAX}")
    reason(wanted.reason)
    if wanted.reference_type == Reference.MANUAL:
        idempotency_key(wanted.idempotency_key)
    elif not (
        KEY.fullmatch(wanted.idempotency_key) and wanted.idempotency_key.startswith(IMPORT_KEY)
    ):
        raise ValueError(f"idempotency_key: {IMPORT_KEY}… en una importación")


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
        and (movement.reference_type, movement.reference_id)
        == (wanted.reference_type, wanted.reference_id)
    )


def _replay(alias: str, wanted: Wanted) -> InventoryMovement | None:
    """El movimiento ya registrado con esa clave, si es el mismo; `KeyReused` si no lo es."""
    found = InventoryMovement.objects.using(alias).filter(idempotency_key=wanted.idempotency_key)
    movement: InventoryMovement | None = found.first()
    if movement is not None and not _same(movement, wanted):
        raise KeyReused(wanted.idempotency_key)
    return movement


def _locked_variant(alias: str, variant_id: UUID) -> ProductVariant:
    """La variante viva, bloqueada como al borrarla en el catálogo (`FOR NO KEY UPDATE`): no se
    borra mientras entra o sale su stock, y el movimiento que la esperaba ya no la encuentra."""
    rows = live_variants().using(alias).select_for_update(no_key=True, of=("self",))
    variant: ProductVariant | None = rows.filter(pk=variant_id).first()
    if variant is None:
        raise BadReference("variant_id", "No existe en esta organización.")
    if not variant.product.tracks_inventory:
        raise BadReference("variant_id", "Es un servicio: no lleva stock.")
    return variant


def _locked_level(alias: str, ctx: TenantContext, wanted: Wanted) -> InventoryLevel:
    cell = {"variant_id": wanted.variant_id, "warehouse_id": wanted.warehouse_id}
    new = InventoryLevel(organization_id=ctx.organization_id, **cell)
    InventoryLevel.objects.using(alias).bulk_create([new], ignore_conflicts=True)
    level: InventoryLevel = InventoryLevel.objects.using(alias).select_for_update().get(**cell)
    return level


def move(ctx: TenantContext, wanted: Wanted) -> tuple[InventoryMovement, bool]:
    """Registra el movimiento y deja el saldo como queda, en la misma transacción, y lo audita.
    Devuelve el movimiento y si es nuevo: la misma clave con lo mismo devuelve el de entonces
    sin escribir nada. `BadReference`, `WarehouseInactive`, `Stale`, `InsufficientStock`,
    `StockLimit` o `KeyReused` sin escribir nada."""
    alias = require_scope(ctx)
    _check(wanted)
    try:
        with transaction.atomic(using=alias):
            variant = _locked_variant(alias, wanted.variant_id)
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
            if wanted.expected_on_hand not in (None, level.on_hand):
                raise Stale(level.on_hand)
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
    if level.on_hand + delta > ON_HAND_MAX:  # antes de escribir: PostgreSQL daría un 500
        raise StockLimit(wanted.variant_id)
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
        reference_type=wanted.reference_type,
        reference_id=wanted.reference_id,
        idempotency_key=wanted.idempotency_key,
        created_by_type=ctx.actor_type,
        created_by_user_id=user,
    )
    return movement
