"""Canales y conversaciones del Inbox (Fase 6, E06; modelo de datos §E.6, ADR-007, 010).
Tablas tenant-owned con FORCE RLS y FKs compuestas en la migración. Nada se borra: una cuenta se
marca borrada, una conversación se cierra o se archiva, el historial de asignaciones es inmutable
(los mensajes llegan en F6-01b)."""

from django.db import models

from core.db.models import TenantModel, uuid7_primary_key

NUMBER = r"^CONV-[0-9]{5,}$"  # `CONV-00191`; pasado el millón crece (core.sequences)


class Platform(models.TextChoices):
    SANDBOX = "SANDBOX"  # el canal de pruebas; WhatsApp, Instagram… llegan con su adapter


class AccountStatus(models.TextChoices):
    PENDING = "PENDING"
    CONNECTED = "CONNECTED"
    DEGRADED = "DEGRADED"
    DISCONNECTED = "DISCONNECTED"
    ERROR = "ERROR"


class Status(models.TextChoices):
    OPEN = "OPEN"
    PENDING = "PENDING"  # en cola, sin tomar por un humano (D-INB-1)
    WAITING_CUSTOMER = "WAITING_CUSTOMER"
    WAITING_INTERNAL = "WAITING_INTERNAL"
    RESOLVED = "RESOLVED"
    CLOSED = "CLOSED"  # terminal: un mensaje posterior abre otra conversación


class AttentionMode(models.TextChoices):
    HUMAN = "HUMAN"
    AI_ASSISTED = "AI_ASSISTED"  # los valores de IA existen; sus invariantes llegan con la Fase 8
    AI_AUTONOMOUS = "AI_AUTONOMOUS"


class Reason(models.TextChoices):  # ADR-007 §2
    MANUAL = "MANUAL"
    TAKE = "TAKE"
    TRANSFER = "TRANSFER"
    ROUND_ROBIN = "ROUND_ROBIN"
    LOAD_BALANCE = "LOAD_BALANCE"
    SKILL_RULE = "SKILL_RULE"
    AI_ROUTING = "AI_ROUTING"
    HANDOFF = "HANDOFF"
    SUPERVISOR_TAKEOVER = "SUPERVISOR_TAKEOVER"
    USER_DEACTIVATED = "USER_DEACTIVATED"
    AI_DISABLED = "AI_DISABLED"
    REOPEN = "REOPEN"


def one_of(field: str, choices: type[models.TextChoices], name: str) -> models.CheckConstraint:
    return models.CheckConstraint(condition=models.Q(**{f"{field}__in": choices.values}), name=name)


class ChannelAccount(TenantModel):
    """Una cuenta de un canal (un número, una página). `external_account_id` es único entre las
    vivas de toda la plataforma: el webhook resuelve la organización con él (§E.6)."""

    Platform = Platform
    Status = AccountStatus

    id = uuid7_primary_key()
    platform = models.CharField(max_length=16, choices=Platform.choices)
    name = models.CharField(max_length=100)
    external_account_id = models.CharField(max_length=100)
    display_identifier = models.CharField(max_length=100, blank=True, default="")
    status = models.CharField(max_length=16, choices=AccountStatus.choices, default="CONNECTED")
    created_by_user_id = models.UUIDField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    deleted_at = models.DateTimeField(null=True, blank=True)
    deleted_by_user_id = models.UUIDField(null=True, blank=True)
    version = models.PositiveIntegerField(default=1)

    class Meta:
        db_table = "channel_accounts"
        constraints = [
            models.UniqueConstraint(  # destino de las FK compuestas
                fields=["organization_id", "id"], name="channel_accounts_org_id_uq"
            ),
            models.UniqueConstraint(  # global, no por organización
                fields=["platform", "external_account_id"],
                condition=models.Q(deleted_at__isnull=True),
                name="channel_accounts_external_uq",
            ),
            one_of("platform", Platform, "channel_accounts_platform_ck"),
            one_of("status", AccountStatus, "channel_accounts_status_ck"),
            models.CheckConstraint(
                condition=models.Q(name__regex=r"\S", external_account_id__regex=r"\S"),
                name="channel_accounts_text_ck",
            ),
            models.CheckConstraint(
                condition=models.Q(version__gte=1), name="channel_accounts_version_ck"
            ),
        ]

    def __str__(self) -> str:
        return str(self.pk)


