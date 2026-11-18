from typing import Any

from django.apps import AppConfig
from django.db import connections
from django.db.models.signals import post_migrate

PARTITION_MONTHS = 12  # horizonte de `ai_llm_calls`: mes actual + 12, como `audit_logs`
ENSURE = "public.ai_llm_calls_ensure_partitions(integer)"


def ensure_partitions(sender: Any, using: str = "default", **kwargs: Any) -> None:
    """Tras cada `migrate` (job de migraciones, crm_migrator; F8-05b1): crea las particiones que
    falten. Sin partición, `ai_budget_reserve` no reserva y no se llama al proveedor."""
    with connections[using].cursor() as cursor:
        cursor.execute("SELECT to_regprocedure(%s)", [ENSURE])
        if cursor.fetchone()[0] is not None:
            cursor.execute("SELECT public.ai_llm_calls_ensure_partitions(%s)", [PARTITION_MONTHS])


class AiGatewayConfig(AppConfig):
    name = "apps.ai_gateway"
    label = "ai_gateway"

    def ready(self) -> None:
        post_migrate.connect(ensure_partitions, sender=self, dispatch_uid="ai_llm_calls_partitions")
