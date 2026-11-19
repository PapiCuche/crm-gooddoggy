from django.apps import AppConfig


class AiAgentsConfig(AppConfig):
    """`ai_agents` (L4, ADR-034 §2): el copiloto y su Tool Registry. Las tools se registran al
    arrancar: una que no cumple el registro impide arrancar (F8-07a)."""

    name = "apps.ai_agents"
    label = "ai_agents"

    def ready(self) -> None:
        from apps.ai_agents import tools  # noqa: F401, PLC0415