class Conversation(TenantModel):
    """Un hilo de un contacto en una cuenta de canal. Tres ejes (§I): `status`, quién atiende
    (equipo y persona a la vez, ADR-007) y `attention_mode`. La asignación solo la escribirá
    `AssignmentService` (F6-05). A lo más una conversación no cerrada por contacto y cuenta."""

    Status = Status
    AttentionMode = AttentionMode

    id = uuid7_primary_key()
    number = models.CharField(max_length=20)
    contact = models.ForeignKey(
        "contacts.Contact", models.PROTECT, related_name="+", db_constraint=False, db_index=False
    )
    channel_account = models.ForeignKey(
        ChannelAccount, models.PROTECT, related_name="+", db_constraint=False, db_index=False
    )
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    attention_mode = models.CharField(
        max_length=16, choices=AttentionMode.choices, default=AttentionMode.HUMAN
    )
    assigned_team_id = models.UUIDField(null=True, blank=True)
    assigned_user_id = models.UUIDField(null=True, blank=True)
    assignee_kind = models.GeneratedField(  # ADR-007: se indexa; sin agente IA hasta la Fase 8
        expression=models.Case(
            models.When(assigned_user_id__isnull=False, then=models.Value("USER")),
            models.When(assigned_team_id__isnull=False, then=models.Value("TEAM")),
            default=models.Value("UNASSIGNED"),
            output_field=models.CharField(max_length=10),
        ),
        output_field=models.CharField(max_length=10),
        db_persist=True,
    )
    assignment_version = models.PositiveIntegerField(default=1)
    assigned_at = models.DateTimeField(null=True, blank=True)
    last_inbound_at = models.DateTimeField(
        null=True, blank=True
    )  # la ventana de ADR-010 sale de aquí
    last_message_at = models.DateTimeField()
    resolved_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    version = models.PositiveIntegerField(default=1)

    class Meta:
        db_table = "conversations"
        indexes = [
            models.Index(
                fields=["organization_id", "status", "-last_message_at"],
                name="conversations_org_status_idx",
            ),
            models.Index(
                fields=["organization_id", "assigned_user_id", "status"],
                name="conversations_org_user_idx",
            ),
            models.Index(
                fields=["organization_id", "assigned_team_id", "status"],
                name="conversations_org_team_idx",
            ),
            models.Index(
                fields=["organization_id", "contact"], name="conversations_org_contact_idx"
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["organization_id", "id"], name="conversations_org_id_uq"
            ),
            models.UniqueConstraint(
                fields=["organization_id", "number"], name="conversations_number_uq"
            ),
            models.UniqueConstraint(  # evita duplicados por mensajes entrantes a la vez
                fields=["organization_id", "contact", "channel_account"],
                condition=~models.Q(status="CLOSED"),
                name="conversations_open_uq",
            ),
            models.CheckConstraint(
                condition=models.Q(number__regex=NUMBER), name="conversations_number_ck"
            ),
            one_of("status", Status, "conversations_status_ck"),
            one_of("attention_mode", AttentionMode, "conversations_attention_ck"),
            models.CheckConstraint(  # `closed_at` y el estado CLOSED van juntos
                condition=models.Q(status="CLOSED", closed_at__isnull=False)
                | (~models.Q(status="CLOSED") & models.Q(closed_at__isnull=True)),
                name="conversations_closed_ck",
            ),
            models.CheckConstraint(
                condition=models.Q(assignment_version__gte=1, version__gte=1),
                name="conversations_version_ck",
            ),
        ]

    def __str__(self) -> str:
        return str(self.pk)


class ConversationAssignment(TenantModel):
    """Cada cambio de asignación [INM] (ADR-007 §2): `crm_app` solo inserta y lee. Sin agente IA
    hasta la Fase 8: lo hace una persona o el sistema, y exactamente una de las dos."""

    Reason = Reason

    id = uuid7_primary_key()
    conversation = models.ForeignKey(
        Conversation,
        models.PROTECT,
        related_name="assignments",
        db_constraint=False,
        db_index=False,
    )
    previous_team_id = models.UUIDField(null=True, blank=True)
    previous_user_id = models.UUIDField(null=True, blank=True)
    new_team_id = models.UUIDField(null=True, blank=True)
    new_user_id = models.UUIDField(null=True, blank=True)
    reason_code = models.CharField(max_length=24, choices=Reason.choices)
    reason_note = models.CharField(max_length=1000, null=True, blank=True)  # noqa: DJ001
    internal_comment = models.CharField(max_length=1000, null=True, blank=True)  # noqa: DJ001
    assigned_by_user_id = models.UUIDField(null=True, blank=True)
    assigned_by_system = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "conversation_assignments"
        indexes = [
            models.Index(
                fields=["organization_id", "conversation", "created_at", "id"],
                name="conv_assignments_org_conv_idx",
            ),
        ]
        constraints = [
            one_of("reason_code", Reason, "conv_assignments_reason_ck"),
            models.CheckConstraint(  # exactamente quien lo hizo: una persona o el sistema
                condition=models.Q(assigned_by_user_id__isnull=False, assigned_by_system=False)
                | models.Q(assigned_by_user_id__isnull=True, assigned_by_system=True),
                name="conv_assignments_actor_ck",
            ),
        ]

    def __str__(self) -> str:
        return str(self.pk)
