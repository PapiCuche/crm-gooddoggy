from typing import Any

from django.apps import AppConfig
from django.conf import settings
from django.db import OperationalError, connections, transaction
from django.db.models.signals import post_migrate

from core.observability.logging import get_logger

logger = get_logger(__name__)
PARTITION_MONTHS = 12  # horizonte de `ai_runs` y `ai_actions`: mes actual + 12, como `audit_logs`
PARTITIONED = ("ai_runs", "ai_actions")  # F8-08b
RETENTION_MONTHS = 12  # D-F8-05 (ADR-034 §13): 12 meses completos de detalle; F8-08k
RETENTION = "public.ai_records_drop_partitions(integer)"
LOCK_NOT_AVAILABLE = "55P03"  # el `lock_timeout` (5 s) de la función: una tabla estaba ocupada


def ensure_partitions(sender: Any, using: str = "default", **kwargs: Any) -> None:
    """Tras cada `migrate` (job de migraciones, crm_migrator): crea las particiones que falten.
    Sin partición, `records.save` falla y el copiloto no deja registro."""
    with connections[using].cursor() as cursor:
        for table in PARTITIONED:
            cursor.execute("SELECT to_regprocedure(%s)", [f"public.{table}_ensure_partitions(int)"])
            if cursor.fetchone()[0] is not None:
                cursor.execute(f"SELECT public.{table}_ensure_partitions(%s)", [PARTITION_MONTHS])


def drop_old_partitions(sender: Any, using: str = "default", **kwargs: Any) -> None:
    """Tras cada `migrate` (job de migraciones, crm_migrator; F8-08k): quita las particiones de
    `ai_llm_calls`, `ai_runs` y `ai_actions` de más de `RETENTION_MONTHS` meses completos, todas o
    ninguna. Solo con `AI_RECORDS_RETENTION` encendido; el log lleva nombres y cuentas. Si una
    tabla estaba ocupada, avisa y lo deja para el siguiente `migrate`; cualquier otro error hace
    fallar el job."""
    if not settings.AI_RECORDS_RETENTION:
        return
    try:
        with transaction.atomic(using=using), connections[using].cursor() as cursor:
            cursor.execute("SELECT to_regprocedure(%s)", [RETENTION])
            if cursor.fetchone()[0] is None:
                return
            cursor.execute(
                "SELECT partition_name, row_count FROM public.ai_records_drop_partitions(%s)",
                [RETENTION_MONTHS],
            )
            dropped = cursor.fetchall()
    except OperationalError as error:
        if getattr(error.__cause__, "sqlstate", None) != LOCK_NOT_AVAILABLE:
            raise
        logger.warning("ai.records_retention_lock_busy")
        return
    for partition, rows in dropped:  # ya confirmado
        logger.info("ai.records_partition_dropped", partition=partition, rows=rows)


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
        post_migrate.connect(ensure_partitions, sender=self, dispatch_uid="ai_agents_partitions")
        post_migrate.connect(drop_old_partitions, sender=self, dispatch_uid="ai_records_retention")
