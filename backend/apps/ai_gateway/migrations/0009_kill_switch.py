"""F8-06 (#769; ADR-034 §5): el interruptor de IA de cada organización, `ai_switches`. Una fila por
organización, apagada por omisión: sin fila no hay llamadas (falla cerrado), así que las
organizaciones que ya existen quedan apagadas sin migrar datos. FORCE RLS con la política única
`tenant_isolation`. `crm_app` inserta la fila de su organización (el `id`, por omisión) y cambia
solo el estado, quién y cuándo; nunca la borra, ni su `id` ni su organización. Sin función SECURITY
DEFINER: la escribe la ruta como `crm_app` bajo RLS, como los topes (0005)."""

from typing import Any

from django.conf import settings
from django.db import migrations, models

import core.ids
from core.db.operations import EnableRLS

MUTABLE = "enabled, changed_by_user_id, changed_at"


def _grants(apps: Any, schema_editor: Any) -> None:
    role = schema_editor.quote_name(settings.DB_APP_ROLE)
    schema_editor.execute(
        f"REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON public.ai_switches FROM {role}"
    )
    schema_editor.execute(
        f"GRANT INSERT (organization_id, {MUTABLE}) ON public.ai_switches TO {role}"
    )
    schema_editor.execute(f"GRANT UPDATE ({MUTABLE}) ON public.ai_switches TO {role}")


def _fk(name: str, columns: str, target: str) -> migrations.RunSQL:
    return migrations.RunSQL(
        f"ALTER TABLE public.ai_switches ADD CONSTRAINT {name} FOREIGN KEY {columns} "
        f"REFERENCES {target}",
        f"ALTER TABLE public.ai_switches DROP CONSTRAINT IF EXISTS {name}",
    )


class Migration(migrations.Migration):
    dependencies = [("ai_gateway", "0008_account_model")]

    operations = [
        migrations.CreateModel(
            name="AISwitch",
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
                ("enabled", models.BooleanField(db_default=False, default=False)),
                ("changed_by_user_id", models.UUIDField(null=True)),
                ("changed_at", models.DateTimeField()),
            ],
            options={
                "db_table": "ai_switches",
                "constraints": [
                    models.UniqueConstraint(fields=("organization_id",), name="ai_switches_org_uq")
                ],
            },
        ),
        EnableRLS("AISwitch"),
        _fk("ai_switches_organization_fk", "(organization_id)", "organizations (id)"),
        # Quien lo cambia es miembro de la organización (ADR-001 §3), como en 0001.
        _fk(
            "ai_switches_changed_by_fk",
            "(organization_id, changed_by_user_id)",
            "organization_memberships (organization_id, user_id)",
        ),
        migrations.RunPython(_grants, migrations.RunPython.noop),
    ]
