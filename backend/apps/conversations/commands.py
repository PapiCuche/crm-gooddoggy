"""Comandos del Inbox (F6-01c; E06, 02 §E.6, ADR-007, 010): el único escritor de las tablas.

Solo la API (F6-02) los importa (contrato de import-linter) y comprueba el permiso de la ruta
antes de leer; los que reciben el id de una conversación en nombre de una persona (`record_outbound`
y `add_note`) comprueban además, sobre la conversación ya bloqueada, el permiso y su alcance
(`Visible`): lo desconocido y lo que queda fuera del alcance dan el mismo `DoesNotExist`. Los demás
(un mensaje entrante, una apertura, el estado de entrega que informa el canal) los llama el canal o
el despachador con su contexto. Validan los tipos exactos (`ValueError`, nunca `TypeError` ni
`IntegrityError`) y auditan solo identificadores, estados y longitudes: **ningún texto de mensaje**
llega a la auditoría. La organización sale siempre del contexto, nunca de un dato recibido. Cada
uno es idempotente donde el mundo reintenta (un webhook repetido, un envío reenviado): la clave es
`external_message_id` o `client_message_id`.

Crear una conversación bloquea el contacto (`FOR NO KEY UPDATE`): dos mensajes entrantes a la vez
de un mismo contacto se serializan y la segunda encuentra la conversación de la primera; el índice
parcial único de la base es la red de seguridad. Un estado de entrega tardío o repetido no
retrocede el mensaje y no es un error (ADR-010, §E.6).
"""

import datetime as dt
import re
import unicodedata
from typing import Any
from uuid import UUID

from django.conf import settings
from django.db import IntegrityError, connections, transaction
from django.db.models import F, Q
from django.utils import timezone

from apps.access.selectors import (
    AccessDenied,
    Denied,
    ExecutionContext,
    can,
    execution_context,
    has_permission,
    member_holds,
)
from apps.audit.services import Entity, record
from apps.contacts.models import Contact
from apps.conversations import media, outbound_media
from apps.conversations.channels import REASONS, Claim, Media, SendResult
from apps.conversations.models import (
    BODY_MAX,
    META_ID,
    PREVIEW_MAX,
    AttentionMode,
    ChannelAccount,
    Conversation,
    ConversationAssignment,
    Credential,
    Delivery,
    Message,
    MessageType,
    Platform,
    Status,
)
from apps.conversations.policy import PURPOSES, ConversationClosed, require_allowed
from apps.conversations.selectors import messaging_decision
from apps.files.models import File
from apps.organizations.selectors import lock_active_member
from core.outbox import emit
from core.sequences import allocate
from core.tenancy.context import TenantContext
from core.tenancy.scope import require_scope

REOPEN_HOURS = 72  # D-INB-2: una RESOLVED recibe mensajes hasta 72 h después; luego se cierra
WINDOW_HOURS = 24  # la ventana de WhatsApp: un entrante con más de 24 h no reabre nada
KEY = "CONV"
ORDER = (Delivery.QUEUED, Delivery.SENDING, Delivery.SENT, Delivery.DELIVERED, Delivery.READ)
ZWJ, JOINERS = "\u200d", "\u200c\u200d"
BIDI = "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"
LAST = ("last_message_at", "last_message_preview", "last_message_direction")
STAMPS = {
    "SENDING": "sending_at", "SENT": "sent_at", "DELIVERED": "delivered_at", "READ": "read_at",
    "FAILED": "failed_at",
}  # fmt: skip
EXTERNAL_MAX = 200
NAME_MAX = 100
EDITABLE = ("name", "display_identifier")  # la plataforma y el id externo no cambian


class AssignmentConflict(Exception):
    """Otra persona cambió la asignación (la versión ya no es la que leyó quien la pide) o la
    conversación ya tiene responsable (tomar) o ya no lo tiene (soltar)."""


class BadReference(ValueError):
    """Una referencia que no sirve (el contacto o la cuenta no son de la organización o están
    borrados). `field` dice cuál."""

    def __init__(self, field: str) -> None:
        super().__init__(field)
        self.field = field


class ClientMessageConflict(Exception):
    """Ese `client_message_id` ya es de otro mensaje (otro texto u otra conversación)."""


class StaleVersion(Exception):
    """Otra petición cambió la cuenta después de la versión que se editaba."""


def _uuid(value: Any, what: str) -> UUID:
    if isinstance(value, UUID):
        return value
    if not isinstance(value, str):
        raise ValueError(f"{what}: un identificador")
    try:
        return UUID(value)
    except ValueError:
        raise ValueError(f"{what}: un identificador") from None


def _body(value: Any) -> str:
    """El texto de un mensaje: algo que se ve (no solo espacios ni caracteres de ancho cero), sin
    controles salvo el salto de línea y el tabulador, ni sustitutos Unicode sueltos (UTF-8 y
    PostgreSQL no los guardan), ni los controles que reordenan el texto (incrustaciones,
    anulaciones y aislamientos de dirección; las marcas LRM, RLM y ALM sí valen)."""
    if not isinstance(value, str) or len(value) > BODY_MAX:
        raise ValueError(f"texto: un texto de 1 a {BODY_MAX} caracteres")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("texto: caracteres Unicode no válidos") from None
    if any(unicodedata.category(c) == "Cc" and c not in "\n\r\t" for c in value):
        raise ValueError("texto: sin caracteres de control")
    if any(c in BIDI for c in value):  # reordenan lo que se ve (texto «trojan source»)
        raise ValueError("texto: sin controles de dirección")
    if all(unicodedata.category(c) in ("Cc", "Cf", "Zs", "Zl", "Zp") for c in value):
        raise ValueError("texto: debe tener algo visible")
    return value


