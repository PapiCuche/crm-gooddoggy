from django.apps import AppConfig


class ConversationsConfig(AppConfig):
    name = "apps.conversations"
    label = "conversations"

    def ready(self) -> None:
        from apps.access.scopes import Everyone, register
        from apps.conversations.models import Conversation, Message, QuickReply, QuickReplyCategory
        from apps.conversations.scopes import MessageVisible, Visible
        from apps.conversations.seed import ensure_quick_reply_categories
        from apps.provisioning.seeds import register_seed

        register(Conversation, Visible())  # propias, de su equipo y sin asignar (03 §H)
        register(Message, MessageVisible())  # los de las conversaciones que se ven
        for model in (QuickReplyCategory, QuickReply):  # F6-08a1: visibles para toda la org
            register(model, Everyone())
        register_seed(ensure_quick_reply_categories)  # las organizaciones nuevas, con categorías
