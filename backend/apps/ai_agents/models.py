"""El registro del copiloto (F8-08b; ADR-034 §5, §7; ADR-005 Parte E). Las tablas son SQL
(migrations/0001): `ai_runs` y `ai_actions`, de solo inserción y particionadas por mes, y
`ai_suggestions`, cuyo texto solo puede pasar a NULL (la purga de D-F8-05). Estos modelos no las
gestionan (`managed = False`): existen para leerlas y para que se emita `post_migrate`. Se
escriben solo con `apps.ai_agents.records.save()`, que redacta los argumentos y audita."""

from typing import Any, NoReturn

from django.contrib.postgres.fields import ArrayField
from django.db import models

from core.db.models import TenantManager, TenantModel, uuid7_primary_key


def _refuse(*args: Any, **kwargs: Any) -> NoReturn:
    raise TypeError("el registro del copiloto solo se escribe con apps.ai_agents.records.save()")


class _ReadOnly(models.QuerySet[Any]):
    bulk_create = bulk_update = update = delete = _refuse


class _Record(TenantModel):
    id = uuid7_primary_key()  # en las particionadas, la clave es (organization_id, created_at, id)
    created_at = models.DateTimeField()

    objects = TenantManager.from_queryset(_ReadOnly)()  # type: ignore[misc]

    class Meta:
        abstract = True

    save = delete = _refuse


class AIRun(_Record):
    """Una invocación: quién, qué conversación, cuántas tools, hallazgos por tipo (sin texto)."""

    purpose = models.CharField(max_length=16)
    requested_by_user_id = models.UUIDField()
    conversation_id = models.UUIDField()
    outcome = models.CharField(max_length=32)
    tool_calls = models.IntegerField()
    findings = models.JSONField()  # {"TIPO": cuántos}
    reservation_ids = ArrayField(models.UUIDField())  # las de `ai_llm_calls`, del gateway
    retried = models.BooleanField()
    handoff_reason = models.CharField(max_length=32, null=True)  # noqa: DJ001 (la tabla es SQL)
    duration_ms = models.IntegerField()

    class Meta:
        managed = False
        db_table = "ai_runs"


class AIAction(_Record):
    """Una tool call con sus argumentos redactados y su evidencia. `run_id` sin FK: `ai_runs` es
    particionada (la escribe la misma transacción, `records.save`)."""

    run_id = models.UUIDField()
    tool_use_id = models.CharField(max_length=64)
    tool = models.CharField(max_length=41)
    code = models.CharField(max_length=32)
    arguments = models.JSONField()  # un UUID tal cual; otro texto, solo su longitud
    evidence_id = models.UUIDField(null=True)
    kind = models.CharField(max_length=16, null=True)  # noqa: DJ001
    as_of = models.DateTimeField(null=True)
    subjects = ArrayField(models.UUIDField())
    facts = models.JSONField()  # lo exacto para el vendedor (el stock); nunca va al modelo

    class Meta:
        managed = False
        db_table = "ai_actions"


class AISuggestion(_Record):
    """El borrador o el resumen, con sus marcas `[verificar]` ya puestas; `text` es NULL tras la
    purga de 30 días (D-F8-05)."""

    run_id = models.UUIDField()
    conversation_id = models.UUIDField()
    requested_by_user_id = models.UUIDField()
    kind = models.CharField(max_length=16)
    text = models.TextField(null=True)  # noqa: DJ001
    findings = models.JSONField()  # [{"type", "start", "end"}]: tipo y posición, nunca el tramo
    handoff_reason = models.CharField(max_length=32, null=True)  # noqa: DJ001
    evidence_expires_at = models.DateTimeField(null=True)

    class Meta:
        managed = False
        db_table = "ai_suggestions"
