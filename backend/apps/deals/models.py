"""Pipelines, etapas y motivos de pérdida (Fase 5, E09-05; modelo de datos §E.6). Tablas
tenant-owned con FORCE RLS. Las oportunidades (F5-03) referenciarán etapas y motivos con FKs
compuestas. Nada se borra: un pipeline o un motivo se marca borrado, una etapa se desactiva."""

from django.db import models

from core.db.models import TenantModel, uuid7_primary_key

CODE = r"^[a-z0-9]+(-[a-z0-9]+)*$"  # `retail`, `mayoristas-lima`
REASON_CODE = r"^[A-Z][A-Z0-9_]*$"  # `OUT_OF_STOCK`, como la semilla de 02 §E.6
COLOR = r"^#[0-9a-f]{6}$"  # `#rrggbb`, en minúsculas
STAGE_ORDER = ("sort_order", "name", "id")  # el orden de las etapas, con desempate estable


class Kind(models.TextChoices):
    RETAIL = "RETAIL"
    WHOLESALE = "WHOLESALE"
    TECH_SERVICE = "TECH_SERVICE"
    OTHER = "OTHER"


class StageType(models.TextChoices):
    OPEN = "OPEN"
    WON = "WON"
    LOST = "LOST"


class Pipeline(TenantModel):
    """Proceso comercial de la organización. Uno solo es el predeterminado entre los vivos, y es
    activo. El código y la moneda no cambian. Las personas (`*_user_id`) son miembros de la
    organización: FKs compuestas en la migración."""

    Kind = Kind

    id = uuid7_primary_key()
    name = models.CharField(max_length=100)
    code = models.CharField(max_length=30)
    kind = models.CharField(max_length=16, choices=Kind.choices)
    currency = models.CharField(max_length=3)
    is_default = models.BooleanField(default=False)
    sort_order = models.IntegerField(default=0)
    is_active = models.BooleanField(default=True)
    created_by_user_id = models.UUIDField(null=True, blank=True)
    updated_by_user_id = models.UUIDField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    deleted_at = models.DateTimeField(null=True, blank=True)
    deleted_by_user_id = models.UUIDField(null=True, blank=True)
    version = models.PositiveIntegerField(default=1)

    class Meta:
        db_table = "pipelines"
        constraints = [
            models.UniqueConstraint(  # destino de las FK compuestas de las tablas hijas
                fields=["organization_id", "id"], name="pipelines_org_id_uq"
            ),
            models.UniqueConstraint(
                fields=["organization_id", "code"],
                condition=models.Q(deleted_at__isnull=True),
                name="pipelines_code_uq",
            ),
            models.UniqueConstraint(
                fields=["organization_id"],
                condition=models.Q(is_default=True, deleted_at__isnull=True),
                name="pipelines_default_uq",
            ),
            models.CheckConstraint(condition=models.Q(code__regex=CODE), name="pipelines_code_ck"),
            models.CheckConstraint(
                condition=models.Q(currency__regex="^[A-Z]{3}$"), name="pipelines_currency_ck"
            ),
            models.CheckConstraint(
                condition=models.Q(kind__in=Kind.values), name="pipelines_kind_ck"
            ),
            models.CheckConstraint(  # la predeterminada está viva y activa
                condition=models.Q(is_default=False)
                | models.Q(is_active=True, deleted_at__isnull=True),
                name="pipelines_default_active_ck",
            ),
            models.CheckConstraint(condition=models.Q(version__gte=1), name="pipelines_version_ck"),
        ]

    def __str__(self) -> str:
        return str(self.pk)


class PipelineStage(TenantModel):
    """Etapa de un pipeline. Un pipeline tiene una etapa WON (100 %), al menos una LOST activa
    (0 %) y al menos una OPEN activa: el servicio lo valida en cada comando y la tabla respalda lo
    que puede (una sola WON; la probabilidad que corresponde al tipo; `requires_fields` es un
    objeto, CHECK escrito en la migración)."""

    StageType = StageType

    id = uuid7_primary_key()
    pipeline = models.ForeignKey(
        Pipeline, models.PROTECT, related_name="stages", db_constraint=False, db_index=False
    )
    name = models.CharField(max_length=100)
    code = models.CharField(max_length=30)
    sort_order = models.IntegerField(default=0)
    stage_type = models.CharField(max_length=4, choices=StageType.choices)
    default_probability = models.PositiveSmallIntegerField()
    stale_after_days = models.PositiveSmallIntegerField(null=True, blank=True)
    color = models.CharField(max_length=7)
    requires_fields = models.JSONField(default=dict)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "pipeline_stages"
        constraints = [
            models.UniqueConstraint(  # destino de las FK compuestas de las oportunidades
                fields=["organization_id", "id"], name="pipeline_stages_org_id_uq"
            ),
            models.UniqueConstraint(  # también lleva a las etapas de un pipeline
                fields=["organization_id", "pipeline", "code"], name="pipeline_stages_code_uq"
            ),
            models.UniqueConstraint(
                fields=["organization_id", "pipeline"],
                condition=models.Q(stage_type="WON"),
                name="pipeline_stages_one_won_uq",
            ),
            models.CheckConstraint(
                condition=models.Q(code__regex=CODE), name="pipeline_stages_code_ck"
            ),
            models.CheckConstraint(
                condition=models.Q(stage_type__in=StageType.values), name="pipeline_stages_type_ck"
            ),
            models.CheckConstraint(
                condition=models.Q(default_probability__lte=100), name="pipeline_stages_prob_ck"
            ),
            models.CheckConstraint(  # ganar es 100 % y perder, 0 %
                condition=models.Q(stage_type="OPEN")
                | models.Q(stage_type="WON", default_probability=100)
                | models.Q(stage_type="LOST", default_probability=0),
                name="pipeline_stages_closed_prob_ck",
            ),
            models.CheckConstraint(
                condition=models.Q(stale_after_days__isnull=True)
                | models.Q(stale_after_days__gt=0),
                name="pipeline_stages_stale_ck",
            ),
            models.CheckConstraint(
                condition=models.Q(color__regex=COLOR), name="pipeline_stages_color_ck"
            ),
        ]

    def __str__(self) -> str:
        return str(self.pk)


class LostReason(TenantModel):
    """Motivo de pérdida configurable (§34). Se desactiva o se marca borrado; las oportunidades
    perdidas (F5-04) seguirán apuntando a él."""

    id = uuid7_primary_key()
    name = models.CharField(max_length=100)
    code = models.CharField(max_length=30)
    sort_order = models.IntegerField(default=0)
    is_active = models.BooleanField(default=True)
    created_by_user_id = models.UUIDField(null=True, blank=True)
    updated_by_user_id = models.UUIDField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    deleted_at = models.DateTimeField(null=True, blank=True)
    deleted_by_user_id = models.UUIDField(null=True, blank=True)
    version = models.PositiveIntegerField(default=1)

    class Meta:
        db_table = "lost_reasons"
        constraints = [
            models.UniqueConstraint(
                fields=["organization_id", "id"], name="lost_reasons_org_id_uq"
            ),
            models.UniqueConstraint(
                fields=["organization_id", "code"],
                condition=models.Q(deleted_at__isnull=True),
                name="lost_reasons_code_uq",
            ),
            models.CheckConstraint(
                condition=models.Q(code__regex=REASON_CODE), name="lost_reasons_code_ck"
            ),
            models.CheckConstraint(
                condition=models.Q(version__gte=1), name="lost_reasons_version_ck"
            ),
        ]

    def __str__(self) -> str:
        return str(self.pk)
