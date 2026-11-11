"""Canales, conversaciones y mensajes del Inbox (Fase 6, E06; modelo de datos §E.6, ADR-007, 010).
Tablas tenant-owned con FORCE RLS y FKs compuestas en la migración. Nada se borra: una cuenta se
marca borrada, una conversación se cierra o se archiva, el historial de asignaciones es inmutable
y un mensaje no se reescribe salvo el estado de entrega, que solo avanza."""

from django.db import models
from django.db.models.functions import Length, Now
from django.db.models.lookups import LessThanOrEqual

from core.db.models import TenantModel, uuid7_primary_key

NUMBER = r"^CONV-[0-9]{5,}$"  # `CONV-00191`; pasado el millón crece (core.sequences)
BODY_MAX = 4096
PREVIEW_MAX = 80  # la vista previa de la lista: texto plano, sin saltos
SENDERS = ("USER", "AI_AGENT", "SYSTEM")  # los que pueden escribir una nota interna


class Platform(models.TextChoices):
    SANDBOX = "SANDBOX"  # el canal de pruebas
    WHATSAPP = (
        "WHATSAPP"  # la Cloud API oficial de Meta (ADR-029); Instagram… llegan con su adapter
    )


AccountStatus: type[models.TextChoices] = models.TextChoices(  # type: ignore[assignment]
    "AccountStatus", "PENDING CONNECTED DEGRADED DISCONNECTED ERROR"
)


# Por qué una cuenta real no está bien (ADR-029 §2): un código de un conjunto cerrado, nunca el
# texto de un error de Meta.
STATUS_DETAILS = (
    "TOKEN_INVALID", "WEBHOOK_NOT_SEEN", "QUALITY_LOW", "LIMIT_REACHED", "DISCONNECTED_BY_ADMIN",
    "PROVIDER_ERROR",
)  # fmt: skip
QUALITY_RATINGS = ("GREEN", "YELLOW", "RED", "UNKNOWN")  # a confirmar con la documentación de Meta
LIMIT_TIERS = (  # a confirmar con la documentación de Meta
    "TIER_50", "TIER_250", "TIER_1K", "TIER_10K", "TIER_100K", "TIER_UNLIMITED", "TIER_NOT_SET",
)  # fmt: skip
META_ID = r"^[0-9]{5,30}$"  # `phone_number_id` y WABA: dígitos (a confirmar)


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


class Direction(models.TextChoices):
    INBOUND = "INBOUND"
    OUTBOUND = "OUTBOUND"
    INTERNAL = "INTERNAL"


class SenderType(models.TextChoices):
    CONTACT = "CONTACT"
    USER = "USER"
    AI_AGENT = "AI_AGENT"
    SYSTEM = "SYSTEM"


class MessageType(models.TextChoices):
    TEXT = "TEXT"
    NOTE = "NOTE"  # solo INTERNAL: nunca sale hacia un canal (ADR-010)
    UNSUPPORTED = (
        "UNSUPPORTED"  # entrante que el canal aún no entiende: el cuerpo es una frase fija
    )


class Delivery(models.TextChoices):
    QUEUED = "QUEUED"
    SENDING = "SENDING"  # reclamado: la llamada al canal está en curso
    SENT = "SENT"
    DELIVERED = "DELIVERED"
    READ = "READ"
    FAILED = "FAILED"  # terminal