def _preview(text: str) -> str:
    """Lo que la lista enseña del último mensaje: el comienzo, en una línea y sin caracteres de
    formato invisibles (se conservan ZWNJ y ZWJ, que el persa y los emoji necesitan). Si el corte
    cae dentro de una secuencia unida por ZWJ, se quita la secuencia entera en vez de partirla."""
    shown = "".join(c for c in text if unicodedata.category(c) != "Cf" or c in JOINERS)
    line = " ".join(shown.split())
    cut = line[:PREVIEW_MAX]
    while cut and (cut[-1] == ZWJ or line[len(cut) : len(cut) + 1] == ZWJ):  # dentro de una unión
        cut = cut[:-1]  # solo esa secuencia: los emoji anteriores se quedan
    return cut.rstrip()


def _external(value: Any) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= EXTERNAL_MAX:
        raise ValueError(f"id externo: un texto de 1 a {EXTERNAL_MAX} caracteres")
    if value != value.strip() or not value.isprintable():
        raise ValueError("id externo: sin espacios en los extremos ni caracteres de control")
    return value


def _sent(message: Message) -> dict[str, Any]:
    """Lo único que se audita de un mensaje: ids, dirección y longitud, nunca el texto."""
    return {
        "conversation_id": str(message.conversation_id),
        "direction": message.direction,
        "length": len(message.body),
    }


def _status(
    ctx: TenantContext, alias: str, convo: Conversation, new: str, action: str, **stamps: Any
) -> None:
    """Cambia el estado de la conversación (ya bloqueada), sube la versión y lo audita."""
    old = convo.status
    convo.status = new
    for field, value in stamps.items():
        setattr(convo, field, value)
    convo.version += 1
    convo.save(using=alias, update_fields=["status", *stamps, "version", "updated_at"])
    record(ctx, action, Entity("conversation", convo.pk, convo.number), {"status": [old, new]})
    emit(  # una pista para la pantalla: solo ids
        ctx, "conversation.updated", aggregate_type="conversation", aggregate_id=convo.pk,
    )  # fmt: skip


def ensure_sandbox_account(ctx: TenantContext) -> ChannelAccount:
    """La cuenta Sandbox de la organización: la crea la primera vez (`channel_account.created`) y
    después la devuelve. Su id externo lleva el de la organización, así que es único en la
    plataforma."""
    alias = require_scope(ctx)
    external = f"sandbox-{ctx.organization_id}"
    live = ChannelAccount.objects.using(alias).filter(
        platform=Platform.SANDBOX, external_account_id=external, deleted_at__isnull=True
    )
    with transaction.atomic(using=alias):
        found: ChannelAccount | None = live.first()
        if found is not None:
            return found
        try:
            with transaction.atomic(using=alias):
                made: ChannelAccount = ChannelAccount.objects.using(alias).create(
                    platform=Platform.SANDBOX,
                    name="Sandbox",
                    external_account_id=external,
                    created_by_user_id=ctx.user_id,
                )
        except IntegrityError:  # otra petición la creó a la vez
            again: ChannelAccount = live.get()
            return again
        record(ctx, "channel_account.created", Entity("channel_account", made.pk, made.name))
        return made


def update_channel_account(
    ctx: TenantContext, *, channel_account_id: UUID, version: int, **fields: Any
) -> ChannelAccount:
    """Cambia el nombre o el identificador visible de una cuenta si nadie la cambió desde
    `version`. **Ni la plataforma ni `external_account_id` cambian nunca** (el webhook resuelve la
    organización con ellos) ni el estado: este sigue a la conexión, y una cuenta no pasa a
    `CONNECTED` sin credencial verificada (Fase 7; la de Sandbox nace conectada). `ValueError`
    (también ante un campo no admitido); `StaleVersion`; `DoesNotExist`."""
    alias = require_scope(ctx)
    account_id = _uuid(channel_account_id, "cuenta")
    if type(version) is not int or not 1 <= version <= 2**31 - 1:
        raise ValueError("versión: un entero")
    if unknown := set(fields) - set(EDITABLE):
        raise ValueError(f"campos no admitidos: {sorted(unknown)}")
    for value in fields.values():
        if not isinstance(value, str) or len(value) > NAME_MAX or not value.isprintable():
            raise ValueError(f"texto de una línea de hasta {NAME_MAX} caracteres")
    if "name" in fields and not fields["name"].strip():
        raise ValueError("nombre: obligatorio")
    with transaction.atomic(using=alias):
        live = ChannelAccount.objects.using(alias).filter(deleted_at__isnull=True)
        account: ChannelAccount = live.select_for_update(no_key=True).get(pk=account_id)
        if account.version != version:
            raise StaleVersion
        changes = {
            k: [getattr(account, k), v] for k, v in fields.items() if getattr(account, k) != v
        }
        if changes:
            for field, (_, value) in changes.items():
                setattr(account, field, value)
            account.version += 1
            account.save(using=alias, update_fields=[*changes, "version", "updated_at"])
            record(ctx, "channel_account.updated", Entity("channel_account", account.pk), changes)
        return account


