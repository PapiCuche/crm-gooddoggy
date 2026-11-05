"""Lecturas del Inbox, por organización (RLS y `TenantManager`). El permiso y el alcance los aplica
quien las sirve (`HasPermission` y `ScopeFilter`)."""

from django.db.models import QuerySet

from apps.conversations.models import Conversation


def conversations() -> QuerySet[Conversation]:
    """Las conversaciones, cada una con su cuenta de canal en la misma consulta. El orden lo pone
    el paginador (lo más reciente primero)."""
    return Conversation.objects.select_related("channel_account")