Reason: type[models.TextChoices] = models.TextChoices(  # type: ignore[assignment]  # ADR-007 §2
    "Reason",
    "MANUAL TAKE TRANSFER ROUND_ROBIN LOAD_BALANCE SKILL_RULE AI_ROUTING HANDOFF "
    "SUPERVISOR_TAKEOVER USER_DEACTIVATED AI_DISABLED REOPEN",
)


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
    # Solo las cuentas reales (WhatsApp, ADR-029 §2): el WABA, la credencial (FK compuesta en la
    # base, F7-02b), el motivo corto del estado y los datos de salud que informa Meta.
    external_parent_id = models.CharField(max_length=100, null=True, blank=True)  # noqa: DJ001
    credential_id = models.UUIDField(null=True, blank=True)  # FK compuesta a `credentials` (0006)
    status_detail = models.CharField(max_length=24, null=True, blank=True)  # noqa: DJ001
    quality_rating = models.CharField(max_length=10, null=True, blank=True)  # noqa: DJ001
    messaging_limit_tier = models.CharField(max_length=16, null=True, blank=True)  # noqa: DJ001
    last_healthcheck_at = models.DateTimeField(null=True, blank=True)
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
            models.CheckConstraint(  # una cuenta de WhatsApp es un número de un WABA
                condition=~models.Q(platform="WHATSAPP")
                | (
                    models.Q(external_account_id__regex=META_ID)
                    & models.Q(external_parent_id__isnull=False)  # un NULL pasaría el regex
                    & models.Q(external_parent_id__regex=META_ID)
                ),
                name="channel_accounts_whatsapp_ck",
            ),
            models.CheckConstraint(  # el Sandbox no tiene WABA, credencial ni salud
                condition=~models.Q(platform="SANDBOX")
                | models.Q(
                    external_parent_id__isnull=True,
                    credential_id__isnull=True,
                    status_detail__isnull=True,
                    quality_rating__isnull=True,
                    messaging_limit_tier__isnull=True,
                    last_healthcheck_at__isnull=True,
                ),
                name="channel_accounts_sandbox_ck",
            ),
            models.CheckConstraint(  # una cuenta real solo está CONNECTED con credencial verificada
                condition=models.Q(platform="SANDBOX")
                | ~models.Q(status="CONNECTED")
                | models.Q(credential_id__isnull=False, last_healthcheck_at__isnull=False),
                name="channel_accounts_connected_ck",
            ),
            models.CheckConstraint(
                condition=models.Q(status_detail__isnull=True)
                | models.Q(status_detail__in=STATUS_DETAILS),
                name="channel_accounts_detail_ck",
            ),
            models.CheckConstraint(
                condition=models.Q(quality_rating__isnull=True)
                | models.Q(quality_rating__in=QUALITY_RATINGS),
                name="channel_accounts_quality_ck",
            ),
            models.CheckConstraint(
                condition=models.Q(messaging_limit_tier__isnull=True)
                | models.Q(messaging_limit_tier__in=LIMIT_TIERS),
                name="channel_accounts_tier_ck",
            ),
        ]

    def __str__(self) -> str:
        return str(self.pk)


# Lo que mide un valor de `core.sealed` (ADR-022 §3): de 32 a 4127 bytes y el formato 1 al frente.
SEALED_MIN, SEALED_MAX, SEALED_FORMAT = 32, 4127, b"\x01"
TOKEN_PURPOSE = "channel.access_token"  # noqa: S105 — el propósito de `core.sealed`, no un secreto
TOKEN_FORMAT = r"^[A-Za-z0-9_.-]{20,512}$"  # noqa: S105 — el token de acceso de Meta (a confirmar)


