"""Lecturas del Inbox, por organización (RLS y `TenantManager`). El permiso y el alcance los aplica
quien las sirve (`HasPermission` y `ScopeFilter`)."""

from django.db.models import QuerySet

from apps.conversations.models import Conversation, Message


def conversations() -> QuerySet[Conversation]:
    """Las conversaciones, cada una con su cuenta de canal en la misma consulta. El orden lo pone
    el paginador (lo más reciente primero)."""
    return Conversation.objects.select_related("channel_account")


def messages(conversation_id: object, *, notes: bool) -> QuerySet[Message]:
    """Los mensajes de una conversación. Las notas internas solo salen con `notes=True`: quien
    no puede verlas (`messages.note` y su alcance) no las recibe, ni cuentan en la página."""
    found = Message.objects.filter(conversation_id=conversation_id)
    return found if notes else found.exclude(direction="INTERNAL")
