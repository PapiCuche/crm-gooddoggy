from django.urls import path

from apps.conversations.api import conversations, messages

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
        "channels/sandbox/inbound/",
        messages.SandboxInboundView.as_view(),
        name="sandbox-inbound",
    ),
]
