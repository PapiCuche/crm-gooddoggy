from django.urls import path

from apps.conversations.api import assignment, channels, conversations, messages

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
        "conversations/<uuid:conversation_id>/notes/",
        messages.NotesView.as_view(),
        name="conversation-notes",
    ),
    path(
        "conversations/<uuid:conversation_id>/assign/",
        assignment.AssignView.as_view(),
        name="conversation-assign",
    ),
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
        "channels/sandbox/inbound/",
        messages.SandboxInboundView.as_view(),
        name="sandbox-inbound",
    ),
]
