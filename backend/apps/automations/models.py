"""Automatizaciones internas de seguimiento (F11-02; ADR-049 §4). Tres tablas tenant-owned con
FORCE RLS: la configuración de cada regla, el interruptor de la organización y el historial de
ejecuciones. Los CHECK, las FKs compuestas y los privilegios por columna de `crm_app` están en la
migración 0001; `crm_app` no borra ninguna de las tres."""

from django.contrib.postgres.fields import ArrayField
from django.db import models

from core.db.models import TenantModel, uuid7_primary_key


class Mode(models.TextChoices):
    OFF = "OFF"
    SIMULATE = "SIMULATE"
    ON = "ON"


class RunStatus(models.TextChoices):
    DONE = "DONE"
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"
    DEAD = "DEAD"
    CLOSED = "CLOSED"


class AutomationRule(TenantModel):
    """Una regla registrada (`key`) en una organización. Sin fila, la regla está en `OFF`.
    `simulate_since` es el instante (reloj de la base) en que entró en `SIMULATE`; fuera de
    `SIMULATE` es nulo (CHECK). `params` lo valida `apps.automations.params`."""

    id = uuid7_primary_key()
    key = models.CharField(max_length=40)
    mode = models.CharField(max_length=8, choices=Mode.choices, default=Mode.OFF)
    params = models.JSONField(default=dict)
    version = models.PositiveIntegerField(default=1)
    simulate_since = models.DateTimeField(null=True)
    updated_by_user_id = models.UUIDField(null=True)  # FK compuesta a las membresías (0001)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "automation_rules"
        constraints = [
            models.UniqueConstraint(
                fields=["organization_id", "key"], name="automation_rules_key_uq"
            ),
            models.UniqueConstraint(
                fields=["organization_id", "id"], name="automation_rules_org_id_uq"
            ),
        ]


class AutomationSwitch(TenantModel):
    """El interruptor de emergencia de la organización (patrón de `ai_switches`). Sin fila, no
    está pausada; las reglas siguen en `OFF` por omisión."""

    id = uuid7_primary_key()
    paused = models.BooleanField(default=False, db_default=False)
    changed_by_user_id = models.UUIDField(null=True)  # FK compuesta a las membresías (0001)
    changed_at = models.DateTimeField()

    class Meta:
        db_table = "automation_switches"
        constraints = [
            models.UniqueConstraint(fields=["organization_id"], name="automation_switches_org_uq"),
        ]


class AutomationRun(TenantModel):
    """Una ejecución de una regla sobre un sujeto (ADR-049 §4, §6). La clave de idempotencia lleva
    `rule_id` y `mode`, no `rule_version`. `subject_id` y `task_ids` no tienen FK (sujeto
    polimórfico y lista)."""

    id = uuid7_primary_key()
    rule = models.ForeignKey(
        AutomationRule, models.PROTECT, related_name="+", db_constraint=False, db_index=False
    )
    rule_version = models.PositiveIntegerField()
    rule_key = models.CharField(max_length=40)
    subject_type = models.CharField(max_length=16)
    subject_id = models.UUIDField()
    episode_at = models.DateTimeField()
    step = models.PositiveSmallIntegerField()
    mode = models.CharField(max_length=8, choices=Mode.choices)
    status = models.CharField(max_length=8, choices=RunStatus.choices)
    reason = models.CharField(max_length=32, null=True)  # noqa: DJ001 — código cerrado (CHECK)
    recipients = ArrayField(models.UUIDField(), default=list)
    task_ids = ArrayField(models.UUIDField(), default=list)
    attempts = models.PositiveSmallIntegerField(default=0)
    depth = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    closed_at = models.DateTimeField(null=True)

    class Meta:
        db_table = "automation_runs"
        constraints = [
            models.UniqueConstraint(
                fields=["organization_id", "rule", "subject_id", "episode_at", "step", "mode"],
                name="automation_runs_idempotency_uq",
            ),
            models.UniqueConstraint(  # F11-05a: una sola ejecución abierta por sujeto, paso y modo
                fields=["organization_id", "rule", "subject_id", "step", "mode"],
                condition=~models.Q(status="CLOSED"),
                name="automation_runs_open_uq",
            ),
        ]
        indexes = [
            models.Index(fields=["organization_id", "created_at"], name="automation_runs_age_idx")
        ]
