"""F11-02 (#1176; ADR-049 §4): `automation_rules`, `automation_switches` y `automation_runs`.

Solo expansión: tres tablas nuevas y nada sobre las que ya existen, así que la imagen anterior sigue
funcionando con el esquema nuevo (no las conoce). Las tres son tenant-owned con FORCE RLS y la
política única `tenant_isolation`, FK a `organizations` y FKs compuestas a las membresías y a la
regla. `crm_app` no tiene `DELETE` ni `TRUNCATE` en ninguna; inserta y cambia solo las columnas que
se listan abajo: nunca la organización, el `id`, la clave de una regla ni la clave de idempotencia
de una ejecución. Los CHECK guardan lo que la aplicación no debe poder romper: modos y estados
cerrados, `simulate_since` solo en `SIMULATE`, `max_recipients` de 1 a 3 (D-F11-04, E1), como
mucho 3 destinatarios y 3 tareas por ejecución (E1), ninguna tarea en `SIMULATE` y `depth = 0`.
`subject_id` y `task_ids` no tienen FK (sujeto polimórfico y lista; ADR-049, Consecuencias).

La purga de 180 días (D-F11-07) es de F11-10 (#1185); el camino elegido está en
docs/phases/phase-11.md. Hacia atrás se borran las tablas."""

from typing import Any

import django.contrib.postgres.fields
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

import core.ids
from core.db.operations import CompositeTenantFK, EnableRLS

MEMBERS = "organization_memberships (organization_id, user_id)"
KEYS = "'conversation.unanswered', 'quote.no_response'"
REASONS = (
    "'NO_RECIPIENT', 'MEMBER_INACTIVE', 'CAPPED', 'LOCK_TIMEOUT', 'DB_ERROR', 'RESOLVED',"
    " 'RULE_CHANGED'"
)
MAX_RECIPIENTS = (  # ausente, o un entero de 1 a 3 (el CASE evita convertir algo que no es número)
    "CASE WHEN NOT params ? 'max_recipients' THEN true"
    " WHEN jsonb_typeof(params -> 'max_recipients') <> 'number' THEN false"
    " ELSE (params ->> 'max_recipients') IN ('1', '2', '3') END"
)
CHECKS = {
    "automation_rules": {
        "key_ck": f"key IN ({KEYS})",
        "mode_ck": "mode IN ('OFF', 'SIMULATE', 'ON')",
        "params_ck": "jsonb_typeof(params) = 'object' AND octet_length(params::text) <= 2048",
        "max_recipients_ck": MAX_RECIPIENTS,
        "version_ck": "version >= 1",
        "simulate_since_ck": "(mode = 'SIMULATE') = (simulate_since IS NOT NULL)",
    },
    "automation_runs": {
        "rule_key_ck": f"rule_key IN ({KEYS})",
        "rule_version_ck": "rule_version >= 1",
        "subject_type_ck": "subject_type IN ('conversation', 'quote')",
        "step_ck": "step IN (1, 2)",
        "mode_ck": "mode IN ('SIMULATE', 'ON')",
        "status_ck": "status IN ('DONE', 'SKIPPED', 'FAILED', 'DEAD', 'CLOSED')",
        "reason_ck": f"reason IS NULL OR reason IN ({REASONS})",
        "recipients_ck": "cardinality(recipients) <= 3",
        "task_ids_ck": "cardinality(task_ids) <= 3",
        "simulated_ck": "mode = 'ON' OR cardinality(task_ids) = 0",
        "attempts_ck": "attempts BETWEEN 0 AND 3",
        "dead_ck": "status <> 'DEAD' OR attempts = 3",
        "depth_ck": "depth = 0",
        "closed_ck": "(status = 'CLOSED') = (closed_at IS NOT NULL)",
    },
}
COLUMNS = {  # (INSERT, UPDATE) de `crm_app`
    "automation_rules": (
        "id, organization_id, key, mode, params, version, simulate_since, updated_by_user_id,"
        " created_at, updated_at",
        "mode, params, version, simulate_since, updated_by_user_id, updated_at",
    ),
    "automation_switches": (
        "organization_id, paused, changed_by_user_id, changed_at",
        "paused, changed_by_user_id, changed_at",
    ),
    "automation_runs": (
        "id, organization_id, rule_id, rule_version, rule_key, subject_type, subject_id,"
        " episode_at, step, mode, status, reason, recipients, task_ids, attempts, depth,"
        " created_at, updated_at, closed_at",
        "rule_version, status, reason, recipients, task_ids, attempts, updated_at, closed_at",
    ),
}


def _grants(apps: Any, schema_editor: Any) -> None:
    role = schema_editor.quote_name(settings.DB_APP_ROLE)
    for table, (insert, update) in COLUMNS.items():
        schema_editor.execute(f"REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON {table} FROM {role}")
        schema_editor.execute(f"GRANT INSERT ({insert}) ON {table} TO {role}")
        schema_editor.execute(f"GRANT UPDATE ({update}) ON {table} TO {role}")


def _sql(table: str, name: str, definition: str) -> migrations.RunSQL:
    return migrations.RunSQL(
        f"ALTER TABLE public.{table} ADD CONSTRAINT {table}_{name} {definition}",
        f"ALTER TABLE public.{table} DROP CONSTRAINT IF EXISTS {table}_{name}",
    )


