from django.apps import AppConfig


class ConversationsConfig(AppConfig):
    name = "apps.conversations"
    label = "conversations"

    def ready(self) -> None:
        from apps.access.scopes import register
        from apps.conversations.models import Conversation, Message
        from apps.conversations.scopes import MessageVisible, Visible

        register(Conversation, Visible())  # propias, de su equipo y sin asignar (03 §H)
        register(Message, MessageVisible())  # los de las conversaciones que se ven
