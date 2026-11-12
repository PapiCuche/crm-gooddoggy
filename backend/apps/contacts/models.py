"""Contactos (Fase 4, E05; modelo de datos §E.5). Tablas tenant-owned con FORCE RLS.

El perfil CRM del cliente es de Good Doggy (ADR-028 §1). Lleva datos personales: nada de esto
va a los logs, y la auditoría enmascara el documento (04 §N.1).
"""

import re

from django.db import models
from django.db.models.functions import Lower

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
    is_blocked = models.BooleanField(
        default=False, db_default=False
    )  # no recibe nada proactivo (ADR-010 §2.3)
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


class TagTarget(models.TextChoices):
    """A qué se aplica una etiqueta (§E.5). Las de conversaciones y oportunidades llegan con sus
    fases, con sus propias tablas N:M (`conversation_tags`, `opportunity_tags`)."""

    CONTACT = "CONTACT"


TAG_COLOR = r"^#[0-9a-f]{6}$"  # `#rrggbb`, en minúsculas


class Tag(TenantModel):
    """Etiqueta configurable de la organización (F4-11, §E.5). No se borra: se desactiva, y una
    desactivada sigue en los contactos que ya la llevan, pero no se pone en otros. El nombre es
    único por organización y destino sin distinguir mayúsculas, también entre las inactivas."""

    Target = TagTarget

    id = uuid7_primary_key()
    name = models.CharField(max_length=50)
    color = models.CharField(max_length=7)
    applies_to = models.CharField(max_length=16, choices=TagTarget.choices)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "tags"
        constraints = [
            models.UniqueConstraint(
                "organization_id", "applies_to", Lower("name"), name="tags_org_target_name_uq"
            ),
            models.UniqueConstraint(  # destino de las FK compuestas `tag_id`
                fields=["organization_id", "id"], name="tags_org_id_uq"
            ),
            models.CheckConstraint(
                condition=models.Q(color__regex=TAG_COLOR), name="tags_color_ck"
            ),
            models.CheckConstraint(
                condition=models.Q(applies_to__in=TagTarget.values), name="tags_applies_to_ck"
            ),
        ]

    def __str__(self) -> str:
        return self.name


class ContactTag(TenantModel):
    """Una etiqueta puesta en un contacto (N:M, §E.5). El contacto, la etiqueta y quien la puso
    son de la organización: FKs compuestas en la migración."""

    id = uuid7_primary_key()
    contact = models.ForeignKey(
        Contact, models.PROTECT, related_name="tag_links", db_constraint=False, db_index=False
    )
    tag = models.ForeignKey(
        Tag, models.PROTECT, related_name="contact_links", db_constraint=False, db_index=False
    )
    added_by_user_id = models.UUIDField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "contact_tags"
        constraints = [
            models.UniqueConstraint(  # también lleva a las etiquetas de un contacto
                fields=["organization_id", "contact", "tag"], name="contact_tags_org_contact_tag_uq"
            ),
        ]
        indexes = [  # el filtro `?tag=` de la lista
            models.Index(
                fields=["organization_id", "tag", "contact"], name="contact_tags_org_tag_idx"
            )
        ]

    def __str__(self) -> str:
        return str(self.pk)


class Channel(models.TextChoices):
    """Canal de un consentimiento (§E.5); `ANY` expresa una exclusión o una concesión global."""

    WHATSAPP = "WHATSAPP"
    INSTAGRAM = "INSTAGRAM"
    MESSENGER = "MESSENGER"
    TIKTOK = "TIKTOK"
    EMAIL = "EMAIL"
    SMS = "SMS"
    PHONE = "PHONE"
    ANY = "ANY"


class Purpose(models.TextChoices):
    """Para qué se escribe al cliente; `ALL_PROACTIVE` cubre a todos los que inician el negocio."""

    MARKETING = "MARKETING"
    PROMOTIONS = "PROMOTIONS"
    FOLLOW_UP = "FOLLOW_UP"
    TRANSACTIONAL = "TRANSACTIONAL"
    SURVEYS = "SURVEYS"
    ALL_PROACTIVE = "ALL_PROACTIVE"


class ConsentStatus(models.TextChoices):
    GRANTED = "GRANTED"
    REVOKED = "REVOKED"  # sin fila no hay dato: el consentimiento es desconocido


class ConsentSource(models.TextChoices):
    CHANNEL_OPT_IN = "CHANNEL_OPT_IN"
    CUSTOMER_MESSAGE = "CUSTOMER_MESSAGE"
    WEB_FORM = "WEB_FORM"
    IN_STORE = "IN_STORE"
    IMPORT = "IMPORT"
    AGENT_RECORDED = "AGENT_RECORDED"
    KEYWORD_STOP = "KEYWORD_STOP"  # el cliente escribió una palabra de baja: solo revoca


