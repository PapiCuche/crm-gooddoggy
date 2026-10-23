"""Importaciones de datos por archivo (modelo de datos §E.12, ADR-028 §5). Tablas tenant-owned
con FORCE RLS y genéricas: cada módulo valida y aplica su `kind` (el stock, `apps.inventory`).

El archivo no se guarda: sus filas viven en `data_import_rows` (solo las columnas conocidas) y
de él quedan el nombre y el sha256, con el que se avisa de un archivo ya subido."""

from django.db import models

from core.db.models import TenantModel, uuid7_primary_key


class DataImport(TenantModel):
    class Kind(models.TextChoices):
        CATALOG = "CATALOG"
        PRICES = "PRICES"
        STOCK = "STOCK"
        CONTACTS = "CONTACTS"

    class Status(models.TextChoices):  # UPLOADED/VALIDATING quedan para un proceso en segundo plano
        UPLOADED = "UPLOADED"
        VALIDATING = "VALIDATING"
        VALIDATED = "VALIDATED"
        APPLYING = "APPLYING"
        APPLIED = "APPLIED"
        FAILED = "FAILED"
        CANCELLED = "CANCELLED"

    id = uuid7_primary_key()
    kind = models.CharField(max_length=16, choices=Kind.choices)
    status = models.CharField(max_length=16, choices=Status.choices)
    file_name = models.CharField(max_length=255)
    content_sha256 = models.CharField(max_length=64)
    total_rows = models.IntegerField(default=0)
    valid_rows = models.IntegerField(default=0)
    error_rows = models.IntegerField(default=0)
    new_count = models.IntegerField(default=0)
    update_count = models.IntegerField(default=0)
    summary = models.JSONField(default=dict)  # encoding, separador, columnas ignoradas, resultado
    created_by_user_id = models.UUIDField()  # FKs compuestas a las membresías (migración)
    applied_by_user_id = models.UUIDField(null=True, blank=True)
    applied_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "data_imports"
        indexes = [
            models.Index(
                fields=["organization_id", "kind", "content_sha256"],
                name="data_imports_org_sha_idx",
            )
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["organization_id", "id"], name="data_imports_org_id_uq"
            ),
            models.CheckConstraint(
                condition=models.Q(kind__in=["CATALOG", "PRICES", "STOCK", "CONTACTS"]),
                name="data_imports_kind_ck",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    status__in=[
                        "UPLOADED",
                        "VALIDATING",
                        "VALIDATED",
                        "APPLYING",
                        "APPLIED",
                        "FAILED",
                        "CANCELLED",
                    ]
                ),
                name="data_imports_status_ck",
            ),
        ]


class DataImportRow(TenantModel):
    """Una fila del archivo: `raw`, las celdas de las columnas conocidas tal como llegaron;
    `normalized`, lo que se aplicaría; `action`, lo que la validación (o la aplicación) decidió.
    `STALE`: la celda cambió entre la previsualización y la aplicación (ADR-028 §5)."""

    class Action(models.TextChoices):
        CREATE = "CREATE"
        UPDATE = "UPDATE"
        UNCHANGED = "UNCHANGED"
        SKIP = "SKIP"
        ERROR = "ERROR"
        STALE = "STALE"

    id = uuid7_primary_key()
    data_import = models.ForeignKey(  # misma organización: FK compuesta en la migración
        DataImport, models.CASCADE, related_name="rows", db_constraint=False, db_index=False
    )
    row_number = models.IntegerField()  # la línea del archivo (la cabecera es la 1)
    raw = models.JSONField()
    normalized = models.JSONField(default=dict)
    action = models.CharField(max_length=16, choices=Action.choices)
    errors = models.JSONField(default=list)
    target_entity_type = models.CharField(max_length=40, blank=True)
    target_entity_id = models.UUIDField(null=True, blank=True)

    class Meta:
        db_table = "data_import_rows"
        constraints = [
            models.UniqueConstraint(
                fields=["organization_id", "data_import", "row_number"],
                name="data_import_rows_line_uq",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    action__in=["CREATE", "UPDATE", "UNCHANGED", "SKIP", "ERROR", "STALE"]
                ),
                name="data_import_rows_action_ck",
            ),
        ]
