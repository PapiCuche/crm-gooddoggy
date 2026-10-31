"""Lecturas de pipelines y motivos de pérdida, por organización (RLS y `TenantManager`). El
permiso y el alcance los aplica quien las sirve (`HasPermission` y `ScopeFilter`)."""

from uuid import UUID

from django.db.models import Prefetch, Q, QuerySet

from apps.deals.models import (
    STAGE_ORDER,
    LostReason,
    Opportunity,
    OpportunityItem,
    OpportunityStageHistory,
    Pipeline,
    PipelineStage,
)


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


def stage_count(pipeline_id: UUID) -> int:
    return PipelineStage.objects.filter(pipeline_id=pipeline_id).count()


def opportunities() -> QuerySet[Opportunity]:
    """Las oportunidades no borradas, con su etapa y su motivo de pérdida en la misma consulta.
    El orden lo pone la paginación (ADR-016): por `id`, no por número (`OPP-1000000` va después
    de `OPP-999999` por creación, no como texto)."""
    return Opportunity.objects.filter(deleted_at__isnull=True).select_related(
        "stage", "lost_reason"
    )


def matching(found: QuerySet[Opportunity], text: str) -> QuerySet[Opportunity]:
    """Las de `found` cuyo título o número contienen `text`, sin distinguir mayúsculas (`%`, `_`
    y `\\` son literales); vacío, todas. Solo filtra."""
    if not text:
        return found
    return found.filter(Q(title__icontains=text) | Q(number__icontains=text))


def with_detail(found: QuerySet[Opportunity]) -> QuerySet[Opportunity]:
    """Con sus líneas y su historial de etapas (con los nombres), una consulta más cada uno."""
    steps = OpportunityStageHistory.objects.select_related("from_stage", "to_stage")
    return found.prefetch_related(
        Prefetch("items", OpportunityItem.objects.order_by("id")),
        Prefetch("history", steps.order_by("changed_at", "id")),
    )
