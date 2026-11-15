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
Quien bloquea varias variantes en una transacción (la importación al aplicarla y al revertirla,
F3-21 y F3-24; el catálogo al borrar o renombrar un producto) lo hace por id ascendente: si no,
dos de ellas se interbloquean.
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


class ActorRefused(Exception):
    """Un agente de IA o una integración: no mueve, reserva ni transfiere stock (ADR-034 §11: la IA
    no toca el stock en el primer corte; F3-39). Tampoco tiene `user_id` y el ledger no guarda su
    `actor_id`: dos de la misma organización repetirían lo del otro con su clave.

    Hereda de `Exception` y no de `ValueError` a propósito: las rutas convierten ciertos
    `ValueError` del servicio en un 400 sobre un campo (`expires_at` en reservas). Un actor
    rechazado nunca debe acabar ahí, ni en ningún otro campo: su respuesta es el 403."""

    def __init__(self, actor_type: str) -> None:
        super().__init__(actor_type)  # `args` reconstruye la excepción (Celery la serializa)
        self.actor_type = actor_type

    def __str__(self) -> str:
        return f"actor {self.actor_type}: no mueve, reserva ni transfiere stock"


class ActorMismatch(ActorRefused):
    """Un contexto de persona sin su persona: `user_id` falta o no es `actor_id` (F3-39b). Es un
    `ActorRefused` (el mismo 403, nunca un 400 sobre un campo ni un 500).

    Así llega una tarea de Celery encolada con `actor_type=USER`: `@tenant_task` arma el contexto
    con `user_id=None` (tenancy-context §3; `user_id` fija `app.user_id`, que lee RLS, y cambiarlo
    es una decisión de ADR). Por eso una tarea que mueve, reserva o transfiere stock actúa como
    `SYSTEM` (el valor por defecto) y, si lo hace por una persona, la guarda en sus datos."""

    def __init__(self) -> None:
        super().__init__(ActorType.USER)

    def __reduce__(self) -> tuple[type[ActorMismatch], tuple[()]]:
        return type(self), ()

    def __str__(self) -> str:
        return "un contexto de persona necesita user_id igual a actor_id"


REFUSED_ACTORS = (ActorType.AI_AGENT, ActorType.INTEGRATION)


def actor(ctx: TenantContext) -> tuple[str, UUID | None]:
    """Quién mueve, reserva o transfiere, como lo guarda el ledger: una persona (con `user_id`, que
    es su `actor_id`: la auditoría lee este) o el sistema. `ActorRefused` para un agente de IA o
    una integración; `ActorMismatch` si un contexto de persona no trae a la persona."""
    if ctx.actor_type in REFUSED_ACTORS:
        raise ActorRefused(ctx.actor_type)
    if ctx.actor_type != ActorType.USER:
        return ctx.actor_type, None
    if ctx.user_id is None or ctx.actor_id != ctx.user_id:
        raise ActorMismatch
    return ctx.actor_type, ctx.user_id


class Stale(Exception):
    """El saldo de la celda, ya bloqueada, no es `Wanted.expected_on_hand` (F3-21), o su
    `version` no es `Wanted.expected_version` (F3-24): alguien la escribió después."""

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
    expected_version: int | None = None  # revertirla (F3-24): si la celda se escribió, `Stale`
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


def lock_variant(alias: str, variant_id: UUID) -> ProductVariant:
    """La variante viva, bloqueada como al borrarla en el catálogo (`FOR NO KEY UPDATE`): no se
    borra mientras entra o sale su stock, y el movimiento que la esperaba ya no la encuentra."""
    rows = live_variants().using(alias).select_for_update(no_key=True, of=("self",))
    variant: ProductVariant | None = rows.filter(pk=variant_id).first()
    if variant is None:
        raise BadReference("variant_id", "No existe en esta organización.")
    if not variant.product.tracks_inventory:
        raise BadReference("variant_id", "Es un servicio: no lleva stock.")
    return variant


def lock_warehouse(alias: str, warehouse_id: UUID, field: str = "warehouse_id") -> tuple[bool, str]:
    """El almacén vivo (`FOR KEY SHARE`): su `is_active` y su `code`. `BadReference(field)` si no
    existe en la organización."""
    with connections[alias].cursor() as cursor:
        cursor.execute(_WAREHOUSE, [warehouse_id])
        warehouse = cursor.fetchone()
    if warehouse is None:
        raise BadReference(field, "No existe en esta organización.")
    return warehouse[0], warehouse[1]


def lock_levels(
    alias: str, ctx: TenantContext, variant_id: UUID, *warehouse_ids: UUID
) -> list[InventoryLevel]:
    """Los saldos de la variante en esos almacenes, creados si faltan y bloqueados (`FOR UPDATE`)
    por `warehouse_id` ascendente, en ese orden."""
    ids = sorted(set(warehouse_ids))
    new = [
        InventoryLevel(organization_id=ctx.organization_id, variant_id=variant_id, warehouse_id=w)
        for w in ids
    ]
    InventoryLevel.objects.using(alias).bulk_create(new, ignore_conflicts=True)
    rows = InventoryLevel.objects.using(alias).select_for_update()
    return list(rows.filter(variant_id=variant_id, warehouse_id__in=ids).order_by("warehouse_id"))


def lock_cell(
    alias: str, ctx: TenantContext, variant_id: UUID, warehouse_id: UUID
) -> tuple[ProductVariant, tuple[bool, str], InventoryLevel]:
    """El orden de bloqueo de lo que escribe una celda (movimientos y reservas): variante, almacén
    vivo (`FOR KEY SHARE`; su `is_active` y `code`) y saldo, creado si falta y bloqueado."""
    variant = lock_variant(alias, variant_id)
    warehouse = lock_warehouse(alias, warehouse_id)
    (level,) = lock_levels(alias, ctx, variant_id, warehouse_id)
    return variant, warehouse, level


def move(ctx: TenantContext, wanted: Wanted) -> tuple[InventoryMovement, bool]:
    """Registra el movimiento y deja el saldo como queda, en la misma transacción, y lo audita.
    Devuelve el movimiento y si es nuevo: la misma clave con lo mismo devuelve el de entonces
    sin escribir nada. `ActorRefused` (un agente de IA o una integración: ADR-034, F3-39b) antes
    de la primera consulta; `BadReference`, `WarehouseInactive`, `Stale`, `InsufficientStock`,
    `StockLimit` o `KeyReused` sin escribir nada."""
    alias = require_scope(ctx)
    who = actor(ctx)  # antes de bloquear nada
    _check(wanted)
    try:
        with transaction.atomic(using=alias):
            variant, warehouse, level = lock_cell(
                alias, ctx, wanted.variant_id, wanted.warehouse_id
            )
            if (done := _replay(alias, wanted)) is not None:
                return done, False
            if not warehouse[0]:
                raise WarehouseInactive(wanted.warehouse_id)
            if wanted.expected_on_hand not in (None, level.on_hand) or (
                wanted.expected_version not in (None, level.version)
            ):
                raise Stale(level.on_hand)
            movement = _apply(alias, who, wanted, level)
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
    alias: str, who: tuple[str, UUID | None], wanted: Wanted, level: InventoryLevel
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
        created_by_type=who[0],
        created_by_user_id=who[1],
    )
    return movement
