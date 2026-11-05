"""Política mínima de envío de F6-04a (ADR-010): cuándo una respuesta no puede ponerse en cola.

Solo lo que se decide con el estado de la conversación y del canal; la ventana de 24 horas, las
plantillas y los consentimientos son del despachador (F6-04b y la Fase 7). Una conversación
`RESOLVED` admite respuestas (D-F6-11): puede reabrirse por un mensaje del cliente y quien la
resolvió puede escribir una despedida; las `CLOSED`, no (un mensaje entrante abre otra).
"""

from apps.conversations.models import Status

UNAVAILABLE = ("DISCONNECTED", "ERROR", "PENDING")  # estados de `ChannelAccount`


class ConversationClosed(Exception):
    """La conversación está CLOSED: no admite más respuestas (un mensaje entrante abre otra)."""


class ChannelUnavailable(Exception):
    """El canal de la conversación no está conectado (`DISCONNECTED`, `ERROR` o `PENDING`)."""


def check_send(conversation_status: str, account_status: str) -> None:
    """Lanza `ConversationClosed` o `ChannelUnavailable` si no se puede responder ahora."""
    if conversation_status == Status.CLOSED:
        raise ConversationClosed
    if account_status in UNAVAILABLE:
        raise ChannelUnavailable
