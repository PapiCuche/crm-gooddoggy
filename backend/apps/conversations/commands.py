"""Comandos del Inbox (F6-01c; E06, 02 §E.6, ADR-007, 010): el único escritor de las tablas.

Solo la API (F6-02) los importa (contrato de import-linter) y comprueba el permiso de la ruta antes
de leer. Estos (una apertura, un mensaje entrante, el estado de entrega que informa el canal, la
cuenta de canal) no reciben el id de una conversación en nombre de una persona; los que sí
(responder y anotar, F6-01d) comprueban además el permiso y el alcance. Validan los tipos exactos
(`ValueError`, nunca `TypeError` ni `IntegrityError`) y auditan solo identificadores, estados y
longitudes: **ningún texto de mensaje** llega a la auditoría. La organización sale siempre del
contexto, nunca de un dato recibido. Un mensaje entrante es idempotente por `external_message_id`.

Crear una conversación bloquea el contacto (`FOR NO KEY UPDATE`): dos mensajes entrantes a la vez
de un mismo contacto se serializan y la segunda encuentra la conversación de la primera; el índice
parcial único de la base es la red de seguridad. Un estado de entrega tardío o repetido no
retrocede el mensaje y no es un error (ADR-010, §E.6).
"""

import datetime as dt
from typing import Any
from uuid import UUID

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.audit.services import Entity, record
from apps.contacts.models import Contact
from apps.conversations.models import (
    BODY_MAX,
    AttentionMode,
    ChannelAccount,
    Conversation,
    Delivery,
    Message,
    Platform,
    Status,
)
from core.sequences import allocate
from core.tenancy.context import TenantContext
from core.tenancy.scope import require_scope

REOPEN_HOURS = 72  # D-INB-2: una RESOLVED recibe mensajes hasta 72 h después; luego se cierra
KEY, ORDER = "CONV", (Delivery.QUEUED, Delivery.SENT, Delivery.DELIVERED, Delivery.READ)
STAMPS = {"SENT": "sent_at", "DELIVERED": "delivered_at", "READ": "read_at", "FAILED": "failed_at"}
EXTERNAL_MAX = 200
NAME_MAX = 100
EDITABLE = ("name", "display_identifier")  # la plataforma y el id externo no cambian


class BadReference(ValueError):
    """Una referencia que no sirve (el contacto o la cuenta no son de la organización o están
    borrados). `field` dice cuál."""

    def __init__(self, field: str) -> None:
        super().__init__(field)
        self.field = field


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
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError("texto: no vacío y sin caracteres nulos")
    if len(value) > BODY_MAX:
        raise ValueError(f"texto: hasta {BODY_MAX} caracteres")
    return value


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
) -> tuple[Message, bool]:
    """Registra un mensaje del cliente, idempotente por `(cuenta, external_message_id)`: repetirlo
    devuelve el mismo y no cambia nada. Abre o reutiliza la conversación (`PENDING` si es nueva),
    pone `last_inbound_at` y `last_message_at` y, según D-INB-2, pasa a `OPEN` una conversación en
    `WAITING_CUSTOMER` o `RESOLVED` dentro del plazo. Audita `message.received`."""
    alias = require_scope(ctx)
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
        try:
            with transaction.atomic(using=alias):
                message: Message = Message.objects.using(alias).create(
                    conversation_id=convo.pk, channel_account_id=account_id, direction="INBOUND",
                    sender_type="CONTACT", body=text, external_message_id=external,
                )  # fmt: skip
        except IntegrityError as error:
            if "messages_external_uq" not in str(error):
                raise
            return same.get(), False  # el mismo mensaje, a la vez
        convo.last_inbound_at = convo.last_message_at = now
        if convo.status in (Status.WAITING_CUSTOMER, Status.RESOLVED):
            _status(  # con las marcas del mensaje: `update_fields` no las guardaría de otro modo
                ctx, alias, convo, Status.OPEN, "conversation.reopened", resolved_at=None,
                last_inbound_at=now, last_message_at=now,
            )  # fmt: skip
        else:
            convo.save(
                using=alias, update_fields=["last_inbound_at", "last_message_at", "updated_at"]
            )
        record(ctx, "message.received", Entity("message", message.pk), None, _sent(message))
        return message, True


def _advances(old: str | None, new: str) -> bool:
    """¿Es `new` un paso adelante de `old`? Saltar pasos vale; `FAILED` solo desde antes de
    entregar y no se abandona. Lo mismo que el disparador de la base."""
    if old is None or old == Delivery.FAILED or new == old:
        return False
    if new == Delivery.FAILED:
        return old in (Delivery.QUEUED, Delivery.SENT)
    return ORDER.index(Delivery(new)) > ORDER.index(Delivery(old))


def advance_delivery(
    ctx: TenantContext, *, message_id: UUID, status: str, at: dt.datetime | None = None
) -> Message:
    """Hace avanzar el estado de entrega de un mensaje saliente y fija su marca de tiempo (`at`, o
    ahora). Un estado que no es un paso adelante (repetido, tardío o tras `FAILED`) se ignora: ni
    error ni cambio ni auditoría. Audita `message.delivery_changed`. `ValueError` si el estado no
    existe o el mensaje no es saliente; `DoesNotExist`."""
    alias = require_scope(ctx)
    message_id = _uuid(message_id, "mensaje")
    if not isinstance(status, str) or status not in Delivery.values:
        raise ValueError("estado de entrega: uno de " + ", ".join(Delivery.values))
    if at is not None and (not isinstance(at, dt.datetime) or at.utcoffset() is None):
        raise ValueError("fecha: con zona horaria")
    with transaction.atomic(using=alias):
        found = Message.objects.using(alias).select_for_update(no_key=True)
        message: Message = found.get(pk=message_id)
        if message.direction != "OUTBOUND":
            raise ValueError("solo lo saliente tiene estado de entrega")
        if not _advances(message.delivery_status, status):
            return message
        old, stamp = message.delivery_status, STAMPS[status]
        # Condicional: el disparador de la base lanza `check_violation` ante un estado atrasado, y
        # eso abortaría la transacción de quien llama. Con el bloqueo no debería pasar nunca; el
        # `WHERE` lo hace imposible aunque alguien quite el bloqueo.
        moved = (
            Message.objects.using(alias)
            .filter(pk=message.pk, delivery_status=old)
            .update(delivery_status=status, **{stamp: at or timezone.now()})
        )
        message.refresh_from_db(using=alias)
        if moved:
            changes = {"delivery_status": [old, status]}
            record(ctx, "message.delivery_changed", Entity("message", message.pk), changes)
        return message
