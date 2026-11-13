"""Metadatos de objetos de una organización (ADR-008 §2); los bytes viven en el storage."""

from django.db import models

from core.db.models import TenantModel, uuid7_primary_key


class File(TenantModel):
    class Purpose(models.TextChoices):
        MESSAGE_MEDIA = "message-media"
        MESSAGE_MEDIA_OUT = "message-media-out"  # F7-09b2: la que sube una persona para enviar
        QUOTE_PDF = "quote-pdf"
        IMPORT = "import"
        AVATAR = "avatar"
        KB = "kb"
        ATTACHMENT = "attachment"

    class ScanStatus(models.TextChoices):
        PENDING = "PENDING"
        CLEAN = "CLEAN"
        INFECTED = "INFECTED"

    id = uuid7_primary_key()
    storage_key = models.CharField(max_length=300)
    original_name = models.CharField(max_length=255)
    mime_type = models.CharField(max_length=100)
    size_bytes = models.PositiveBigIntegerField()
    sha256 = models.CharField(max_length=64)
    purpose = models.CharField(max_length=20, choices=Purpose.choices)
    scan_status = models.CharField(max_length=10, choices=ScanStatus.choices, default="PENDING")
    created_by_user_id = models.UUIDField(null=True)  # FK compuesta a membresías: Fase 2
    created_at = models.DateTimeField(
        db_default=models.Func(function="now", output_field=models.DateTimeField())
    )

    class Meta:
        db_table = "files"
        constraints = [
            models.UniqueConstraint(fields=["organization_id", "storage_key"], name="files_key_uq"),
            models.UniqueConstraint(  # destino de las FK compuestas (F7-09a1b, migración 0002)
                fields=["organization_id", "id"], name="files_org_id_uq"
            ),
        ]
