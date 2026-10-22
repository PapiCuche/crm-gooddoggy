"""Inventario LOCAL (Fase 3, E04; modelo de datos §E.9). Tablas tenant-owned con FORCE RLS."""

from django.db import models

from core.db.models import TenantModel, uuid7_primary_key


class Warehouse(TenantModel):
    """Almacén de una sucursal: el lugar del stock. `code` es su clave estable, única entre los
    no borrados, y `is_sellable` dice si su stock cuenta para la venta. Borrado lógico."""

    class Type(models.TextChoices):
        STORE = "STORE"
        WAREHOUSE = "WAREHOUSE"
        TRANSIT = "TRANSIT"
        SERVICE = "SERVICE"

    id = uuid7_primary_key()
    branch = models.ForeignKey(  # misma organización: FK compuesta en la migración
        "organizations.Branch",
        models.PROTECT,
        related_name="+",
        db_constraint=False,
        db_index=False,
    )
    code = models.CharField(max_length=20)
    name = models.CharField(max_length=100)
    type = models.CharField(max_length=16, choices=Type.choices, default=Type.WAREHOUSE)
    is_sellable = models.BooleanField(default=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    deleted_at = models.DateTimeField(null=True, blank=True)
    deleted_by_user_id = models.UUIDField(null=True, blank=True)

    class Meta:
        db_table = "warehouses"
        indexes = [
            models.Index(fields=["organization_id", "branch"], name="warehouses_org_branch_idx")
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["organization_id", "code"],
                condition=models.Q(deleted_at__isnull=True),
                name="warehouses_org_code_uq",
            ),
            models.UniqueConstraint(  # destino de las FK compuestas `warehouse_id`
                fields=["organization_id", "id"], name="warehouses_org_id_uq"
            ),
            models.CheckConstraint(
                condition=models.Q(code__regex=r"^[A-Z0-9]+(-[A-Z0-9]+)*$"),
                name="warehouses_code_ck",
            ),
            models.CheckConstraint(
                condition=models.Q(type__in=["STORE", "WAREHOUSE", "TRANSIT", "SERVICE"]),
                name="warehouses_type_ck",
            ),
        ]

    def __str__(self) -> str:
        return self.code


class InventoryLevel(TenantModel):
    """Saldo de una variante en un almacén (modelo de datos §E.9): `available = on_hand -
    reserved`, columna generada. Solo lo cambia el servicio de inventario, con la fila bloqueada
    y siempre junto a un movimiento (`apps.inventory.stock`)."""

    id = uuid7_primary_key()
    variant = models.ForeignKey(  # misma organización: FKs compuestas en la migración
        "catalog.ProductVariant",
        models.PROTECT,
        related_name="+",
        db_constraint=False,
        db_index=False,
    )
    warehouse = models.ForeignKey(
        Warehouse, models.PROTECT, related_name="+", db_constraint=False, db_index=False
    )
    on_hand = models.IntegerField(default=0)
    reserved = models.IntegerField(default=0)
    available = models.GeneratedField(
        expression=models.F("on_hand") - models.F("reserved"),
        output_field=models.IntegerField(),
        db_persist=True,
    )
    low_stock_threshold = models.IntegerField(null=True, blank=True)
    version = models.IntegerField(default=1)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "inventory_levels"
        indexes = [
            models.Index(
                fields=["organization_id", "warehouse"], name="inventory_levels_org_wh_idx"
            )
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["organization_id", "variant", "warehouse"], name="inventory_levels_cell_uq"
            ),
            models.CheckConstraint(
                condition=models.Q(
                    on_hand__gte=0, reserved__gte=0, reserved__lte=models.F("on_hand")
                ),
                name="inventory_levels_stock_ck",
            ),
        ]


class InventoryMovement(TenantModel):
    """Una línea del ledger de stock (modelo de datos §E.9): solo se añade. `quantity` lleva el
    signo del cambio en `on_hand`; un ajuste guarda la diferencia con lo contado. El runtime no
    tiene `UPDATE` ni `DELETE` sobre la tabla (migración)."""

    class Type(models.TextChoices):
        RECEIPT = "RECEIPT"
        ISSUE = "ISSUE"
        ADJUSTMENT = "ADJUSTMENT"
        TRANSFER_OUT = "TRANSFER_OUT"
        TRANSFER_IN = "TRANSFER_IN"
        RESERVE = "RESERVE"
        RELEASE = "RELEASE"
        SALE = "SALE"

    class Reference(models.TextChoices):
        ORDER = "ORDER"
        QUOTE = "QUOTE"
        RESERVATION = "RESERVATION"
        TRANSFER = "TRANSFER"
        IMPORT = "IMPORT"
        MANUAL = "MANUAL"

    id = uuid7_primary_key()
    variant = models.ForeignKey(
        "catalog.ProductVariant",
        models.PROTECT,
        related_name="+",
        db_constraint=False,
        db_index=False,
    )
    warehouse = models.ForeignKey(
        Warehouse, models.PROTECT, related_name="+", db_constraint=False, db_index=False
    )
    movement_type = models.CharField(max_length=16, choices=Type.choices)
    quantity = models.IntegerField()
    on_hand_after = models.IntegerField()
    reserved_after = models.IntegerField()
    reference_type = models.CharField(
        max_length=16, choices=Reference.choices, default=Reference.MANUAL
    )
    reference_id = models.UUIDField(null=True, blank=True)
    transfer_group_id = models.UUIDField(null=True, blank=True)
    reason = models.CharField(max_length=255)
    idempotency_key = models.CharField(max_length=100)
    created_by_type = models.CharField(max_length=16)  # ActorType (E.0)
    created_by_user_id = models.UUIDField(null=True, blank=True)  # FK compuesta a membresías
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "inventory_movements"
        indexes = [
            models.Index(fields=["organization_id", "variant"], name="inventory_movements_var_idx"),
            models.Index(
                fields=["organization_id", "warehouse"], name="inventory_movements_wh_idx"
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["organization_id", "idempotency_key"], name="inventory_movements_key_uq"
            ),
            models.CheckConstraint(
                condition=models.Q(on_hand_after__gte=0, reserved_after__gte=0),
                name="inventory_movements_after_ck",
            ),
            models.CheckConstraint(  # un ingreso suma, una salida resta
                condition=~models.Q(movement_type="RECEIPT", quantity__lte=0)
                & ~models.Q(movement_type="ISSUE", quantity__gte=0),
                name="inventory_movements_sign_ck",
            ),
            models.CheckConstraint(
                condition=models.Q(created_by_type="USER", created_by_user_id__isnull=False)
                | (
                    models.Q(created_by_type__in=["AI_AGENT", "SYSTEM", "INTEGRATION"])
                    & models.Q(created_by_user_id__isnull=True)
                ),
                name="inventory_movements_actor_ck",
            ),
        ]
