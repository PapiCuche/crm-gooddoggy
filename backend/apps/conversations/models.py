"""Canales, conversaciones y mensajes del Inbox (Fase 6, E06; modelo de datos §E.6, ADR-007, 010).
Tablas tenant-owned con FORCE RLS y FKs compuestas en la migración. Nada se borra: una cuenta se
marca borrada, una conversación se cierra o se archiva, el historial de asignaciones es inmutable
y un mensaje no se reescribe salvo el estado de entrega, que solo avanza."""

import datetime as dt

from django.conf import settings
from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields import RangeOperators
from django.db import models
from django.db.models.functions import Coalesce, Length, Lower, Now
from django.db.models.lookups import LessThanOrEqual
from django.utils import timezone

from core.db.models import TenantModel, uuid7_primary_key

NUMBER = r"^CONV-[0-9]{5,}$"  # `CONV-00191`; pasado el millón crece (core.sequences)
BODY_MAX = 4096
PREVIEW_MAX = 80  # la vista previa de la lista: texto plano, sin saltos
SENDERS = ("USER", "AI_AGENT", "SYSTEM")  # los que pueden escribir una nota interna


class Platform(models.TextChoices):
    SANDBOX = "SANDBOX"  # el canal de pruebas
    WHATSAPP = "WHATSAPP"  # la Cloud API oficial de Meta (ADR-029)
    MESSENGER = "MESSENGER"  # una página de Facebook (ADR-035 §3)
    INSTAGRAM = "INSTAGRAM"  # una cuenta profesional de Instagram vinculada a su página


SOCIAL = (Platform.MESSENGER, Platform.INSTAGRAM)  # identidad por cuenta, sin teléfono (ADR-035)
PAGE_SUBSCRIPTIONS = ("SUBSCRIBED", "NOT_SUBSCRIBED", "UNKNOWN")  # F7b-06h, `page_subscription`
SUBSCRIPTION_MAX_TURNS = 4  # F7b-06i: los intervalos de salud que vale un «página suscrita»
# F7b-06i: el desfase de reloj tolerado al leer `last_healthcheck_at` (la web, o la base en
# `channel_accounts_health_due`, `0052`); más allá, es futura. Desde F7b-06j lo escribe la hora de
# la base, así que el margen cubre el reloj de la web, las filas escritas antes por un worker y un
# salto atrás del reloj de la base.
HEALTH_CLOCK_SKEW = dt.timedelta(minutes=5)
PAGE_OWNER = "channel_accounts_page_owner_ex"  # la página de Messenger o la de Instagram
EXTERNAL_ID = r"^[A-Za-z0-9._-]{1,100}$"  # el PSID o el IGSID: un id opaco de Meta, sin espacios


AccountStatus: type[models.TextChoices] = models.TextChoices(  # type: ignore[assignment]
    "AccountStatus", "PENDING CONNECTED DEGRADED DISCONNECTED ERROR"
)


# Por qué una cuenta real no está bien (ADR-029 §2): un código de un conjunto cerrado, nunca el
# texto de un error de Meta. `PAGE_NOT_SUBSCRIBED` (F7b-06g, ADR-035 §10): la app del CRM no está
# entre las apps suscritas de la página de Messenger o Instagram.
STATUS_DETAILS = (
    "TOKEN_INVALID", "WEBHOOK_NOT_SEEN", "QUALITY_LOW", "LIMIT_REACHED", "DISCONNECTED_BY_ADMIN",
    "PROVIDER_ERROR", "MESSAGES_ACCESS_OFF", "PAGE_NOT_SUBSCRIBED",
)  # fmt: skip
QUALITY_RATINGS = ("GREEN", "YELLOW", "RED", "UNKNOWN")  # a confirmar con la documentación de Meta
LIMIT_TIERS = (  # a confirmar con la documentación de Meta
    "TIER_50", "TIER_250", "TIER_1K", "TIER_10K", "TIER_100K", "TIER_UNLIMITED", "TIER_NOT_SET",
)  # fmt: skip
META_ID = r"^[0-9]{5,30}$"  # `phone_number_id` y WABA: dígitos (a confirmar)
ECHO_HEX = r"^[0-9a-f]{32}$"  # `secrets.token_hex(16)`: 128 bits (ADR-035 §6)
META_ERROR = r"^[0-9]{1,10}(/[0-9]{1,10})?$"  # el error de Meta en números: `code[/subcode]`


