from django.apps import AppConfig


class AiAgentsConfig(AppConfig):
    """`ai_agents` (L4, ADR-034 §2): registra las tools al arrancar y cierra el registro."""

    name = "apps.ai_agents"
    label = "ai_agents"

    def ready(self) -> None:
        from apps.ai_agents import context, registry, tools  # noqa: F401, PLC0415
        from apps.ai_agents.tools import stock  # noqa: PLC0415

        registry.freeze()
        stock.few_units()  # F8-07d: un umbral inválido no arranca (y en cada llamada, sin leer)
        context.limit()  # F8-08d: igual con el número de mensajes del contexto
