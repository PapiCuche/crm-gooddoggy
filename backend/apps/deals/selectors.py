"""Lecturas de pipelines y motivos de pérdida, por organización (RLS y `TenantManager`). El
permiso y el alcance los aplica quien las sirve (`HasPermission` y `ScopeFilter`)."""

from uuid import UUID

from django.db.models import Prefetch, QuerySet

from apps.deals.models import STAGE_ORDER, LostReason, Pipeline, PipelineStage


def pipelines() -> QuerySet[Pipeline]:
    """Los pipelines no borrados, activos o no, cada uno con todas sus etapas (también las
    inactivas) en su orden: una consulta más para toda la página. El orden de los pipelines lo
    pone la paginación (ADR-016)."""
    stages = Prefetch("stages", PipelineStage.objects.order_by(*STAGE_ORDER))
    return Pipeline.objects.filter(deleted_at__isnull=True).prefetch_related(stages)


def lost_reasons() -> QuerySet[LostReason]:
    """Los motivos no borrados, activos o no. El orden lo pone la paginación (ADR-016)."""
    return LostReason.objects.filter(deleted_at__isnull=True)


def lost_reason_count() -> int:
    return LostReason.objects.filter(deleted_at__isnull=True).count()


def pipeline_count() -> int:
    return Pipeline.objects.filter(deleted_at__isnull=True).count()


def locked_pipeline(pipeline_id: UUID) -> Pipeline:
    """Un pipeline no borrado, bloqueado hasta el fin de la transacción como lo bloquean los
    comandos de etapas (`FOR NO KEY UPDATE`): lo que se comprueba aquí sigue valiendo cuando el
    comando escribe. `DoesNotExist` si no es de la organización."""
    live = Pipeline.objects.filter(deleted_at__isnull=True)
    found: Pipeline = live.select_for_update(no_key=True).get(pk=pipeline_id)
    return found


def has_stage(pipeline_id: UUID, stage_id: UUID) -> bool:
    return PipelineStage.objects.filter(pipeline_id=pipeline_id, pk=stage_id).exists()