class Credential(TenantModel):
    """El token de acceso de una cuenta de canal, cifrado con `core.sealed` (ADR-022, ADR-029 §2;
    propósito `channel.access_token`, dueño `channel_account:<owner_id>`). Una credencial viva por
    dueño. Al rotarla o desconectar la cuenta se **invalida**: `revoked_at` y el cifrado, borrado.
    Nada se borra ni se reescribe salvo `ciphertext`, `key_version` y `revoked_at` (UPDATE por
    columna). `last_four` no abre nada (no hay huella: F7-02c la quitó, ver ADR-029)."""

    id = uuid7_primary_key()
    owner_type = models.CharField(max_length=16, default="CHANNEL_ACCOUNT")
    owner_id = models.UUIDField()
    ciphertext = models.BinaryField(null=True)
    key_version = models.PositiveSmallIntegerField()
    last_four = models.CharField(max_length=4)
    expires_at = models.DateTimeField(null=True, blank=True)
    rotated_at = models.DateTimeField(null=True, blank=True)  # cuándo sustituyó a la anterior
    revoked_at = models.DateTimeField(null=True, blank=True)
    created_by_user_id = models.UUIDField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "credentials"
        constraints = [
            models.UniqueConstraint(  # destino de la FK compuesta de la cuenta
                fields=["organization_id", "id"], name="credentials_org_id_uq"
            ),
            models.UniqueConstraint(  # una viva por dueño
                fields=["organization_id", "owner_type", "owner_id"],
                condition=models.Q(revoked_at__isnull=True),
                name="credentials_live_owner_uq",
            ),
            models.CheckConstraint(
                condition=models.Q(owner_type="CHANNEL_ACCOUNT"), name="credentials_owner_ck"
            ),
            models.CheckConstraint(  # viva: cifrada con el formato vigente; invalidada: sin cifrado
                condition=models.Q(
                    models.Q(revoked_at__isnull=True, ciphertext__isnull=False)
                    & models.Q(models.lookups.GreaterThanOrEqual(Length("ciphertext"), SEALED_MIN))
                    & models.Q(models.lookups.LessThanOrEqual(Length("ciphertext"), SEALED_MAX))
                    & models.Q(
                        models.lookups.Exact(
                            models.Func(
                                "ciphertext",
                                1,
                                1,
                                function="substring",
                                output_field=models.BinaryField(),
                            ),
                            models.Value(SEALED_FORMAT, output_field=models.BinaryField()),
                        )
                    )
                    | models.Q(revoked_at__isnull=False, ciphertext__isnull=True)
                ),
                name="credentials_ciphertext_ck",
            ),
            models.CheckConstraint(
                condition=models.Q(key_version__gte=1, last_four__regex=r"^.{4}$"),
                name="credentials_shape_ck",
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
    # Para la lista (F6-02): lo último que dijo el cliente o el equipo, nunca una nota interna.
    last_message_preview = models.CharField(max_length=PREVIEW_MAX, null=True, blank=True)  # noqa: DJ001
    last_message_direction = models.CharField(  # noqa: DJ001
        max_length=8,
        null=True,
        blank=True,
        choices=[("INBOUND", "Inbound"), ("OUTBOUND", "Outbound")],
    )
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
            models.Index(  # la lista del Inbox: lo más reciente primero (F6-02)
                fields=["organization_id", "-last_message_at", "-id"],
                name="conversations_org_recent_idx",
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["organization_id", "id"], name="conversations_org_id_uq"
            ),
            models.UniqueConstraint(  # destino de la FK de los mensajes: la cuenta es la del hilo
                fields=["organization_id", "id", "channel_account"],
                name="conversations_org_account_uq",
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
            models.CheckConstraint(  # RESOLVED lleva su fecha: la reapertura de 72 h parte de ella
                condition=~models.Q(status="RESOLVED") | models.Q(resolved_at__isnull=False),
                name="conversations_resolved_ck",
            ),
            models.CheckConstraint(
                condition=models.Q(assignment_version__gte=1, version__gte=1),
                name="conversations_version_ck",
            ),
            models.CheckConstraint(  # la vista previa y su dirección van juntas
                condition=models.Q(
                    last_message_preview__isnull=True, last_message_direction__isnull=True
                )
                | models.Q(
                    last_message_preview__isnull=False,
                    last_message_direction__isnull=False,  # un NULL pasaría el `IN` de un CHECK
                    last_message_direction__in=["INBOUND", "OUTBOUND"],
                ),
                name="conversations_preview_ck",
            ),
        ]

    def __str__(self) -> str:
        return str(self.pk)


class Message(TenantModel):
    """Todo lo que aparece en el chat. Una nota (`INTERNAL`) no puede tener id externo, ni ser de
    un contacto, ni salir: la base lo impide (ADR-010, primera de las tres guardas). Solo
    `delivery_status` cambia, y solo avanza (disparador de la migración)."""

    Direction = Direction
    Delivery = Delivery

    id = uuid7_primary_key()
    conversation = models.ForeignKey(
        Conversation, models.PROTECT, related_name="messages", db_constraint=False, db_index=False
    )
    channel_account = models.ForeignKey(  # la de la conversación: la FK compuesta lo hace cumplir
        ChannelAccount, models.PROTECT, related_name="+", db_constraint=False, db_index=False
    )
    direction = models.CharField(max_length=8, choices=Direction.choices)
    sender_type = models.CharField(max_length=8, choices=SenderType.choices)
    sender_user_id = models.UUIDField(null=True, blank=True)
    message_type = models.CharField(max_length=16, choices=MessageType.choices, default="TEXT")
    body = models.TextField()
    client_message_id = models.UUIDField(null=True, blank=True)
    external_message_id = models.CharField(max_length=200, null=True, blank=True)  # noqa: DJ001
    # Lo que informa el canal (acotado a «ahora»): ancla de la ventana de 24 h (ADR-029 §5).
    provider_timestamp = models.DateTimeField(null=True, blank=True)
    delivery_status = models.CharField(  # noqa: DJ001
        max_length=10, choices=Delivery.choices, null=True, blank=True
    )
    sending_at = models.DateTimeField(null=True, blank=True)  # cuándo se reclamó (el barredor)
    sent_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    read_at = models.DateTimeField(null=True, blank=True)
    failed_at = models.DateTimeField(null=True, blank=True)
    # La decisión de la política (ADR-010) al encolar y tras la última revaluación: solo la
    # instantánea de `PolicyDecision.as_json()` (permitido, modo, motivo y horas), nunca texto.
    policy_decision = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "messages"
        indexes = [
            models.Index(
                fields=["organization_id", "conversation", "created_at", "id"],
                name="messages_org_conversation_idx",
            ),
            models.Index(  # el barredor de F6-04b: solo lo que sigue en cola (pocas filas)
                fields=["organization_id", "id"],
                condition=models.Q(delivery_status="QUEUED"),
                name="messages_queued_idx",
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["organization_id", "channel_account", "external_message_id"],
                condition=models.Q(external_message_id__isnull=False),
                name="messages_external_uq",
            ),
            models.UniqueConstraint(
                fields=["organization_id", "client_message_id"],
                condition=models.Q(client_message_id__isnull=False),
                name="messages_client_uq",
            ),
            one_of("direction", Direction, "messages_direction_ck"),
            one_of("sender_type", SenderType, "messages_sender_ck"),
            one_of("message_type", MessageType, "messages_type_ck"),
            models.CheckConstraint(  # «no soportado» solo lo escribe un cliente
                condition=~models.Q(message_type="UNSUPPORTED") | models.Q(direction="INBOUND"),
                name="messages_unsupported_ck",
            ),
            models.CheckConstraint(  # sin id externo ni remitente cliente: no sale por ningún canal
                condition=~models.Q(direction="INTERNAL")
                | (models.Q(external_message_id__isnull=True, sender_type__in=SENDERS)),
                name="messages_internal_ck",
            ),
            models.CheckConstraint(  # una nota es interna, y lo interno es una nota
                condition=models.Q(message_type="NOTE", direction="INTERNAL")
                | (~models.Q(message_type="NOTE") & ~models.Q(direction="INTERNAL")),
                name="messages_note_ck",
            ),
            models.CheckConstraint(  # el cliente solo escribe lo entrante; un usuario, su `sender`
                condition=models.Q(direction="INBOUND", sender_type="CONTACT")
                | (~models.Q(direction="INBOUND") & ~models.Q(sender_type="CONTACT")),
                name="messages_contact_ck",
            ),
            models.CheckConstraint(
                condition=models.Q(sender_type="USER", sender_user_id__isnull=False)
                | (~models.Q(sender_type="USER") & models.Q(sender_user_id__isnull=True)),
                name="messages_sender_user_ck",
            ),
            models.CheckConstraint(  # solo lo saliente se entrega, y siempre tiene estado
                condition=models.Q(direction="OUTBOUND", delivery_status__isnull=False)
                | (~models.Q(direction="OUTBOUND") & models.Q(delivery_status__isnull=True)),
                name="messages_delivery_ck",
            ),
            models.CheckConstraint(
                condition=models.Q(delivery_status__isnull=True)
                | models.Q(delivery_status__in=Delivery.values),
                name="messages_delivery_values_ck",
            ),
            models.CheckConstraint(
                condition=models.Q(body__regex=r"\S")
                & models.Q(LessThanOrEqual(Length("body"), BODY_MAX)),
                name="messages_body_ck",
            ),
            models.CheckConstraint(  # la decisión de la política es solo de lo saliente
                condition=models.Q(policy_decision__isnull=True) | models.Q(direction="OUTBOUND"),
                name="messages_policy_ck",
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


INGRESS_STATUSES = ("RECEIVED", "PROCESSING", "PROCESSED", "IGNORED", "FAILED", "DEAD")
INGRESS_MAX_ATTEMPTS = 8  # tras 8 intentos un ítem queda `DEAD` (ADR-029 §3)


class WebhookIngress(models.Model):
    """Un ítem lógico de un webhook firmado (F7-03b; ADR-029 §3). **De plataforma**: sin
    organización hasta que el enrutado la encuentra; RLS activada **sin política** y sin ningún
    privilegio de `crm_app`, que solo entra por las funciones `webhook_ingress_*`. El payload se
    vacía al pasar a `PROCESSED` o `IGNORED` (queda su hash); `routed_organization_id` y
    `routed_channel_account_id` los pone el enrutado y son informativos (no se llaman
    `organization_id`: no es una tabla de tenant)."""

    id = uuid7_primary_key()
    provider = models.CharField(max_length=16)
    event_key = models.CharField(max_length=700)
    kind = models.CharField(max_length=16)
    waba_id = models.CharField(max_length=30)
    phone_number_id = models.CharField(max_length=30, null=True)  # noqa: DJ001
    status = models.CharField(max_length=16, db_default="RECEIVED")
    payload = models.JSONField(null=True)
    payload_hash = models.CharField(max_length=64)
    attempts = models.PositiveSmallIntegerField(db_default=0)
    next_attempt_at = models.DateTimeField(db_default=Now())
    claimed_until = models.DateTimeField(null=True)
    ignored_reason = models.CharField(max_length=32, null=True)  # noqa: DJ001
    routed_organization_id = models.UUIDField(null=True)
    routed_channel_account_id = models.UUIDField(null=True)
    received_at = models.DateTimeField(db_default=Now())
    processed_at = models.DateTimeField(null=True)

    class Meta:
        db_table = "webhook_ingress"
        constraints = [
            models.UniqueConstraint(
                fields=["provider", "event_key"], name="webhook_ingress_key_uq"
            ),
            models.CheckConstraint(
                condition=models.Q(status__in=INGRESS_STATUSES), name="webhook_ingress_status_ck"
            ),
            models.CheckConstraint(  # vaciado al terminar, y solo entonces
                condition=models.Q(payload__isnull=True)
                | ~models.Q(status__in=("PROCESSED", "IGNORED")),
                name="webhook_ingress_emptied_ck",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    payload_hash__regex=r"^[0-9a-f]{64}$", attempts__lte=INGRESS_MAX_ATTEMPTS
                ),
                name="webhook_ingress_shape_ck",
            ),
        ]
        indexes = [
            models.Index(
                fields=["next_attempt_at"],
                condition=models.Q(status__in=("RECEIVED", "FAILED")),
                name="webhook_ingress_due_idx",
            ),
            models.Index(fields=["received_at"], name="webhook_ingress_received_idx"),
        ]

    def __str__(self) -> str:
        return self.event_key


class WebhookFailure(models.Model):
    """Un intento fallido de procesar un ítem: solo un código corto, nunca el error ni el cuerpo."""

    id = uuid7_primary_key()
    ingress = models.ForeignKey(
        WebhookIngress, models.CASCADE, related_name="+", db_constraint=True
    )
    attempt = models.PositiveSmallIntegerField()
    reason = models.CharField(max_length=32)
    created_at = models.DateTimeField(db_default=Now())

    class Meta:
        db_table = "webhook_failures"

    def __str__(self) -> str:
        return str(self.ingress_id)