def _open(
    ctx: TenantContext, alias: str, contact_id: UUID, account_id: UUID
) -> tuple[Conversation, bool]:
    """La conversación no cerrada del contacto en la cuenta, o una nueva en cola (`PENDING`,
    D-INB-1). Con el contacto bloqueado. Una resuelta hace más de `REOPEN_HOURS` se cierra y se
    abre otra (D-INB-2)."""
    live = Contact.objects.using(alias).filter(pk=contact_id, deleted_at__isnull=True)
    if not live.select_for_update(no_key=True).values_list("pk", flat=True)[:1]:
        raise BadReference("contact_id")
    accounts = ChannelAccount.objects.using(alias).filter(pk=account_id, deleted_at__isnull=True)
    if not accounts.exists():
        raise BadReference("channel_account_id")
    open_ones = Conversation.objects.using(alias).exclude(status=Status.CLOSED)
    open_ones = open_ones.filter(contact_id=contact_id, channel_account_id=account_id)
    if (found := open_ones.select_for_update(no_key=True).first()) is not None:
        stale = dt.timedelta(hours=REOPEN_HOURS)
        if (
            found.status != Status.RESOLVED
            or timezone.now() - (found.resolved_at or found.updated_at) <= stale
        ):
            return found, False
        _status(ctx, alias, found, Status.CLOSED, "conversation.closed", closed_at=timezone.now())
    now = timezone.now()
    try:
        with transaction.atomic(using=alias):
            made: Conversation = Conversation.objects.using(alias).create(
                number=allocate(ctx, KEY, width=5, using=alias),
                contact_id=contact_id,
                channel_account_id=account_id,
                last_message_at=now,
            )
    except IntegrityError as error:
        if "conversations_open_uq" not in str(error):
            raise
        return open_ones.get(), False  # otra petición la abrió entre medias
    meta = {"contact_id": str(contact_id), "channel_account_id": str(account_id)}
    record(ctx, "conversation.created", Entity("conversation", made.pk, made.number), None, meta)
    return made, True


def open_conversation(
    ctx: TenantContext,
    *,
    contact_id: UUID,
    channel_account_id: UUID,
    attention_mode: str = AttentionMode.HUMAN,
) -> tuple[Conversation, bool]:
    """Abre (o devuelve) la conversación del contacto en la cuenta; el segundo valor dice si se
    creó. Solo `HUMAN` hasta la Fase 8: los modos de IA existen en la base, pero ningún comando los
    pone. `BadReference("contact_id")` o `("channel_account_id")`; `ValueError`."""
    alias = require_scope(ctx)
    if attention_mode != AttentionMode.HUMAN:
        raise ValueError("modo de atención: solo HUMAN hasta la Fase 8 (IA)")
    contact_id, account_id = _uuid(contact_id, "contacto"), _uuid(channel_account_id, "cuenta")
    with transaction.atomic(using=alias):
        return _open(ctx, alias, contact_id, account_id)


def record_inbound(
    ctx: TenantContext,
    *,
    contact_id: UUID,
    channel_account_id: UUID,
    external_message_id: str,
    body: str,
    message_type: str = MessageType.TEXT,
    provider_at: dt.datetime | None = None,
    attachment: tuple[str, str] | None = None,
) -> tuple[Message, bool]:
    """Registra un mensaje del cliente, idempotente por `(cuenta, external_message_id)`: repetirlo
    devuelve el mismo y no cambia nada. Abre o reutiliza la conversación (`PENDING` si es nueva),
    pone `last_inbound_at` y `last_message_at` y, según D-INB-2, pasa a `OPEN` una conversación en
    `WAITING_CUSTOMER` o `RESOLVED` dentro del plazo. Audita `message.received` y deja el evento
    `message.received` en el outbox (solo ids). `message_type` es `TEXT` o `UNSUPPORTED` (un
    marcador: el cuerpo es una frase fija del canal) o, con `attachment` (la clase y el id del
    medio, `media.found`), el tipo de ese medio (`media.TYPES`): su adjunto `PENDING` nace con el
    mensaje nuevo, en la misma transacción (un mensaje repetido no lo vuelve a crear).
    `provider_at` es el instante que informa el canal: ancla de la ventana de 24 h
    (`last_inbound_at`, acotado a «ahora» y sin retroceder); el orden del chat sigue siendo el de
    llegada. Con más de 24 h de antigüedad no reabre una conversación `WAITING_CUSTOMER` ni
    `RESOLVED` (se anota sin cambiarle el estado)."""
    alias = require_scope(ctx)
    allowed: tuple[str, ...] = (MessageType.TEXT, MessageType.UNSUPPORTED)
    if attachment is not None:  # un medio: su tipo y nada más, con un id de solo dígitos
        kind, media_id = attachment
        ok = kind in media.TYPES and re.fullmatch(META_ID, str(media_id))
        allowed = (media.TYPES[kind],) if ok else ()
    if message_type not in allowed:
        raise ValueError("tipo: TEXT o UNSUPPORTED, o el de su medio")
    contact_id, account_id = _uuid(contact_id, "contacto"), _uuid(channel_account_id, "cuenta")
    external, text = _external(external_message_id), _body(body)
    same = Message.objects.using(alias).filter(
        channel_account_id=account_id, external_message_id=external
    )
    with transaction.atomic(using=alias):
        if found := same.first():
            return found, False
        convo, _ = _open(ctx, alias, contact_id, account_id)
        now = timezone.now()
        seen = min(provider_at, now) if provider_at is not None else None
        stale = provider_at is not None and now - provider_at > dt.timedelta(hours=WINDOW_HOURS)
        try:
            with transaction.atomic(using=alias):
                message: Message = Message.objects.using(alias).create(
                    conversation_id=convo.pk, channel_account_id=account_id, direction="INBOUND",
                    sender_type="CONTACT", body=text, external_message_id=external,
                    message_type=message_type, provider_timestamp=seen,
                )  # fmt: skip
        except IntegrityError as error:
            if "messages_external_uq" not in str(error):
                raise
            return same.get(), False  # el mismo mensaje, a la vez
        if attachment is not None:
            media.attach(ctx, message, *attachment)
        anchor = max(convo.last_inbound_at, seen or now) if convo.last_inbound_at else seen or now
        convo.last_inbound_at, convo.last_message_at = anchor, now
        convo.last_message_preview, convo.last_message_direction = _preview(text), "INBOUND"
        if convo.status in (Status.WAITING_CUSTOMER, Status.RESOLVED) and not stale:
            _status(  # con las marcas del mensaje: `update_fields` no las guardaría de otro modo
                ctx, alias, convo, Status.OPEN, "conversation.reopened", resolved_at=None,
                last_inbound_at=anchor, last_message_at=now,
                last_message_preview=convo.last_message_preview, last_message_direction="INBOUND",
            )  # fmt: skip
        else:
            convo.save(using=alias, update_fields=[*LAST, "last_inbound_at", "updated_at"])
        record(ctx, "message.received", Entity("message", message.pk), None, _sent(message))
        ids = {
            "conversation_id": convo.pk,
            "channel_account_id": account_id,
            "contact_id": contact_id,
        }
        emit(  # para quien se suscriba (la IA, las automatizaciones): solo ids, nunca el texto
            ctx, "message.received", aggregate_type="message", aggregate_id=message.pk,
            payload={key: str(value) for key, value in ids.items()},
        )  # fmt: skip
        return message, True


