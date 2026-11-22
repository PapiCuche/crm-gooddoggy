from typing import Any

from django.conf import settings
from django.urls import ResolverMatch, URLPattern, path, register_converter
from django.urls.resolvers import RoutePattern

from apps.conversations.api import (
    assignment,
    attention,
    channels,
    conversations,
    media,
    messages,
    quick_replies,
)
from apps.conversations.api.quick_reply_admin import ManagedRepliesView, ManagedReplyView
from apps.conversations.api.quick_reply_category_admin import (
    ManagedCategoriesView,
    ManagedCategoryView,
)


class SandboxOnly(URLPattern):
    """F1-14: la ruta existe solo con `SANDBOX_INBOUND_ENABLED` (se lee en cada petición). Apagada,
    no resuelve: la respuesta es la de una ruta desconocida, sin vista, sesión ni cuerpo."""

    def resolve(self, path: str) -> ResolverMatch | None:
        return super().resolve(path) if settings.SANDBOX_INBOUND_ENABLED else None


def hide_sandbox(endpoints: list[Any], **kwargs: Any) -> list[Any]:
    """Hook de drf-spectacular: apagada, `/api/schema/` tampoco anuncia la entrada Sandbox."""
    if settings.SANDBOX_INBOUND_ENABLED:
        return endpoints
    return [e for e in endpoints if getattr(e[3], "cls", None) is not messages.SandboxInboundView]


class SocialPlatform:
    """`messenger` o `instagram` en la ruta; la vista recibe la plataforma (F7b-06a)."""

    regex = "messenger|instagram"

    def to_python(self, value: str) -> str:
        return value.upper()

    def to_url(self, value: str) -> str:
        return value.lower()


register_converter(SocialPlatform, "social")

urlpatterns = [
    path("conversations/", conversations.ConversationsView.as_view(), name="conversations"),
    path(
        "conversations/unanswered/",
        conversations.UnansweredCountView.as_view(),
        name="conversations-unanswered",
    ),
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
        "conversations/<uuid:conversation_id>/media/",
        media.SendMediaView.as_view(),
        name="conversation-media",
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
        "conversations/<uuid:conversation_id>/quick-replies/",
        quick_replies.QuickRepliesView.as_view(),
        name="conversation-quick-replies",
    ),
    path(
        "conversations/<uuid:conversation_id>/quick-replies/<uuid:quick_reply_id>/render/",
        quick_replies.RenderView.as_view(),
        name="conversation-quick-reply-render",
    ),
    path(
        "conversations/<uuid:conversation_id>/assign/",
        assignment.AssignView.as_view(),
        name="conversation-assign",
    ),
    path(
        "conversations/<uuid:conversation_id>/human-request/",
        attention.HumanRequestView.as_view(),
        name="conversation-human-request",
    ),
    path(  # F8-13: excluirla de la IA o volver a incluirla
        "conversations/<uuid:conversation_id>/ai-exclusion/",
        attention.AiExclusionView.as_view(),
        name="conversation-ai-exclusion",
    ),
    path("quick-replies/", ManagedRepliesView.as_view(), name="quick-replies"),
    path("quick-replies/<uuid:quick_reply_id>/", ManagedReplyView.as_view(), name="quick-reply"),
    path("quick-reply-categories/", ManagedCategoriesView.as_view(), name="quick-reply-categories"),
    path(
        "quick-reply-categories/<uuid:category_id>/",
        ManagedCategoryView.as_view(),
        name="quick-reply-category",
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
    path(
        "channels/<social:platform>/", channels.SocialConnectView.as_view(), name="social-connect"
    ),
    path(
        "channels/<social:platform>/<uuid:account_id>/",
        channels.SocialDisconnectView.as_view(),
        name="social-disconnect",
    ),
    path(
        "channels/<social:platform>/<uuid:account_id>/token/",
        channels.SocialRotateTokenView.as_view(),
        name="social-rotate-token",
    ),
    SandboxOnly(
        RoutePattern("channels/sandbox/inbound/", name="sandbox-inbound", is_endpoint=True),
        messages.SandboxInboundView.as_view(),
        name="sandbox-inbound",
    ),
]
