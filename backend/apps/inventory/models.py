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