def _insert(
    ctx: TenantContext, alias: str, convo: Conversation, client_id: UUID | None, text: str,
    **fields: Any,
) -> tuple[Message, bool]:  # fmt: skip
    """Guarda un mensaje del actor en la conversación bloqueada; con `client_id` repetido y el mismo
    texto en la misma conversación devuelve el que ya estaba. `ClientMessageConflict` si no."""
    if client_id is not None:
        same = Message.objects.using(alias).filter(client_message_id=client_id)
        if (found := same.first()) is not None:
            if (found.conversation_id, found.direction, found.body, found.message_type) != (
                convo.pk, fields["direction"], text, fields.get("message_type", MessageType.TEXT),
            ):  # fmt: skip
                raise ClientMessageConflict
            return found, False
    sender = {"sender_type": "USER", "sender_user_id": ctx.user_id}
    try:
        with transaction.atomic(using=alias):
            made: Message = Message.objects.using(alias).create(
                conversation_id=convo.pk, channel_account_id=convo.channel_account_id,
                body=text, client_message_id=client_id, **sender, **fields,
            )  # fmt: skip
    except IntegrityError as error:
        if "messages_client_uq" not in str(error):
            raise
        raise ClientMessageConflict from None  # el mismo id en otra conversación, a la vez
    return made, True


def _locked(alias: str, conversation_id: UUID) -> Conversation:
    found: Conversation = (
        Conversation.objects.using(alias).select_for_update(no_key=True).get(pk=conversation_id)
    )
    return found


def _allowed(ctx: TenantContext, code: str) -> ExecutionContext:
    """El actor es un miembro activo y tiene el permiso: **antes** de leer la conversación, así que
    quien no lo tiene no distingue una conversación que existe de una que no."""
    ectx = execution_context(ctx)  # `AccessDenied` si no es un miembro activo
    if not has_permission(ectx, code):
        raise AccessDenied(Denied.PERMISSION)
    return ectx


def _in_scope(ectx: ExecutionContext, code: str, convo: Conversation) -> None:
    """La conversación (la bloqueada) está en el alcance del actor; si no, el mismo `DoesNotExist`,
    con el mismo texto, que una desconocida."""
    if not can(ectx, code, convo):
        raise Conversation.DoesNotExist("Conversation matching query does not exist.")