class Migration(migrations.Migration):
    initial = True
    dependencies = [
        ("core", "0004_outbox_function_search_path"),
        ("organizations", "0013_invitation_sends_guard"),
    ]

    operations = [
        migrations.CreateModel(
            name="AutomationRule",
            fields=[
                ("organization_id", models.UUIDField(db_index=True, editable=False)),
                (
                    "id",
                    models.UUIDField(
                        db_default=models.Func(function="uuidv7", output_field=models.UUIDField()),
                        default=core.ids.new_id,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("key", models.CharField(max_length=40)),
                (
                    "mode",
                    models.CharField(
                        choices=[("OFF", "Off"), ("SIMULATE", "Simulate"), ("ON", "On")],
                        default="OFF",
                        max_length=8,
                    ),
                ),
                ("params", models.JSONField(default=dict)),
                ("version", models.PositiveIntegerField(default=1)),
                ("simulate_since", models.DateTimeField(null=True)),
                ("updated_by_user_id", models.UUIDField(null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "db_table": "automation_rules",
                "constraints": [
                    models.UniqueConstraint(
                        fields=("organization_id", "key"), name="automation_rules_key_uq"
                    ),
                    models.UniqueConstraint(
                        fields=("organization_id", "id"), name="automation_rules_org_id_uq"
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="AutomationSwitch",
            fields=[
                ("organization_id", models.UUIDField(db_index=True, editable=False)),
                (
                    "id",
                    models.UUIDField(
                        db_default=models.Func(function="uuidv7", output_field=models.UUIDField()),
                        default=core.ids.new_id,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("paused", models.BooleanField(db_default=False, default=False)),
                ("changed_by_user_id", models.UUIDField(null=True)),
                ("changed_at", models.DateTimeField()),
            ],
            options={
                "db_table": "automation_switches",
                "constraints": [
                    models.UniqueConstraint(
                        fields=("organization_id",), name="automation_switches_org_uq"
                    )
                ],
            },
        ),
        migrations.CreateModel(
            name="AutomationRun",
            fields=[
                ("organization_id", models.UUIDField(db_index=True, editable=False)),
                (
                    "id",
                    models.UUIDField(
                        db_default=models.Func(function="uuidv7", output_field=models.UUIDField()),
                        default=core.ids.new_id,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("rule_version", models.PositiveIntegerField()),
                ("rule_key", models.CharField(max_length=40)),
                ("subject_type", models.CharField(max_length=16)),
                ("subject_id", models.UUIDField()),
                ("episode_at", models.DateTimeField()),
                ("step", models.PositiveSmallIntegerField()),
                (
                    "mode",
                    models.CharField(
                        choices=[("OFF", "Off"), ("SIMULATE", "Simulate"), ("ON", "On")],
                        max_length=8,
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("DONE", "Done"),
                            ("SKIPPED", "Skipped"),
                            ("FAILED", "Failed"),
                            ("DEAD", "Dead"),
                            ("CLOSED", "Closed"),
                        ],
                        max_length=8,
                    ),
                ),
                ("reason", models.CharField(max_length=32, null=True)),
                (
                    "recipients",
                    django.contrib.postgres.fields.ArrayField(
                        base_field=models.UUIDField(), default=list, size=None
                    ),
                ),
                (
                    "task_ids",
                    django.contrib.postgres.fields.ArrayField(
                        base_field=models.UUIDField(), default=list, size=None
                    ),
                ),
                ("attempts", models.PositiveSmallIntegerField(default=0)),
                ("depth", models.PositiveSmallIntegerField(default=0)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("closed_at", models.DateTimeField(null=True)),
                (
                    "rule",
                    models.ForeignKey(
                        db_constraint=False,
                        db_index=False,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="automations.automationrule",
                    ),
                ),
            ],
            options={
                "db_table": "automation_runs",
                "indexes": [
                    models.Index(
                        fields=["organization_id", "created_at"], name="automation_runs_age_idx"
                    )
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=(
                            "organization_id",
                            "rule",
                            "subject_id",
                            "episode_at",
                            "step",
                            "mode",
                        ),
                        name="automation_runs_idempotency_uq",
                    )
                ],
            },
        ),
        EnableRLS("AutomationRule"),
        EnableRLS("AutomationSwitch"),
        EnableRLS("AutomationRun"),
        *(
            _sql(
                table,
                "organization_fk",
                "FOREIGN KEY (organization_id) REFERENCES organizations (id)",
            )
            for table in COLUMNS
        ),
        _sql(
            "automation_rules",
            "updated_by_fk",
            f"FOREIGN KEY (organization_id, updated_by_user_id) REFERENCES {MEMBERS}",
        ),
        _sql(
            "automation_switches",
            "changed_by_fk",
            f"FOREIGN KEY (organization_id, changed_by_user_id) REFERENCES {MEMBERS}",
        ),
        CompositeTenantFK("AutomationRun", "rule", "AutomationRule", "automation_runs_rule_fk"),
        *(
            _sql(table, name, f"CHECK ({definition})")
            for table, checks in CHECKS.items()
            for name, definition in checks.items()
        ),
        migrations.RunPython(_grants, migrations.RunPython.noop),
    ]
