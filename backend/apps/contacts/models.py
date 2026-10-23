"""Contactos (Fase 4, E05; modelo de datos §E.5). Tablas tenant-owned con FORCE RLS.

El perfil CRM del cliente es de Good Doggy (ADR-028 §1). Lleva datos personales: nada de esto
va a los logs, y la auditoría enmascara el documento (04 §N.1).
"""

import re

from django.db import models

from core.db.models import TenantModel, uuid7_primary_key

PHONE_E164 = r"^\+[1-9][0-9]{7,14}$"
SEPARATORS = re.compile(r"[\s().-]")  # los que se escriben entre las cifras


def dialed(value: str) -> str:
    """Lo marcado, sin separadores y con el `00` internacional como `+`: lo que se compara con
    E.164 al guardar un teléfono (F4-01) y al buscarlo (F4-07)."""
    digits = SEPARATORS.sub("", value)
    return f"+{digits[2:]}" if digits.startswith("00") else digits


class DocumentType(models.TextChoices):
    DNI = "DNI"
    CE = "CE"
    RUC = "RUC"
    PASSPORT = "PASSPORT"


class Source(models.TextChoices):
    INBOUND_MESSAGE = "INBOUND_MESSAGE"
    WALK_IN = "WALK_IN"
    REFERRAL = "REFERRAL"
    IMPORT = "IMPORT"
    MANUAL = "MANUAL"
    CAMPAIGN = "CAMPAIGN"
    AI = "AI"


class Contact(TenantModel):
    """Perfil central de un cliente. Sin `UNIQUE` sobre teléfono, correo ni documento: los
    duplicados se gestionan, no se impiden a ciegas (familias que comparten número, números
    reciclados), y nunca se fusiona por coincidencias (ADR-028). Las personas (`*_user_id`) son
    miembros de la organización y la sucursal es suya: FKs compuestas en la migración."""

    DocumentType = DocumentType
    Source = Source

    id = uuid7_primary_key()
    display_name = models.CharField(max_length=200)
    first_name = models.CharField(max_length=100, blank=True, default="")
    last_name = models.CharField(max_length=100, blank=True, default="")
    # NULL = sin dato: nunca casa con otro contacto al buscar duplicados (E05-07)
    primary_phone_e164 = models.CharField(max_length=16, null=True, blank=True)  # noqa: DJ001
    primary_email = models.CharField(max_length=254, null=True, blank=True)  # noqa: DJ001
    document_type = models.CharField(  # noqa: DJ001
        max_length=16, choices=DocumentType.choices, null=True, blank=True
    )
    document_number = models.CharField(max_length=20, null=True, blank=True)  # noqa: DJ001
    owner_user_id = models.UUIDField(null=True, blank=True)  # responsable
    branch_id = models.UUIDField(null=True, blank=True)
    source = models.CharField(max_length=24, choices=Source.choices, default=Source.MANUAL)
    created_by_user_id = models.UUIDField(null=True, blank=True)
    updated_by_user_id = models.UUIDField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    deleted_at = models.DateTimeField(null=True, blank=True)
    deleted_by_user_id = models.UUIDField(null=True, blank=True)
    version = models.PositiveIntegerField(default=1)

    class Meta:
        db_table = "contacts"
        indexes = [
            models.Index(  # `varchar_pattern_ops`: también el prefijo de la búsqueda (F4-07)
                fields=["organization_id", "primary_phone_e164"],
                opclasses=["uuid_ops", "varchar_pattern_ops"],
                name="contacts_org_phone_idx",
            ),
            models.Index(
                fields=["organization_id", "owner_user_id"], name="contacts_org_owner_idx"
            ),
            models.Index(fields=["organization_id", "branch_id"], name="contacts_org_branch_idx"),
            models.Index(  # el filtro `OWN` también mira al creador (F4-02)
                fields=["organization_id", "created_by_user_id"], name="contacts_org_created_by_idx"
            ),
        ]
        constraints = [
            models.UniqueConstraint(  # destino de las FK compuestas de las tablas hijas
                fields=["organization_id", "id"], name="contacts_org_id_uq"
            ),
            models.CheckConstraint(
                condition=models.Q(primary_phone_e164__regex=PHONE_E164),
                name="contacts_phone_e164_ck",
            ),
            models.CheckConstraint(  # el número sin tipo, o el tipo sin número, no dicen nada
                condition=models.Q(document_type__isnull=True, document_number__isnull=True)
                | models.Q(
                    document_type__isnull=False,  # sin esto, `NULL IN (…)` dejaría pasar la fila
                    document_type__in=DocumentType.values,
                    document_number__isnull=False,
                ),
                name="contacts_document_ck",
            ),
            models.CheckConstraint(
                condition=models.Q(source__in=Source.values), name="contacts_source_ck"
            ),
            models.CheckConstraint(condition=models.Q(version__gte=1), name="contacts_version_ck"),
        ]

    def __str__(self) -> str:
        return str(self.pk)


class Note(TenantModel):
    """Nota sobre un cliente (§E.5), no del chat. FKs compuestas al contacto y a las membresías."""

    id = uuid7_primary_key()
    contact_id = models.UUIDField()
    body = models.TextField()
    author_user_id = models.UUIDField()
    is_pinned = models.BooleanField(default=False)
    updated_by_user_id = models.UUIDField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    deleted_at = models.DateTimeField(null=True, blank=True)
    deleted_by_user_id = models.UUIDField(null=True, blank=True)
    version = models.PositiveIntegerField(default=1)

    class Meta:
        db_table = "notes"
        indexes = [
            models.Index(  # el listado de un contacto, por `id` (ADR-016)
                fields=["organization_id", "contact_id", "id"],
                name="notes_org_contact_idx",
                condition=models.Q(deleted_at__isnull=True),
            ),
        ]
        constraints = [
            models.CheckConstraint(condition=models.Q(version__gte=1), name="notes_version_ck"),
        ]

    def __str__(self) -> str:
        return str(self.pk)