def record_outbound(
    ctx: TenantContext, *, conversation_id: UUID, client_message_id: UUID, body: str,
    purpose: str, media: outbound_media.Prepared | None = None,
) -> tuple[Message, bool]:  # fmt: skip
    """Pone en cola (`QUEUED`) un mensaje del actor al cliente, idempotente por
    `client_message_id` (el mismo id con otro texto es `ClientMessageConflict`, nunca una
    sobrescritura). Siempre nace `QUEUED`: ningún argumento elige otro estado. Cuenta como
    actividad y, si la conversación estaba en cola (`PENDING`), la pasa a `OPEN` (D-INB-1). Audita
    `message.queued` y deja el evento `message.queued` en el outbox (ids, sin texto).
    `ConversationClosed` si está cerrada, `ChannelUnavailable` si el canal no está conectado,
    `ContactBlocked` si el contacto está bloqueado y `WindowClosed` si pasó la ventana del canal
    (`policy.evaluate`, que se guarda en el mensaje; un reenvío de lo ya guardado sí se devuelve);
    `ClientMessageConflict`; `DoesNotExist` (también fuera del alcance de `messages.send`);
    `AccessDenied` sin el permiso. `purpose` (`policy.PURPOSES`; sin valor por omisión: quien llama
    dice para qué escribe): un seguimiento (`FOLLOW_UP`) a quien se dio de baja es `ConsentRevoked`,
    la respuesta (`CUSTOMER_REPLY`) no. Plantillas: F7-10. Con `media` (F7-09b, el blob ya guardado
    por `outbound_media.prepare`) el mensaje es de su tipo y crea su archivo; un reintento es el
    mismo solo con el mismo archivo (`sha256`)."""
    alias = require_scope(ctx)
    if purpose not in PURPOSES:
        raise ValueError("propósito no admitido")
    if media is not None:
        outbound_media.check(ctx, media)
    conversation_id, client_id = (
        _uuid(conversation_id, "conversación"),
        _uuid(client_message_id, "id"),
    )
    text = _body(body)
    if media is not None and len(text) > outbound_media.CAPTION_MAX:  # lo que admite WhatsApp
        raise ValueError(f"pie de foto: hasta {outbound_media.CAPTION_MAX} caracteres")
    with transaction.atomic(using=alias):
        ectx = _allowed(ctx, "messages.send")
        convo = _locked(alias, conversation_id)
        _in_scope(ectx, "messages.send", convo)
        earlier = Message.objects.using(alias).filter(client_message_id=client_id).first()
        snapshot = None
        if earlier is None:  # un reenvío de lo ya guardado se devuelve aunque ya no se pueda enviar
            decision = messaging_decision(convo, alias=alias, purpose=purpose)
            require_allowed(decision)  # cerrada, sin canal, bloqueado, ventana cerrada o baja
            snapshot = decision.as_json()
        elif media and not File.objects.using(alias).filter(
            pk=earlier.file_id, sha256=media.sha256
        ).exists():  # fmt: skip
            raise ClientMessageConflict  # el mismo id con otro archivo
        typed = {"message_type": media.kind, "file_id": media.file_id} if media else {}
        if media and earlier is None:
            File.objects.using(alias).create(
                id=media.file_id, storage_key=media.key, original_name=media.name,
                mime_type=media.mime, size_bytes=media.size, sha256=media.sha256,
                purpose="message-media", created_by_user_id=ctx.user_id,
            )  # fmt: skip
        message, created = _insert(
            ctx, alias, convo, client_id, text, direction="OUTBOUND", delivery_status="QUEUED",
            policy_decision=snapshot, **typed,
        )  # fmt: skip
        if created:
            now = timezone.now()
            if convo.status == Status.PENDING:  # con la marca: `update_fields` no la guardaría sola
                _status(
                    ctx, alias, convo, Status.OPEN, "conversation.opened", last_message_at=now,
                    last_message_preview=_preview(text), last_message_direction="OUTBOUND",
                )  # fmt: skip
            else:
                convo.last_message_at = now
                convo.last_message_preview, convo.last_message_direction = (
                    _preview(text),
                    "OUTBOUND",
                )
                convo.save(using=alias, update_fields=[*LAST, "updated_at"])
            record(ctx, "message.queued", Entity("message", message.pk), None, _sent(message))
            ids = {"conversation_id": convo.pk, "channel_account_id": convo.channel_account_id}
            emit(  # para el despachador (F6-04b); solo ids, nunca el texto
                ctx,
                "message.queued",
                aggregate_type="message",
                aggregate_id=message.pk,
                payload={key: str(value) for key, value in ids.items()},
            )
        return message, created


def queue_confirmation(ctx: TenantContext, *, conversation_id: UUID, body: str) -> Message | None:
    """Pone en cola un texto fijo del sistema al cliente (F7-08b: la confirmación de una baja o un
    alta), sin persona (`SYSTEM`) ni permiso: lo llama el canal en la transacción del mensaje
    entrante. Es una respuesta (`CUSTOMER_REPLY`): si la política la niega, no escribe nada y
    devuelve `None`."""
    alias = require_scope(ctx)
    text = _body(body)
    with transaction.atomic(using=alias):
        convo = _locked(alias, _uuid(conversation_id, "conversación"))
        decision = messaging_decision(convo, alias=alias)
        if not decision.allowed:
            return None
        message: Message = Message.objects.using(alias).create(
            conversation_id=convo.pk, channel_account_id=convo.channel_account_id, body=text,
            direction="OUTBOUND", sender_type="SYSTEM", delivery_status="QUEUED",
            policy_decision=decision.as_json(),
        )  # fmt: skip
        convo.last_message_at = timezone.now()
        convo.last_message_preview, convo.last_message_direction = _preview(text), "OUTBOUND"
        convo.save(using=alias, update_fields=[*LAST, "updated_at"])
        record(ctx, "message.queued", Entity("message", message.pk), None, _sent(message))
        ids = {"conversation_id": convo.pk, "channel_account_id": convo.channel_account_id}
        emit(  # para el despachador, como `record_outbound`; solo ids
            ctx, "message.queued", aggregate_type="message", aggregate_id=message.pk,
            payload={key: str(value) for key, value in ids.items()},
        )  # fmt: skip
        return message


def add_note(
    ctx: TenantContext, *, conversation_id: UUID, body: str, client_message_id: UUID | None = None
) -> tuple[Message, bool]:
    """Una nota interna: nunca sale por un canal (`INTERNAL` y `NOTE`, por `CHECK`), no cuenta como
    mensaje de la conversación y se admite también en una cerrada. Idempotente si trae
    `client_message_id`. Exige `messages.note` y su alcance, como `record_outbound`. Audita
    `message.noted`."""
    alias = require_scope(ctx)
    conversation_id = _uuid(conversation_id, "conversación")
    client_id = None if client_message_id is None else _uuid(client_message_id, "id")
    text = _body(body)
    with transaction.atomic(using=alias):
        ectx = _allowed(ctx, "messages.note")
        convo = _locked(alias, conversation_id)
        _in_scope(ectx, "messages.note", convo)
        note, created = _insert(
            ctx, alias, convo, client_id, text, direction="INTERNAL", message_type="NOTE"
        )
        if created:
            record(ctx, "message.noted", Entity("message", note.pk), None, _sent(note))
        return note, created


