from typing import Any

from django.apps import AppConfig
from django.db import connections
from django.db.models.signals import post_migrate

PARTITION_MONTHS = 12  # horizonte de `ai_runs` y `ai_actions`: mes actual + 12, como `audit_logs`
PARTITIONED = ("ai_runs", "ai_actions")  # F8-08b


def ensure_partitions(sender: Any, using: str = "default", **kwargs: Any) -> None:
    """Tras cada `migrate` (job de migraciones, crm_migrator): crea las particiones que falten.
    Sin partición, `records.save` falla y el copiloto no deja registro."""
    with connections[using].cursor() as cursor:
        for table in PARTITIONED:
            cursor.execute("SELECT to_regprocedure(%s)", [f"public.{table}_ensure_partitions(int)"])
            if cursor.fetchone()[0] is not None:
                cursor.execute(f"SELECT public.{table}_ensure_partitions(%s)", [PARTITION_MONTHS])


class AiAgentsConfig(AppConfig):
    """`ai_agents` (L4, ADR-034 §2): registra las tools al arrancar y cierra el registro."""

    name = "apps.ai_agents"
    label = "ai_agents"

    def ready(self) -> None:
        from apps.ai_agents import registry, tools  # noqa: F401, PLC0415
        from apps.ai_agents.tools import stock  # noqa: PLC0415

        registry.freeze()
        stock.few_units()  # F8-07d: un umbral inválido no arranca (y en cada llamada, sin leer)
        post_migrate.connect(ensure_partitions, sender=self, dispatch_uid="ai_agents_partitions")
