"""Lecturas del Inbox, por organización (RLS y `TenantManager`). El permiso y el alcance los aplica
quien las sirve (`HasPermission` y `ScopeFilter`)."""

from typing import Any
from uuid import UUID

from django.db.models import OuterRef, Q, QuerySet, Subquery
from django.utils import timezone

from apps.contacts.models import Contact
from apps.contacts.selectors import is_revoked
from apps.conversations import policy
from apps.conversations.media import EXTENSIONS
from apps.conversations.models import (
    ChannelAccount,
    Conversation,
    Credential,
    Message,
    MessageAttachment,
)
from apps.files.models import File

# El archivo de un adjunto `DONE` que se puede servir: el suyo (`message-media`), con el antivirus
# en `CLEAN` (D-F7-04: sin antivirus, nada) y un tipo del mapa canónico (el de los magic bytes).
SERVABLE = Q(purpose="message-media", scan_status="CLEAN", mime_type__in=EXTENSIONS)


def conversations() -> QuerySet[Conversation]:
    """Las conversaciones, cada una con su cuenta de canal en la misma consulta. El orden lo pone
    el paginador (lo más reciente primero)."""
    return Conversation.objects.select_related("channel_account")


def messages(conversation_id: object, *, notes: bool) -> QuerySet[Message]:
    """Los mensajes de una conversación; las notas internas, solo con `notes=True`."""
    found = Message.objects.filter(conversation_id=conversation_id)
    return found if notes else found.exclude(direction="INTERNAL")


def attachments(message_ids: list[UUID]) -> dict[UUID, dict[str, Any]]:
    """Los adjuntos de esos mensajes, por mensaje, en dos consultas: clase, estado y, solo con
    `DONE` y un archivo `message-media`, su tipo (el de los magic bytes) y tamaño. Nunca la clave,
    el id de Meta ni el intento. `available`: si la ruta de la URL lo serviría (sin mirar al
    contacto: eso lo añade quien lo muestra)."""
    rows = list(MessageAttachment.objects.filter(message_id__in=message_ids))
    done = [row.file_id for row in rows if row.download_status == "DONE"]
    mine = File.objects.filter(pk__in=done, purpose="message-media")
    shown = mine.annotate(ok=Q(SERVABLE)).values("id", "mime_type", "size_bytes", "ok")
    files = {f["id"]: f for f in shown}
    found = {}
    for row in rows:
        file = files.get(row.file_id) or {}
        found[row.message_id] = {
            "id": row.pk, "kind": row.kind, "status": row.download_status,
            "mime_type": file.get("mime_type"), "size_bytes": file.get("size_bytes"),
            "available": bool(file.get("ok")),
        }  # fmt: skip
    return found


def servable(conversation_id: UUID, attachment_id: UUID) -> tuple[MessageAttachment, File] | None:
    """El adjunto `DONE` de un mensaje entrante de esa conversación y su archivo servible
    (`SERVABLE`), o `None` por cualquier otra razón (sin distinguir cuál)."""
    inbound = Message.objects.filter(conversation_id=conversation_id, direction="INBOUND")
    row = MessageAttachment.objects.filter(
        pk=attachment_id, download_status="DONE", message_id__in=inbound.values("pk")
    ).first()  # fmt: skip
    file = None if row is None else File.objects.filter(SERVABLE, pk=row.file_id).first()
    return None if row is None or file is None else (row, file)


def channel_accounts() -> QuerySet[ChannelAccount]:
    """Las cuentas de canal vivas, cada una con `token_last_four` (el final de su credencial viva,
    `NULL` si no la tiene) en la misma consulta. Nunca se anota nada más de la credencial."""
    live = Credential.objects.filter(
        pk=OuterRef("credential_id"), owner_id=OuterRef("pk"), revoked_at__isnull=True
    )  # viva y de esta cuenta: una credencial ajena no se enseña
    found: QuerySet[ChannelAccount] = ChannelAccount.objects.filter(deleted_at__isnull=True)
    return found.annotate(token_last_four=Subquery(live.values("last_four")[:1]))


def messaging_decision(
    convo: Conversation,
    *,
    account: ChannelAccount | None = None,
    alias: str | None = None,
    purpose: str = policy.CUSTOMER_REPLY,
) -> policy.PolicyDecision:
    """La decisión de `policy.evaluate` para escribir ahora en la conversación, con los hechos de
    esta organización (RLS): la cuenta de la conversación, su contacto y `last_inbound_at`. Un
    contacto o una cuenta que no se ven niegan (cerrado ante la duda). Con `alias`, en la
    transacción de quien llama (el comando que ya bloqueó la conversación); con `account`, la que
    ya leyó (y bloqueó) quien llama. `purpose`: para qué se escribe (`policy.PURPOSES`); una
    respuesta al cliente no consulta el consentimiento."""
    accounts, contacts = ChannelAccount.objects, Contact.objects
    if alias is not None:
        accounts, contacts = accounts.using(alias), contacts.using(alias)  # type: ignore[assignment]
    account = account or accounts.filter(pk=convo.channel_account_id).first()
    blocked = contacts.filter(pk=convo.contact_id).values_list("is_blocked", flat=True).first()
    platform = "" if account is None else account.platform
    revoked = purpose != policy.CUSTOMER_REPLY and is_revoked(
        convo.contact_id, policy.CONSENT_CHANNELS.get(platform, "ANY"),
        policy.PURPOSES.get(purpose, purpose), alias=alias,
    )  # fmt: skip
    return policy.evaluate(
        conversation_status=convo.status,
        platform=platform,
        account_status="" if account is None else account.status,
        account_deleted=account is None or account.deleted_at is not None,
        contact_blocked=blocked is not False,  # sin contacto visible: bloqueado
        last_inbound_at=convo.last_inbound_at,
        now=timezone.now(),
        purpose=purpose,
        revoked=revoked,
    )