def _advances(old: str | None, new: str, unknown: bool = False) -> bool:
    """¿Es `new` un paso adelante de `old`? Saltar pasos vale; `FAILED` solo desde antes de
    entregar y no se abandona, salvo un `FAILED` con `PROVIDER_UNKNOWN` (`unknown`), que un estado
    tardío de entrega reconcilia. Lo mismo que el disparador de la base."""
    if old == Delivery.FAILED and unknown:
        return new in (Delivery.SENT, Delivery.DELIVERED, Delivery.READ)
    if old is None or old == Delivery.FAILED or new == old:
        return False
    if new == Delivery.FAILED:
        return old in (Delivery.QUEUED, Delivery.SENDING, Delivery.SENT)
    return ORDER.index(Delivery(new)) > ORDER.index(Delivery(old))


def advance_delivery(
    ctx: TenantContext,
    *,
    message_id: UUID,
    status: str,
    at: dt.datetime | None = None,
    reason: str | None = None,
) -> Message:
    """Hace avanzar el estado de entrega de un mensaje saliente y fija su marca de tiempo (`at`, o
    ahora). Un estado que no es un paso adelante (repetido, tardío o tras `FAILED`) se ignora: ni
    error ni cambio ni auditoría. Audita `message.delivery_changed`, con el `reason` (un motivo
    corto de `channels.REASONS`, solo con `FAILED`) en los metadatos. `ValueError` si el estado no
    existe, el motivo no vale o el mensaje no es saliente; `DoesNotExist`."""
    alias = require_scope(ctx)
    message_id = _uuid(message_id, "mensaje")
    if not isinstance(status, str) or status not in Delivery.values:
        raise ValueError("estado de entrega: uno de " + ", ".join(Delivery.values))
    if at is not None and (not isinstance(at, dt.datetime) or at.utcoffset() is None):
        raise ValueError("fecha: con zona horaria")
    if reason is not None and (status != Delivery.FAILED or reason not in REASONS):
        raise ValueError("motivo: uno de " + ", ".join(REASONS) + ", solo con FAILED")
    with transaction.atomic(using=alias):
        found = Message.objects.using(alias).select_for_update(no_key=True)
        message: Message = found.get(pk=message_id)
        if message.direction != "OUTBOUND":
            raise ValueError("solo lo saliente tiene estado de entrega")
        late = message.provider_error_code == "PROVIDER_UNKNOWN"
        if not _advances(message.delivery_status, status, late):
            return message
        old, stamp = message.delivery_status, STAMPS[status]
        # Condicional: el disparador de la base lanza `check_violation` ante un estado atrasado, y
        # eso abortaría la transacción de quien llama. Con el bloqueo no debería pasar nunca; el
        # `WHERE` lo hace imposible aunque alguien quite el bloqueo.
        fields = {stamp: at or timezone.now(), "provider_error_code": reason}
        if late:
            fields["failed_at"] = None  # no falló: salió (lo reconcilia un estado tardío)
        moved = (
            Message.objects.using(alias)
            .filter(pk=message.pk, delivery_status=old)
            .update(delivery_status=status, **fields)
        )
        message.refresh_from_db(using=alias)
        if moved:
            changes = {"delivery_status": [old, status]}
            why = None if reason is None else {"reason": reason}
            why = {"reconciled": "PROVIDER_UNKNOWN"} if late else why
            record(ctx, "message.delivery_changed", Entity("message", message.pk), changes, why)
            emit(  # una pista para la pantalla: solo ids
                ctx, "message.updated", aggregate_type="message", aggregate_id=message.pk,
                payload={"conversation_id": str(message.conversation_id)},
            )  # fmt: skip
        return message


def _fail_from(ctx: TenantContext, message_id: UUID, reason: str, was: str) -> Message | None:
    alias = require_scope(ctx)
    message_id = _uuid(message_id, "mensaje")
    with transaction.atomic(using=alias):
        locked = Message.objects.using(alias).select_for_update(no_key=True)
        if not locked.filter(pk=message_id, delivery_status=was).exists():
            return None  # ya salió, ya se cerró o no existe: no se toca
        return advance_delivery(ctx, message_id=message_id, status=Delivery.FAILED, reason=reason)


def fail_queued(ctx: TenantContext, *, message_id: UUID, reason: str) -> Message | None:
    """Deja `FAILED` un mensaje que sigue `QUEUED` (los reintentos del despachador se agotaron)."""
    return _fail_from(ctx, message_id, reason, "QUEUED")


def fail_sending(ctx: TenantContext, *, message_id: UUID, reason: str) -> Message | None:
    """Deja `FAILED` un mensaje que sigue `SENDING` (el barredor: venció el plazo sin saber si
    salió, `PROVIDER_UNKNOWN`). Uno que ya se cerró, o que no existe, no se toca."""
    return _fail_from(ctx, message_id, reason, "SENDING")


