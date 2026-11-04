from django.apps import AppConfig


class ConversationsConfig(AppConfig):
    name = "apps.conversations"
    label = "conversations"

    def ready(self) -> None:
        from apps.access.scopes import register
        from apps.conversations.models import Conversation
        from apps.conversations.scopes import Visible

        register(Conversation, Visible())  # propias, de su equipo y sin asignar (03 §H)
