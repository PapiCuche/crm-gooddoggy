from django.conf import settings
from django.urls import ResolverMatch, URLPattern, path
from django.urls.resolvers import RoutePattern

from apps.conversations.api import assignment, channels, conversations, media, messages


class SandboxOnly(URLPattern):
    """F1-14: la ruta existe solo con `SANDBOX_INBOUND_ENABLED` (se lee en cada petición). Apagada,
    no resuelve: la respuesta es la de una ruta desconocida, sin vista, sesión ni cuerpo."""

    def resolve(self, path: str) -> ResolverMatch | None:
        return super().resolve(path) if settings.SANDBOX_INBOUND_ENABLED else None


urlpatterns = [
    path("conversations/", conversations.ConversationsView.as_view(), name="conversations"),
    path(
        "conversations/<uuid:conversation_id>/",
        conversations.ConversationView.as_view(),
        name="conversation",
    ),
    path(
        "conversations/<uuid:conversation_id>/messages/",
        messages.MessagesView.as_view(),
        name="conversation-messages",
    ),
    path(
        "conversations/<uuid:conversation_id>/attachments/<uuid:attachment_id>/url/",
        media.AttachmentUrlView.as_view(),
        name="conversation-attachment-url",
    ),
    path(
        "conversations/<uuid:conversation_id>/messaging-policy/",
        messages.MessagingPolicyView.as_view(),
        name="conversation-messaging-policy",
    ),
    path(
        "conversations/<uuid:conversation_id>/notes/",
        messages.NotesView.as_view(),
        name="conversation-notes",
    ),
    path(
        "conversations/<uuid:conversation_id>/assign/",
        assignment.AssignView.as_view(),
        name="conversation-assign",
    ),
    path("channels/", channels.ChannelsView.as_view(), name="channels"),
    path("channels/<uuid:account_id>/", channels.ChannelView.as_view(), name="channel"),
    path("channels/whatsapp/", channels.ConnectView.as_view(), name="whatsapp-connect"),
    path(
        "channels/whatsapp/<uuid:account_id>/",
        channels.DisconnectView.as_view(),
        name="whatsapp-disconnect",
    ),
    path(
        "channels/whatsapp/<uuid:account_id>/token/",
        channels.RotateTokenView.as_view(),
        name="whatsapp-rotate-token",
    ),
    SandboxOnly(
        RoutePattern("channels/sandbox/inbound/", name="sandbox-inbound", is_endpoint=True),
        messages.SandboxInboundView.as_view(),
        name="sandbox-inbound",
    ),
]
