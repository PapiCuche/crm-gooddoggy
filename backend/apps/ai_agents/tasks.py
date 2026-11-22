"""Tareas de `ai_agents` (F8-08j): la purga diaria del texto de los borradores (D-F8-05)."""

from apps.ai_agents import drafts
from core.tenancy.celery import platform_task


@platform_task(name="ai_agents.purge_suggestion_text", ignore_result=True)
def purge_suggestion_text() -> dict[str, int]:
    """Sin tenant al empezar; un scope por organización. Devuelve solo cuentas."""
    return drafts.purge()
