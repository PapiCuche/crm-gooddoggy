"""Lecturas del Inbox, por organización (RLS y `TenantManager`). El permiso y el alcance los aplica
quien las sirve (`HasPermission` y `ScopeFilter`)."""

from django.db.models import OuterRef, QuerySet, Subquery
from django.utils import timezone

from apps.contacts.models import Contact
from apps.conversations import policy
from apps.conversations.models import ChannelAccount, Conversation, Credential, Message


def conversations() -> QuerySet[Conversation]:
    """Las conversaciones, cada una con su cuenta de canal en la misma consulta. El orden lo pone
    el paginador (lo más reciente primero)."""
    return Conversation.objects.select_related("channel_account")


def messages(conversation_id: object, *, notes: bool) -> QuerySet[Message]:
    """Los mensajes de una conversación; las notas internas, solo con `notes=True`."""
    found = Message.objects.filter(conversation_id=conversation_id)
    return found if notes else found.exclude(direction="INTERNAL")


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
) -> policy.PolicyDecision:
    """La decisión de `policy.evaluate` para escribir ahora en la conversación, con los hechos de
    esta organización (RLS): la cuenta de la conversación, su contacto y `last_inbound_at`. Un
    contacto o una cuenta que no se ven niegan (cerrado ante la duda). Con `alias`, en la
    transacción de quien llama (el comando que ya bloqueó la conversación); con `account`, la que
    ya leyó (y bloqueó) quien llama."""
    accounts, contacts = ChannelAccount.objects, Contact.objects
    if alias is not None:
        accounts, contacts = accounts.using(alias), contacts.using(alias)  # type: ignore[assignment]
    account = account or accounts.filter(pk=convo.channel_account_id).first()
    blocked = contacts.filter(pk=convo.contact_id).values_list("is_blocked", flat=True).first()
    return policy.evaluate(
        conversation_status=convo.status,
        platform="" if account is None else account.platform,
        account_status="" if account is None else account.status,
        account_deleted=account is None or account.deleted_at is not None,
        contact_blocked=blocked is not False,  # sin contacto visible: bloqueado
        last_inbound_at=convo.last_inbound_at,
        now=timezone.now(),
    )
