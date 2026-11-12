"""F7-08b: `consent_keywords`, las palabras de baja y de alta de cada organización (D-F7-05).

Tenant-owned con FORCE RLS y una política; FK a la organización y FK compuesta a la membresía de
quien la configuró (ADR-001 §3). `crm_app` la lee, la inserta y la borra (el comando reemplaza el
conjunto entero), sin UPDATE ni TRUNCATE. Sin filas, el código aplica las palabras por defecto.
"""

from typing import Any

from django.conf import settings
from django.db import migrations, models

import core.ids
from core.db.operations import EnableRLS

FKS = (
    ("consent_keywords_organization_fk", "organization_id", "organizations (id)"),
    (
        "consent_keywords_created_by_fk",
        "organization_id, created_by_user_id",
        "organization_memberships (organization_id, user_id)",
    ),
)


def _privileges(apps: Any, schema_editor: Any) -> None:
    role = schema_editor.quote_name(settings.DB_APP_ROLE)
    schema_editor.execute(f"REVOKE UPDATE, TRUNCATE ON public.consent_keywords FROM {role}")


def _restore(apps: Any, schema_editor: Any) -> None:
    role = schema_editor.quote_name(settings.DB_APP_ROLE)
    schema_editor.execute(f"GRANT UPDATE, TRUNCATE ON public.consent_keywords TO {role}")


class Migration(migrations.Migration):
    dependencies = [
        ("contacts", "0007_consents"),
    ]

    operations = [
        migrations.CreateModel(
            name="ConsentKeyword",
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
                ("keyword", models.CharField(max_length=30)),
                (
                    "action",
                    models.CharField(
                        choices=[("OPT_OUT", "Opt Out"), ("OPT_IN", "Opt In")], max_length=7
                    ),
                ),
                ("created_by_user_id", models.UUIDField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={
                "db_table": "consent_keywords",
                "constraints": [
                    models.UniqueConstraint(
                        fields=("organization_id", "keyword"),
                        name="consent_keywords_org_keyword_uq",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("action__in", ["OPT_OUT", "OPT_IN"])),
                        name="consent_keywords_action_ck",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("keyword__regex", "^[a-z0-9]+( [a-z0-9]+){0,2}$")),
                        name="consent_keywords_keyword_ck",
                    ),
                ],
            },
        ),
        *(
            migrations.RunSQL(
                f"ALTER TABLE public.consent_keywords ADD CONSTRAINT {name} FOREIGN KEY "
                f"({columns}) REFERENCES {target}",
                f"ALTER TABLE public.consent_keywords DROP CONSTRAINT IF EXISTS {name}",
            )
            for name, columns, target in FKS
        ),
        EnableRLS("ConsentKeyword"),
        migrations.RunPython(_privileges, _restore),
    ]
