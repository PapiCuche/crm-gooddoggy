from django.apps import AppConfig


class AiAgentsConfig(AppConfig):
    """`ai_agents` (L4, ADR-034 §2): registra las tools al arrancar y cierra el registro."""

    name = "apps.ai_agents"
    label = "ai_agents"

    def ready(self) -> None:
        from apps.ai_agents import registry, tools  # noqa: F401, PLC0415

        registry.freeze()