# Por qué un mensaje saliente quedó `FAILED` (F7-06a; ADR-029 §8): un conjunto cerrado de códigos
# propios, nunca el título ni el texto del error del proveedor. Los de la política y el despachador
# y los que salen de la tabla de errores de Meta (`delivery.CODES`). `CONSENT_REVOKED`: F7-08b.
# `MESSAGES_ACCESS_OFF` (F7b-05a, ADR-035 §6): el titular de Instagram apagó el acceso a mensajes.
FAILURE_CODES = (
    "CONVERSATION_CLOSED", "CHANNEL_UNAVAILABLE", "CONTACT_BLOCKED", "WINDOW_CLOSED",
    "CONSENT_REVOKED", "ADAPTER_REJECTED", "RETRIES_EXHAUSTED", "TOKEN_INVALID",
    "RECIPIENT_UNREACHABLE", "PROVIDER_REJECTED", "PROVIDER_UNKNOWN", "RATE_LIMITED",
    "QUALITY_BLOCKED", "TEMPLATE_UNAVAILABLE", "PROVIDER_ERROR", "MEDIA_NOT_SCANNED",
    "MESSAGES_ACCESS_OFF",
)  # fmt: skip


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


class HumanRequest(models.TextChoices):
    """Por qué se pidió que una persona atienda (F8-08n): lo que detectó el copiloto (ADR-034 §8:
    el cliente pidió una persona, está molesto o lo pidió el modelo) o lo pidió quien atiende."""

    ASKED_FOR_PERSON = "ASKED_FOR_PERSON"
    UPSET = "UPSET"
    MODEL = "MODEL"
    AGENT = "AGENT"


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
    # Un medio entrante con su adjunto (F7-09a2b): el cuerpo es la misma frase fija; el sticker es
    # `IMAGE` (su adjunto dice `STICKER`). Saliente (F7-09b): `IMAGE` y `DOCUMENT`, con su archivo.
    IMAGE = "IMAGE"
    AUDIO = "AUDIO"
    VIDEO = "VIDEO"
    DOCUMENT = "DOCUMENT"


