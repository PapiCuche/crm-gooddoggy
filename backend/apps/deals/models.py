"""Pipelines, etapas, motivos de pérdida y oportunidades (Fase 5, E09; modelo de datos §E.6).
Tablas tenant-owned con FORCE RLS y FKs compuestas. Nada se borra: un pipeline, un motivo o una
oportunidad se marca borrado y una etapa se desactiva; el historial de etapas solo se añade."""

from django.db import models

from core.db.models import TenantModel, uuid7_primary_key

NUMBER = r"^OPP-[0-9]{6,}$"  # `OPP-000123`; pasado el millón crece (core.sequences)
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
            models.UniqueConstraint(  # destino de la FK de las oportunidades
                fields=["organization_id", "pipeline", "id", "stage_type"],
                name="pipeline_stages_org_pipeline_type_uq",
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


class Priority(models.TextChoices):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    URGENT = "URGENT"


class OpportunitySource(models.TextChoices):  # los orígenes de §E.5, sin importar `contacts`
    INBOUND_MESSAGE = "INBOUND_MESSAGE"
    WALK_IN = "WALK_IN"
    REFERRAL = "REFERRAL"
    IMPORT = "IMPORT"
    MANUAL = "MANUAL"
    CAMPAIGN = "CAMPAIGN"
    AI = "AI"


class Opportunity(TenantModel):
    """Posible venta de un contacto (§E.6). `status` se deriva del tipo de la etapa y se guarda
    para indexar: la FK compuesta `(etapa, pipeline, status)` lo hace cumplir en la base, y un
    CHECK ata el estado a `won_at`, `lost_at` y al motivo (que perder exige). Las personas
    (`*_user_id`) son miembros de la organización: FKs compuestas en la migración."""

    Status = StageType
    Priority = Priority
    Source = OpportunitySource

    id = uuid7_primary_key()
    number = models.CharField(max_length=20)
    contact = models.ForeignKey(
        "contacts.Contact", models.PROTECT, related_name="+", db_constraint=False, db_index=False
    )
    pipeline = models.ForeignKey(
        Pipeline, models.PROTECT, related_name="+", db_constraint=False, db_index=False
    )
    stage = models.ForeignKey(
        PipelineStage, models.PROTECT, related_name="+", db_constraint=False, db_index=False
    )
    status = models.CharField(max_length=4, choices=StageType.choices, default=StageType.OPEN)
    assigned_user_id = models.UUIDField(null=True, blank=True)
    title = models.CharField(max_length=200)
    amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    currency = models.CharField(max_length=3)
    probability = models.PositiveSmallIntegerField()
    priority = models.CharField(max_length=8, choices=Priority.choices, default=Priority.MEDIUM)
    source = models.CharField(
        max_length=24, choices=OpportunitySource.choices, default=OpportunitySource.MANUAL
    )
    next_action = models.CharField(max_length=200, null=True, blank=True)  # noqa: DJ001
    next_action_at = models.DateTimeField(null=True, blank=True)
    expected_close_date = models.DateField(null=True, blank=True)
    last_activity_at = models.DateTimeField()
    stage_entered_at = models.DateTimeField()
    won_at = models.DateTimeField(null=True, blank=True)
    lost_at = models.DateTimeField(null=True, blank=True)
    lost_reason = models.ForeignKey(
        LostReason,
        models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        db_constraint=False,
        db_index=False,
    )
    lost_reason_note = models.CharField(max_length=1000, null=True, blank=True)  # noqa: DJ001
    created_by_user_id = models.UUIDField(null=True, blank=True)
    updated_by_user_id = models.UUIDField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    deleted_at = models.DateTimeField(null=True, blank=True)
    deleted_by_user_id = models.UUIDField(null=True, blank=True)
    version = models.PositiveIntegerField(default=1)

    class Meta:
        db_table = "opportunities"
        indexes = [
            models.Index(
                fields=["organization_id", "pipeline", "stage", "status"],
                name="opportunities_org_stage_idx",
            ),
            models.Index(
                fields=["organization_id", "assigned_user_id", "status"],
                name="opportunities_org_owner_idx",
            ),
            models.Index(  # las abiertas sin actividad (oportunidades estancadas)
                fields=["organization_id", "last_activity_at"],
                condition=models.Q(status="OPEN", deleted_at__isnull=True),
                name="opportunities_org_stale_idx",
            ),
            models.Index(
                fields=["organization_id", "contact"], name="opportunities_org_contact_idx"
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["organization_id", "id"], name="opportunities_org_id_uq"
            ),
            models.UniqueConstraint(
                fields=["organization_id", "number"], name="opportunities_number_uq"
            ),
            models.CheckConstraint(
                condition=models.Q(number__regex=NUMBER), name="opportunities_number_ck"
            ),
            models.CheckConstraint(
                condition=models.Q(title__regex=r"\S"), name="opportunities_title_ck"
            ),
            models.CheckConstraint(
                condition=models.Q(amount__gte=0), name="opportunities_amount_ck"
            ),
            models.CheckConstraint(
                condition=models.Q(currency__regex="^[A-Z]{3}$"), name="opportunities_currency_ck"
            ),
            models.CheckConstraint(
                condition=models.Q(priority__in=Priority.values), name="opportunities_priority_ck"
            ),
            models.CheckConstraint(
                condition=models.Q(source__in=OpportunitySource.values),
                name="opportunities_source_ck",
            ),
            models.CheckConstraint(  # el estado fija la fecha y el motivo de lo cerrado
                condition=models.Q(
                    status="OPEN",
                    won_at__isnull=True,
                    lost_at__isnull=True,
                    lost_reason__isnull=True,
                    lost_reason_note__isnull=True,
                )
                | models.Q(
                    status="WON",
                    won_at__isnull=False,
                    lost_at__isnull=True,
                    lost_reason__isnull=True,
                    lost_reason_note__isnull=True,
                )
                | models.Q(
                    status="LOST",
                    won_at__isnull=True,
                    lost_at__isnull=False,
                    lost_reason__isnull=False,
                ),
                name="opportunities_status_ck",
            ),
            models.CheckConstraint(  # ganar es 100 % y perder, 0 %
                condition=models.Q(status="OPEN", probability__lte=100)
                | models.Q(status="WON", probability=100)
                | models.Q(status="LOST", probability=0),
                name="opportunities_probability_ck",
            ),
            models.CheckConstraint(
                condition=models.Q(version__gte=1), name="opportunities_version_ck"
            ),
        ]

    def __str__(self) -> str:
        return str(self.pk)


class OpportunityItem(TenantModel):
    """Producto de interés de una oportunidad (§E.6): una línea por variante, con un precio
    estimado referencial (no firme: el precio de verdad lo fija la cotización)."""

    id = uuid7_primary_key()
    opportunity = models.ForeignKey(
        Opportunity, models.PROTECT, related_name="items", db_constraint=False, db_index=False
    )
    variant = models.ForeignKey(
        "catalog.ProductVariant",
        models.PROTECT,
        related_name="+",
        db_constraint=False,
        db_index=False,
    )
    quantity = models.IntegerField()
    estimated_unit_price = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    notes = models.CharField(max_length=1000, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "opportunity_items"
        constraints = [
            models.UniqueConstraint(
                fields=["organization_id", "opportunity", "variant"],
                name="opportunity_items_variant_uq",
            ),
            models.CheckConstraint(
                condition=models.Q(quantity__gt=0), name="opportunity_items_quantity_ck"
            ),
            models.CheckConstraint(
                condition=models.Q(estimated_unit_price__gte=0), name="opportunity_items_price_ck"
            ),
        ]

    def __str__(self) -> str:
        return str(self.pk)


class OpportunityStageHistory(TenantModel):
    """Cada paso de una oportunidad entre etapas [INM] (§E.6): nace con `from_stage` vacío y no
    se reescribe (`crm_app` solo inserta y lee). `seconds_in_previous_stage` vale lo que estuvo
    en la etapa de la que sale."""

    id = uuid7_primary_key()
    opportunity = models.ForeignKey(
        Opportunity, models.PROTECT, related_name="history", db_constraint=False, db_index=False
    )
    from_stage = models.ForeignKey(
        PipelineStage,
        models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        db_constraint=False,
        db_index=False,
    )
    to_stage = models.ForeignKey(
        PipelineStage, models.PROTECT, related_name="+", db_constraint=False, db_index=False
    )
    changed_by_user_id = models.UUIDField(null=True, blank=True)
    changed_at = models.DateTimeField()
    seconds_in_previous_stage = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        db_table = "opportunity_stage_history"
        indexes = [
            models.Index(
                fields=["organization_id", "opportunity", "changed_at", "id"],
                name="opp_history_org_opp_idx",
            ),
        ]
        constraints = [
            models.CheckConstraint(  # la creación no sale de ninguna etapa; todo otro paso, sí
                condition=models.Q(from_stage__isnull=True, seconds_in_previous_stage__isnull=True)
                | models.Q(from_stage__isnull=False, seconds_in_previous_stage__isnull=False),
                name="opp_history_from_seconds_ck",
            ),
            models.CheckConstraint(
                condition=models.Q(from_stage__isnull=True)
                | ~models.Q(from_stage=models.F("to_stage")),
                name="opp_history_moved_ck",
            ),
        ]

    def __str__(self) -> str:
        return str(self.pk)
