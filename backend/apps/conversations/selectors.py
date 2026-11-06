"""Lecturas del Inbox, por organización (RLS y `TenantManager`). El permiso y el alcance los aplica
quien las sirve (`HasPermission` y `ScopeFilter`)."""

from django.db.models import OuterRef, QuerySet, Subquery

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
    live = Credential.objects.filter(pk=OuterRef("credential_id"), revoked_at__isnull=True)
    found: QuerySet[ChannelAccount] = ChannelAccount.objects.filter(deleted_at__isnull=True)
    return found.annotate(token_last_four=Subquery(live.values("last_four")[:1]))