def _assign(
    ctx: TenantContext, alias: str, convo: Conversation, user_id: UUID | None, reason: str,
    expected: int | None, *, only_free: bool = False,
) -> Conversation:  # fmt: skip
    """Cambia el responsable de la conversación (ya bloqueada): el único punto que escribe la
    asignación (ADR-007). El `UPDATE` es condicional a la versión que se leyó (y, al tomar, a que
    siga sin responsable), así que dos a la vez no pisan: una gana, la otra `AssignmentConflict`.
    Deja la fila inmutable de `conversation_assignments`, la auditoría y el evento, solo con ids.
    Soltar una `OPEN` la devuelve a la cola (`PENDING`, D-INB-1)."""
    if convo.status == Status.CLOSED:
        raise ConversationClosed
    if (expected is not None and expected != convo.assignment_version) or (
        only_free and convo.assigned_user_id is not None
    ):
        raise AssignmentConflict
    old, now = convo.assigned_user_id, timezone.now()
    status = Status.PENDING if user_id is None and convo.status == Status.OPEN else convo.status
    same = Conversation.objects.using(alias).filter(
        pk=convo.pk, assignment_version=convo.assignment_version
    )
    moved = (same.filter(assigned_user_id__isnull=True) if only_free else same).update(
        assigned_user_id=user_id, assigned_at=None if user_id is None else now, status=status,
        assignment_version=F("assignment_version") + 1, version=F("version") + 1, updated_at=now,
    )  # fmt: skip
    if not moved:
        raise AssignmentConflict
    ConversationAssignment.objects.using(alias).create(
        conversation_id=convo.pk, previous_team_id=convo.assigned_team_id, previous_user_id=old,
        new_team_id=convo.assigned_team_id, new_user_id=user_id, reason_code=reason,
        assigned_by_user_id=ctx.user_id,
    )  # fmt: skip
    changes = {"assigned_user_id": [str(old) if old else None, str(user_id) if user_id else None]}
    if status != convo.status:
        changes["status"] = [convo.status, status]
    record(ctx, "conversation.assigned", Entity("conversation", convo.pk, convo.number), changes,
           {"reason": reason})  # fmt: skip
    ids = {"previous_user_id": old, "user_id": user_id}
    emit(
        ctx, "conversation.assigned", aggregate_type="conversation", aggregate_id=convo.pk,
        payload={key: str(value) if value else None for key, value in ids.items()},
    )  # fmt: skip
    convo.refresh_from_db(using=alias)
    return convo


def _assigner(
    ctx: TenantContext, alias: str, conversation_id: UUID, *, recheck: bool = True
) -> Conversation:
    """El actor tiene `conversations.assign` y ve la conversación (`conversations.view` y su
    alcance); si no la ve, el mismo `DoesNotExist` que una desconocida. La devuelve bloqueada.
    Se mira antes de bloquear (lo que la persona veía) y, con `recheck`, otra vez con la fila
    bloqueada: transferir o soltar lo que otra persona acaba de llevarse de su alcance sería
    saltarse el alcance. Al tomar no hace falta: si alguien se la llevó, el conflicto lo dice (y
    quien nunca la vio ya recibió el 404 de antes)."""
    conversation_id = _uuid(conversation_id, "conversación")
    ectx = _allowed(ctx, "conversations.assign")
    seen = Conversation.objects.using(alias).filter(pk=conversation_id).first()
    if seen is None:
        raise Conversation.DoesNotExist("Conversation matching query does not exist.")
    _in_scope(ectx, "conversations.view", seen)
    convo = _locked(alias, conversation_id)
    if recheck:
        _in_scope(ectx, "conversations.view", convo)
    return convo


def _version(value: Any) -> int | None:
    if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 1):
        raise ValueError("versión: un entero desde 1")
    return value


def take_conversation(
    ctx: TenantContext, *, conversation_id: UUID, expected_version: int | None = None
) -> Conversation:
    """El actor toma una conversación que nadie tiene (`TAKE`). `AssignmentConflict` si otra
    persona la tomó antes o cambió la versión; `ConversationClosed`; `DoesNotExist`;
    `AccessDenied` sin `conversations.assign`."""
    alias, expected = require_scope(ctx), _version(expected_version)
    with transaction.atomic(using=alias):
        convo = _assigner(ctx, alias, conversation_id, recheck=False)
        assert ctx.user_id is not None  # noqa: S101 — `_allowed` ya exigió un miembro
        return _assign(ctx, alias, convo, ctx.user_id, "TAKE", expected, only_free=True)


def transfer_conversation(
    ctx: TenantContext, *, conversation_id: UUID, user_id: UUID, expected_version: int | None = None
) -> Conversation:
    """Pasa la conversación a otra persona (`TRANSFER`; `MANUAL` si no tenía responsable). Quien
    recibe es miembro activo con `conversations.view`: si no, `BadReference("user_id")`, con el
    mismo error para todo caso (no se revela quién existe). Ya la tiene: `AssignmentConflict`."""
    alias, expected = require_scope(ctx), _version(expected_version)
    target = _uuid(user_id, "persona")
    with transaction.atomic(using=alias):
        convo = _assigner(ctx, alias, conversation_id)
        if not (
            lock_active_member(target)
            and member_holds(execution_context(ctx), target, "conversations.view")
        ):
            raise BadReference("user_id")
        if convo.assigned_user_id == target:
            raise AssignmentConflict
        reason = "TRANSFER" if convo.assigned_user_id else "MANUAL"
        return _assign(ctx, alias, convo, target, reason, expected)


def release_conversation(
    ctx: TenantContext, *, conversation_id: UUID, expected_version: int | None = None
) -> Conversation:
    """Deja la conversación sin responsable (`MANUAL`); el equipo, si lo hay, la conserva.
    `AssignmentConflict` si ya no tiene responsable."""
    alias, expected = require_scope(ctx), _version(expected_version)
    with transaction.atomic(using=alias):
        convo = _assigner(ctx, alias, conversation_id)
        if convo.assigned_user_id is None:
            raise AssignmentConflict
        return _assign(ctx, alias, convo, None, "MANUAL", expected)


