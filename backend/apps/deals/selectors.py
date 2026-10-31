"""Lecturas de pipelines y motivos de pérdida, por organización (RLS y `TenantManager`). El
permiso y el alcance los aplica quien las sirve (`HasPermission` y `ScopeFilter`)."""

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