class ContactConsent(TenantModel):
    """El consentimiento vigente de un contacto por canal y propósito (§E.5, ADR-010): una fila
    por combinación, la proyección rápida con la que decide la política. Lo que cambió y cuándo
    está en `ContactConsentEvent`; aquí `crm_app` solo actualiza el estado, nunca borra."""

    Channel = Channel
    Purpose = Purpose
    Status = ConsentStatus
    Source = ConsentSource

    id = uuid7_primary_key()
    contact = models.ForeignKey(
        Contact, models.PROTECT, related_name="consents", db_constraint=False, db_index=False
    )
    channel = models.CharField(max_length=10, choices=Channel.choices)
    purpose = models.CharField(max_length=13, choices=Purpose.choices)
    status = models.CharField(max_length=7, choices=ConsentStatus.choices)
    source = models.CharField(max_length=17, choices=ConsentSource.choices)
    granted_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    evidence = models.JSONField(default=dict, blank=True)
    recorded_by_user_id = models.UUIDField(null=True, blank=True)  # vacío: el sistema
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "contact_consents"
        constraints = [
            models.UniqueConstraint(  # una fila de estado vigente por combinación
                fields=["organization_id", "contact", "channel", "purpose"],
                name="contact_consents_org_combo_uq",
            ),
            *[
                models.CheckConstraint(condition=models.Q(**{f"{c}__in": v.values}), name=n)
                for c, v, n in (
                    ("channel", Channel, "contact_consents_channel_ck"),
                    ("purpose", Purpose, "contact_consents_purpose_ck"),
                    ("status", ConsentStatus, "contact_consents_status_ck"),
                    ("source", ConsentSource, "contact_consents_source_ck"),
                )
            ],
            models.CheckConstraint(  # el estado dice qué marca de tiempo tiene que existir
                condition=models.Q(status="GRANTED", granted_at__isnull=False)
                | models.Q(status="REVOKED", revoked_at__isnull=False),
                name="contact_consents_stamp_ck",
            ),
            models.CheckConstraint(  # la palabra de baja solo revoca
                condition=~models.Q(source="KEYWORD_STOP") | models.Q(status="REVOKED"),
                name="contact_consents_stop_ck",
            ),
        ]

    def __str__(self) -> str:
        return str(self.pk)


class ContactConsentEvent(TenantModel):
    """Cada concesión o revocación [INM] (§E.5): la prueba ante un reclamo. Nace y se lee; `crm_app`
    no tiene UPDATE ni DELETE. La evidencia lleva ids (mensaje, formulario), nunca el texto."""

    id = uuid7_primary_key()
    contact = models.ForeignKey(
        Contact, models.PROTECT, related_name="+", db_constraint=False, db_index=False
    )
    channel = models.CharField(max_length=10, choices=Channel.choices)
    purpose = models.CharField(max_length=13, choices=Purpose.choices)
    status = models.CharField(max_length=7, choices=ConsentStatus.choices)  # a lo que pasó
    source = models.CharField(max_length=17, choices=ConsentSource.choices)
    evidence = models.JSONField(default=dict, blank=True)
    recorded_by_user_id = models.UUIDField(null=True, blank=True)
    occurred_at = models.DateTimeField()

    class Meta:
        db_table = "contact_consent_events"
        indexes = [
            models.Index(
                fields=["organization_id", "contact", "occurred_at", "id"],
                name="consent_events_org_contact_idx",
            ),
        ]
        constraints = [
            *[
                models.CheckConstraint(condition=models.Q(**{f"{c}__in": v.values}), name=n)
                for c, v, n in (
                    ("channel", Channel, "consent_events_channel_ck"),
                    ("purpose", Purpose, "consent_events_purpose_ck"),
                    ("status", ConsentStatus, "consent_events_status_ck"),
                    ("source", ConsentSource, "consent_events_source_ck"),
                )
            ],
            models.CheckConstraint(
                condition=~models.Q(source="KEYWORD_STOP") | models.Q(status="REVOKED"),
                name="consent_events_stop_ck",
            ),
        ]

    def __str__(self) -> str:
        return str(self.pk)


class KeywordAction(models.TextChoices):
    OPT_OUT = "OPT_OUT"  # baja: revoca lo proactivo del canal por el que llega
    OPT_IN = "OPT_IN"  # alta: lo restablece


class ConsentKeyword(TenantModel):
    """Una palabra de baja o de alta de la organización (F7-08b, D-F7-05), guardada ya normalizada
    (`keywords.normalize`). Sin ninguna fila valen las de `keywords.DEFAULTS`. `crm_app` la lee, la
    inserta y la borra (el comando reemplaza el conjunto entero); nunca la reescribe."""

    Action = KeywordAction

    id = uuid7_primary_key()
    keyword = models.CharField(max_length=30)
    action = models.CharField(max_length=7, choices=KeywordAction.choices)
    created_by_user_id = models.UUIDField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "consent_keywords"
        constraints = [
            models.UniqueConstraint(
                fields=["organization_id", "keyword"], name="consent_keywords_org_keyword_uq"
            ),
            models.CheckConstraint(
                condition=models.Q(action__in=KeywordAction.values),
                name="consent_keywords_action_ck",
            ),
            models.CheckConstraint(  # la forma normalizada: de una a tres palabras ASCII
                condition=models.Q(keyword__regex=r"^[a-z0-9]+( [a-z0-9]+){0,2}$"),
                name="consent_keywords_keyword_ck",
            ),
        ]

    def __str__(self) -> str:
        return str(self.pk)