def claim_outbound(ctx: TenantContext, *, message_id: UUID) -> Claim | None:
    """Reclama un mensaje saliente `QUEUED` (F7-05b): `QUEUED` → `SENDING` y devuelve lo que el
    adaptador necesita. Con la conversación bloqueada y la cuenta con `FOR SHARE` (desconectarla
    espera; dos envíos de una cuenta no se serializan) re-evalúa la política justo antes de enviar:
    si ya no se puede, queda `FAILED` con el motivo y devuelve `None`. Idempotente: lo que no está
    `QUEUED`, ajeno, inexistente, aún dentro de su espera (`not_before`: un evento reentregado no
    envía antes) o que otro despachador reclama a la vez da `None`."""
    alias = require_scope(ctx)
    message_id = _uuid(message_id, "mensaje")
    with transaction.atomic(using=alias):
        waiting = Q(not_before__isnull=True) | Q(not_before__lte=timezone.now())  # tras su espera
        queued = Message.objects.using(alias).filter(
            waiting, pk=message_id, direction="OUTBOUND", delivery_status="QUEUED"
        )
        row = queued.values(
            "conversation_id", "channel_account_id", "conversation__contact_id"
        ).first()
        if row is None:
            return None
        # Orden de bloqueos: cuenta → contacto → conversación (como `ingress.seen`, `receive` y
        # `_open`): sin ciclos, y un STOP en registro y este reclamo se serializan.
        with connections[alias].cursor() as cursor:
            cursor.execute(
                "SELECT 1 FROM channel_accounts WHERE id = %s FOR SHARE",
                [row["channel_account_id"]],
            )
            cursor.execute(
                "SELECT 1 FROM contacts WHERE id = %s FOR SHARE", [row["conversation__contact_id"]]
            )
        convo = _locked(alias, row["conversation_id"])  # el mismo orden que `record_outbound`
        message = queued.select_for_update(no_key=True, skip_locked=True).first()
        if message is None:  # otro despachador lo reclama, o ya salió
            return None
        account = ChannelAccount.objects.using(alias).get(pk=convo.channel_account_id)
        # el de encolar; sin él (un mensaje anterior a F7-08b), el más estricto
        purpose = (message.policy_decision or {}).get("purpose") or "FOLLOW_UP"
        decision = messaging_decision(convo, account=account, alias=alias, purpose=purpose)
        queued.update(policy_decision=decision.as_json())  # la última decisión queda en el mensaje
        if decision.reason is not None:
            advance_delivery(
                ctx, message_id=message.pk, status=Delivery.FAILED, reason=decision.reason
            )
            return None
        shown = None
        if message.file_id is not None:  # D-F7-04: solo `CLEAN`, o `PENDING` si se decidió
            file = File.objects.using(alias).get(pk=message.file_id)
            pending = file.scan_status == "PENDING" and settings.MEDIA_SEND_UNSCANNED
            if file.scan_status != "CLEAN" and not pending:
                advance_delivery(
                    ctx, message_id=message.pk, status=Delivery.FAILED, reason="MEDIA_NOT_SCANNED"
                )
                return None
            label = outbound_media.LABELS[message.message_type]
            shown = Media(
                message.message_type, file.storage_key, file.mime_type, file.sha256,
                file.original_name, None if message.body == label else message.body,
            )  # fmt: skip
        phone = Contact.objects.using(alias).filter(pk=convo.contact_id)
        live = Credential.objects.using(alias).filter(
            pk=account.credential_id, owner_id=account.pk, revoked_at__isnull=True
        )  # viva y de esta cuenta
        advance_delivery(ctx, message_id=message.pk, status=Delivery.SENDING)
        return Claim(
            message_id=message.pk, account_id=account.pk, platform=account.platform,
            phone_number_id=account.external_account_id, body=message.body,
            recipient=phone.values_list("primary_phone_e164", flat=True).first() or "",
            credential=live.first() if account.credential_id else None, media=shown,
        )  # fmt: skip


def release_outbound(ctx: TenantContext, *, message_id: UUID, wait: dt.timedelta) -> int | None:
    """Devuelve a la cola un `SENDING` que seguro no salió (nunca tras un resultado ambiguo), cuenta
    el intento y no lo deja reintentar antes de `wait` (`not_before`: el barredor de la cola no lo
    reencola antes). Devuelve los intentos, o `None` si ya no estaba `SENDING`."""
    alias = require_scope(ctx)
    message_id = _uuid(message_id, "mensaje")
    with transaction.atomic(using=alias):
        held = Message.objects.using(alias).filter(pk=message_id, delivery_status="SENDING")
        if not held.select_for_update(no_key=True).exists():
            return None
        held.update(
            delivery_status=Delivery.QUEUED, sending_at=None, not_before=timezone.now() + wait,
            dispatch_attempts=F("dispatch_attempts") + 1,
        )  # fmt: skip
        changes = {"delivery_status": [Delivery.SENDING, Delivery.QUEUED]}
        record(ctx, "message.delivery_changed", Entity("message", message_id), changes, None)
        attempts: int = Message.objects.using(alias).get(pk=message_id).dispatch_attempts
        return attempts


def settle_outbound(ctx: TenantContext, *, message_id: UUID, result: SendResult) -> Message | None:
    """Cierra un `SENDING` con la respuesta del adaptador: `SENT` (y `DELIVERED` si el canal lo
    entregó) o `FAILED` con un motivo de `channels.REASONS` (uno raro: `ADAPTER_REJECTED`)."""
    alias = require_scope(ctx)
    message_id = _uuid(message_id, "mensaje")
    with transaction.atomic(using=alias):
        held = Message.objects.using(alias).filter(pk=message_id, delivery_status="SENDING")
        if not held.select_for_update(no_key=True).exists():
            return None
        if not result.ok:
            reason = result.reason if result.reason in REASONS else "ADAPTER_REJECTED"
            return advance_delivery(
                ctx, message_id=message_id, status=Delivery.FAILED, reason=reason
            )
        held.update(external_message_id=result.external_id)
        sent = advance_delivery(ctx, message_id=message_id, status=Delivery.SENT)
        if result.delivered:
            return advance_delivery(ctx, message_id=message_id, status=Delivery.DELIVERED)
        return sent