INBOUND_ONLY = ("UNSUPPORTED", "AUDIO", "VIDEO")  # los tipos que solo escribe un cliente
OUTBOUND_MEDIA = ("IMAGE", "DOCUMENT")  # un saliente de estos tipos lleva su archivo, y solo él


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
    # F7b-06h: lo último que Meta dijo de la app del CRM en la página (`None`, no se sabe) y el
    # último intento de comprobación que no terminó (la cola de `channel_accounts_health_due`).
    page_subscribed = models.BooleanField(null=True, blank=True)
    health_attempt_at = models.DateTimeField(null=True, blank=True)
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
            ExclusionConstraint(  # F7b-06e (ADR-035 §3): una página, de una sola organización
                name=PAGE_OWNER,  # sea cual sea la plataforma; en la misma, Messenger e Instagram
                index_type="gist",
                expressions=[
                    (Coalesce("external_parent_id", "external_account_id"), RangeOperators.EQUAL),
                    ("organization_id", RangeOperators.NOT_EQUAL),
                ],
                condition=models.Q(deleted_at__isnull=True, platform__in=SOCIAL),
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
            models.CheckConstraint(  # una página: sin padre ni la salud propia de WhatsApp
                condition=~models.Q(platform="MESSENGER")
                | (
                    models.Q(external_account_id__regex=META_ID)
                    & models.Q(external_parent_id__isnull=True)
                    & models.Q(quality_rating__isnull=True, messaging_limit_tier__isnull=True)
                ),
                name="channel_accounts_messenger_ck",
            ),
            models.CheckConstraint(  # una cuenta de Instagram: su página vinculada es el padre
                condition=~models.Q(platform="INSTAGRAM")
                | (
                    models.Q(external_account_id__regex=META_ID)
                    & models.Q(external_parent_id__isnull=False)
                    & models.Q(external_parent_id__regex=META_ID)
                    & models.Q(quality_rating__isnull=True, messaging_limit_tier__isnull=True)
                ),
                name="channel_accounts_instagram_ck",
            ),
            models.UniqueConstraint(  # destino de la FK de las identidades: la misma plataforma
                fields=["organization_id", "id", "platform"],
                name="channel_accounts_org_platform_uq",
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
            models.CheckConstraint(  # F7b-06h: solo una página tiene apps suscritas
                condition=models.Q(page_subscribed__isnull=True)
                | models.Q(platform__in=["MESSENGER", "INSTAGRAM"]),
                name="channel_accounts_page_subscribed_ck",
            ),
        ]

    def __str__(self) -> str:
        return str(self.pk)

    @property
    def page_subscription(self) -> str | None:
        """Lo que la pantalla puede afirmar de la suscripción de la página (F7b-06h), un código
        cerrado: `SUBSCRIBED` solo si la última respuesta de Meta lo dijo, no es más vieja que
        `subscription_max_age()` ni futura (más allá de `HEALTH_CLOCK_SKEW`), y la cuenta se sigue
        comprobando; `UNKNOWN` si no se sabe (sin `META_APP_ID`, un rechazo que no cambia nada,
        `ERROR`, una respuesta vieja); `None` fuera de Messenger e Instagram."""
        if self.platform not in SOCIAL:
            return None
        if self.status_detail == "PAGE_NOT_SUBSCRIBED":
            return "NOT_SUBSCRIBED"
        checked = self.status in ("PENDING", "CONNECTED", "DEGRADED")
        at, now = self.last_healthcheck_at, timezone.now()
        fresh = at is not None and now - subscription_max_age() <= at <= now + HEALTH_CLOCK_SKEW
        return "SUBSCRIBED" if checked and fresh and self.page_subscribed is True else "UNKNOWN"


def subscription_max_age() -> dt.timedelta:
    """Hasta cuándo vale el último «página suscrita» de Meta (F7b-06i): cuatro intervalos de la
    comprobación de salud (una hora por omisión). Una cuenta que Meta no contesta (un corte o un
    límite de tasa largo) conserva su última respuesta; pasada esta edad la pantalla ya no la
    afirma, y una hora futura tampoco vale."""
    return dt.timedelta(minutes=SUBSCRIPTION_MAX_TURNS * settings.CHANNEL_HEALTH_INTERVAL_MINUTES)


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
    created_by_user_id = models.UUIDField(null=True, blank=True)  # FK a membresías (0034, F2-128)
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


class ContactIdentity(TenantModel):
    """El id de un contacto en una cuenta de Instagram o Messenger (ADR-035 §5; 02 §E.5): el IGSID
    o el PSID, únicos por cuenta de canal y nunca globales. Sin teléfono y nunca se fusiona: dos
    redes son dos contactos hasta que una persona los vincule (D-F7b-05). Inmutable: `crm_app` solo
    lee e inserta. La plataforma es la de la cuenta (FK compuesta en la migración)."""

    id = uuid7_primary_key()
    contact = models.ForeignKey(
        "contacts.Contact", models.PROTECT, related_name="+", db_constraint=False, db_index=False
    )
    channel_account = models.ForeignKey(
        ChannelAccount, models.PROTECT, related_name="+", db_constraint=False, db_index=False
    )
    platform = models.CharField(max_length=16, choices=Platform.choices)
    external_id = models.CharField(max_length=100)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "contact_identities"
        indexes = [
            models.Index(
                fields=["organization_id", "contact"], name="contact_identities_contact_idx"
            )
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["organization_id", "id"], name="contact_identities_org_id_uq"
            ),
            models.UniqueConstraint(  # destino de la FK de la conversación: su contacto y cuenta
                fields=["organization_id", "id", "contact", "channel_account"],
                name="contact_identities_thread_uq",
            ),
            models.UniqueConstraint(  # el PSID es por página y el IGSID por cuenta [S1] [S27]
                fields=["organization_id", "channel_account", "external_id"],
                name="contact_identities_external_uq",
            ),
            models.CheckConstraint(
                condition=models.Q(platform__in=SOCIAL), name="contact_identities_platform_ck"
            ),
            models.CheckConstraint(
                condition=models.Q(external_id__regex=EXTERNAL_ID),
                name="contact_identities_external_ck",
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
    # A quién responder en Instagram o Messenger (ADR-035 §5): obligatoria en esas cuentas
    # (disparador) y del mismo contacto y cuenta que el hilo (FK compuesta, migración 0040).
    contact_identity = models.ForeignKey(
        ContactIdentity, models.PROTECT, null=True, blank=True, related_name="+",
        db_constraint=False, db_index=False,
    )  # fmt: skip
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
    # Instagram o Messenger (ADR-035 §4): desde cuándo otra app controla el hilo (`standby`). La
    # fija un entrante en `standby` y la limpia uno normal, el más reciente según el canal.
    standby_since = models.DateTimeField(null=True, blank=True)
    # F8-08n: alguien pidió que una persona la atienda (quién, cuándo y por qué); se borra cuando
    # una persona responde al cliente o la toma, o a mano. El historial, en la auditoría.
    human_requested_at = models.DateTimeField(null=True, blank=True)
    human_request_reason = models.CharField(  # noqa: DJ001
        max_length=20, choices=HumanRequest.choices, null=True, blank=True
    )
    human_requested_by_user_id = models.UUIDField(null=True, blank=True)
    # F8-13: excluida de la IA (cuándo y quién): el copiloto no la lee ni la manda a un proveedor.
    ai_excluded_at = models.DateTimeField(null=True, blank=True)
    ai_excluded_by_user_id = models.UUIDField(null=True, blank=True)
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
            models.Index(  # «Sin responder» (F6-09): CONCURRENTLY en la migración 0037
                fields=["organization_id", "-last_message_at", "-id"],
                condition=models.Q(last_message_direction="INBOUND")
                & ~models.Q(status__in=["RESOLVED", "CLOSED"]),
                name="conversations_unanswered_idx",
            ),
            models.Index(  # las cuentas con hilos en `standby` (F7b-06b): CONCURRENTLY en la 0048
                fields=["organization_id", "channel_account"],
                condition=models.Q(standby_since__isnull=False) & ~models.Q(status="CLOSED"),
                name="conversations_standby_idx",
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
            models.CheckConstraint(  # F8-08n: la solicitud va entera (quién, cuándo, por qué) o no
                condition=models.Q(
                    human_requested_at__isnull=True,
                    human_request_reason__isnull=True,
                    human_requested_by_user_id__isnull=True,
                )
                | models.Q(
                    human_requested_at__isnull=False,
                    human_requested_by_user_id__isnull=False,
                    human_request_reason__isnull=False,  # un NULL pasaría el `IN` de un CHECK
                    human_request_reason__in=HumanRequest.values,
                ),
                name="conversations_human_request_ck",
            ),
            models.CheckConstraint(  # F8-13: la exclusión va entera (cuándo y quién) o no
                condition=models.Q(ai_excluded_at__isnull=True, ai_excluded_by_user_id__isnull=True)
                | models.Q(ai_excluded_at__isnull=False, ai_excluded_by_user_id__isnull=False),
                name="conversations_ai_excluded_ck",
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
    provider_error_code = models.CharField(max_length=24, null=True, blank=True)  # noqa: DJ001
    # El error del proveedor en números (F7b-13): `131031` o `10/2018278`, nunca su texto.
    provider_error_detail = models.CharField(max_length=24, null=True, blank=True)  # noqa: DJ001
    # Reintentos del canal (F7-05c): cuántas veces se devolvió el reclamo y no antes de cuándo
    # vuelve a intentarse (el barredor de la cola no lo reencola antes: no amplifica).
    dispatch_attempts = models.PositiveSmallIntegerField(default=0, db_default=0)
    not_before = models.DateTimeField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    read_at = models.DateTimeField(null=True, blank=True)
    failed_at = models.DateTimeField(null=True, blank=True)
    # La decisión de la política (ADR-010) al encolar y tras la última revaluación: solo la
    # instantánea de `PolicyDecision.as_json()` (permitido, modo, motivo y horas), nunca texto.
    policy_decision = models.JSONField(null=True, blank=True)
    file_id = models.UUIDField(null=True, blank=True)  # el medio saliente: FK compuesta (0027)
    # F7b-05c (ADR-035 §6): la ficha aleatoria (128 bits, `secrets`) que un envío de Instagram o
    # Messenger lleva en `message.metadata`; su eco la devuelve. Nunca el id ni datos del mensaje.
    echo_token = models.CharField(max_length=32, null=True, blank=True)  # noqa: DJ001
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
            models.Index(  # el barredor de F7-05c: solo lo que sigue `SENDING` (pocas filas)
                fields=["organization_id", "sending_at"],
                condition=models.Q(delivery_status="SENDING"),
                name="messages_sending_idx",
            ),
        ]
        constraints = [
            models.UniqueConstraint(  # destino de la FK compuesta de los adjuntos (0020)
                fields=["organization_id", "id"], name="messages_org_id_uq"
            ),
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
            models.UniqueConstraint(  # F7b-05c: la ficha del eco, única por cuenta (0046)
                fields=["organization_id", "channel_account", "echo_token"],
                condition=models.Q(echo_token__isnull=False),
                name="messages_echo_token_uq",
            ),
            models.CheckConstraint(
                condition=models.Q(echo_token__isnull=True)
                | models.Q(direction="OUTBOUND", echo_token__regex=ECHO_HEX),
                name="messages_echo_token_ck",
            ),
            one_of("direction", Direction, "messages_direction_ck"),
            one_of("sender_type", SenderType, "messages_sender_ck"),
            one_of("message_type", MessageType, "messages_type_ck"),
            models.CheckConstraint(  # «no soportado» y los medios solo los escribe un cliente
                condition=~models.Q(message_type__in=INBOUND_ONLY) | models.Q(direction="INBOUND"),
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
            models.CheckConstraint(  # el motivo de un fallo: un código cerrado, solo con `FAILED`
                condition=models.Q(provider_error_code__isnull=True)
                | (
                    models.Q(delivery_status="FAILED")
                    & models.Q(provider_error_code__in=FAILURE_CODES)
                ),
                name="messages_error_code_ck",
            ),
            models.CheckConstraint(  # el error del proveedor en números, solo con `FAILED`
                condition=models.Q(provider_error_detail__isnull=True)
                | models.Q(delivery_status="FAILED", provider_error_detail__regex=META_ERROR),
                name="messages_error_detail_ck",
            ),
            models.CheckConstraint(  # la decisión de la política es solo de lo saliente
                condition=models.Q(policy_decision__isnull=True) | models.Q(direction="OUTBOUND"),
                name="messages_policy_ck",
            ),
            models.CheckConstraint(  # un archivo solo en un medio saliente, que siempre lo lleva;
                condition=models.Q(  # un documento nunca es entrante (aún sin antivirus)
                    file_id__isnull=False, direction="OUTBOUND", message_type__in=OUTBOUND_MEDIA
                )
                | (
                    models.Q(file_id__isnull=True)
                    & ~models.Q(message_type="DOCUMENT")
                    & ~models.Q(direction="OUTBOUND", message_type="IMAGE")
                ),
                name="messages_file_ck",
            ),
        ]

    def __str__(self) -> str:
        return str(self.pk)


MEDIA_KINDS = ("IMAGE", "AUDIO", "VIDEO", "STICKER")  # documentos: cuando haya antivirus
MEDIA_STATUSES = ("PENDING", "DOWNLOADING", "DONE", "FAILED", "EXPIRED")
MEDIA_FAILURES = (
    "TOKEN_INVALID", "CHANNEL_UNAVAILABLE", "UNAVAILABLE", "TOO_LARGE", "TYPE_NOT_ALLOWED",
    "TYPE_MISMATCH",
)  # fmt: skip


class MessageAttachment(TenantModel):
    """El medio de un mensaje entrante (F7-09a1b; ADR-029 §7), uno por mensaje; de Meta solo su id.
    La máquina de estados la cumple el disparador de `0021`; `attempt_id`: el intento y su blob."""

    id = uuid7_primary_key()
    message = models.ForeignKey(
        Message, models.PROTECT, related_name="+", db_constraint=False, db_index=False
    )
    kind = models.CharField(max_length=8)
    external_media_id = models.CharField(max_length=30)
    download_status = models.CharField(max_length=12, default="PENDING")
    failure_code = models.CharField(max_length=24, null=True, blank=True)  # noqa: DJ001
    attempts = models.PositiveSmallIntegerField(default=0)
    attempt_id = models.UUIDField(null=True, blank=True)
    lease_until = models.DateTimeField(null=True, blank=True)
    not_before = models.DateTimeField(null=True, blank=True)
    file_id = models.UUIDField(null=True, blank=True)  # FK compuesta a `files` (0021)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "message_attachments"
        indexes = [
            models.Index(  # el tope por organización: solo las descargas en curso
                fields=["organization_id", "lease_until"],
                condition=models.Q(download_status="DOWNLOADING"),
                name="message_attachments_busy_idx",
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["organization_id", "message"], name="message_attachments_message_uq"
            ),
            models.CheckConstraint(
                condition=models.Q(
                    kind__in=MEDIA_KINDS,
                    download_status__in=MEDIA_STATUSES,
                    external_media_id__regex=META_ID,
                    attempts__lte=5,
                ),  # fmt: skip
                name="message_attachments_values_ck",
            ),
            models.CheckConstraint(  # un código cerrado, siempre y solo con `FAILED` (un NULL
                condition=models.Q(  # pasaría el `IN`: por eso el `IS NOT NULL`)
                    download_status="FAILED",
                    failure_code__isnull=False,
                    failure_code__in=MEDIA_FAILURES,
                )
                | (~models.Q(download_status="FAILED") & models.Q(failure_code__isnull=True)),
                name="message_attachments_failure_ck",
            ),
            models.CheckConstraint(  # el archivo, siempre y solo con `DONE`
                condition=models.Q(download_status="DONE", file_id__isnull=False)
                | (~models.Q(download_status="DONE") & models.Q(file_id__isnull=True)),
                name="message_attachments_file_ck",
            ),
            models.CheckConstraint(  # una descarga en curso tiene su intento y su plazo
                condition=~models.Q(download_status="DOWNLOADING")
                | models.Q(attempt_id__isnull=False, lease_until__isnull=False),
                name="message_attachments_lease_ck",
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


SHORTCUT = r"^[a-z0-9_-]{1,32}$"  # el atajo sin la `/`: `delivery`, `garantia_12m`


def _soft(table: str) -> list[models.BaseConstraint]:
    """Lo común de las dos tablas de respuestas rápidas: destino de FKs compuestas, una borrada
    no está activa y la versión empieza en 1."""
    return [
        models.UniqueConstraint(fields=["organization_id", "id"], name=f"{table}_org_id_uq"),
        models.CheckConstraint(
            condition=models.Q(deleted_at__isnull=True) | models.Q(is_active=False),
            name=f"{table}_deleted_ck",
        ),
        models.CheckConstraint(condition=models.Q(version__gte=1), name=f"{table}_version_ck"),
    ]


class QuickReplyCategory(TenantModel):
    """Categoría de respuestas rápidas (02 §E.6): Ventas, Delivery… Se borra lógicamente. El
    nombre es único, sin distinguir mayúsculas, entre las vivas."""

    id = uuid7_primary_key()
    name = models.CharField(max_length=100)
    sort_order = models.IntegerField(default=0)
    is_active = models.BooleanField(default=True)
    created_by_user_id = models.UUIDField(null=True, blank=True)
    updated_by_user_id = models.UUIDField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    deleted_at = models.DateTimeField(null=True, blank=True)
    deleted_by_user_id = models.UUIDField(null=True, blank=True)
    version = models.PositiveIntegerField(default=1)

    class Meta:
        db_table = "quick_reply_categories"
        constraints = [
            *_soft("qr_categories"),
            models.UniqueConstraint(
                "organization_id",
                Lower("name"),
                condition=models.Q(deleted_at__isnull=True),
                name="qr_categories_name_uq",
            ),  # fmt: skip
        ]


class QuickReply(TenantModel):
    """Respuesta rápida de la organización (02 §E.6, §20), visible para toda ella (ORG): solo
    texto, con las variables de la lista cerrada de `apps.conversations.quick_replies`. El atajo
    es único entre las activas y vivas. Se borra lógicamente."""

    id = uuid7_primary_key()
    shortcut = models.CharField(max_length=32)
    title = models.CharField(max_length=100)
    category = models.ForeignKey(
        QuickReplyCategory, models.PROTECT, related_name="+", db_constraint=False, db_index=False
    )
    body = models.TextField()
    is_active = models.BooleanField(default=True)
    usage_count = models.PositiveIntegerField(default=0)
    created_by_user_id = models.UUIDField(null=True, blank=True)
    updated_by_user_id = models.UUIDField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    deleted_at = models.DateTimeField(null=True, blank=True)
    deleted_by_user_id = models.UUIDField(null=True, blank=True)
    version = models.PositiveIntegerField(default=1)

    class Meta:
        db_table = "quick_replies"
        indexes = [
            models.Index(fields=["organization_id", "category"], name="quick_replies_category_idx")
        ]
        constraints = [
            *_soft("quick_replies"),
            models.UniqueConstraint(
                fields=["organization_id", "shortcut"],
                condition=models.Q(is_active=True, deleted_at__isnull=True),
                name="quick_replies_shortcut_uq",
            ),
            models.CheckConstraint(
                condition=models.Q(shortcut__regex=SHORTCUT), name="quick_replies_shortcut_ck"
            ),
            models.CheckConstraint(
                condition=models.Q(body__regex=r"\S")
                & models.Q(LessThanOrEqual(Length("body"), BODY_MAX)),
                name="quick_replies_body_ck",
            ),
        ]


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
    waba_id = models.CharField(max_length=30, null=True)  # noqa: DJ001 — solo WhatsApp
    phone_number_id = models.CharField(max_length=30, null=True)  # noqa: DJ001
    # F7b-04a (ADR-035 §4): la plataforma y, en Instagram y Messenger, el `entry.id` que enruta.
    platform = models.CharField(max_length=16, db_default="WHATSAPP")
    routing_id = models.CharField(max_length=30, null=True)  # noqa: DJ001
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
            models.CheckConstraint(  # WhatsApp enruta por número y WABA; los demás, por cuenta
                condition=models.Q(
                    platform="WHATSAPP", waba_id__isnull=False, routing_id__isnull=True
                )
                | models.Q(
                    platform__in=SOCIAL,
                    waba_id__isnull=True,
                    phone_number_id__isnull=True,
                    routing_id__isnull=False,
                ),
                name="webhook_ingress_route_ck",
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
