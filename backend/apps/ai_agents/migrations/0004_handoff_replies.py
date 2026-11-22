"""F8-08l (#1046; ADR-034 §8): el texto de traspaso de cada organización, `ai_handoff_replies`.

Una fila por organización como mucho; sin fila, el copiloto propone el inicial
(`AI_HANDOFF_REPLY`, el del propietario), así que las organizaciones que ya existen no necesitan
datos. Una tabla propia y no una columna de `ai_provider_accounts`: esas son una por proveedor, de
`ai_gateway` (L1, que no importa el copiloto) y pueden no existir o estar desactivadas; el texto
es de la organización y del copiloto. FORCE RLS con la política única `tenant_isolation`.
`crm_app` inserta la fila de su organización (el `id`, por omisión) y cambia solo el texto, quién y
cuándo; nunca la borra, ni su `id` ni su organización. El texto, de 1 a 500 caracteres (la
aplicación lo valida antes: `detectors.reply_problems`). Sin función SECURITY DEFINER: la escribe
la ruta como `crm_app` bajo RLS, como el interruptor (`ai_gateway` 0009). Hacia atrás se borra la
tabla (los textos guardados se pierden y vuelve el inicial)."""

from typing import Any

import django.db.models.functions.text
import django.db.models.lookups
from django.conf import settings
from django.db import migrations, models

import core.ids
from core.db.operations import EnableRLS

MUTABLE = "text, changed_by_user_id, changed_at"


def _grants(apps: Any, schema_editor: Any) -> None:
    role = schema_editor.quote_name(settings.DB_APP_ROLE)
    table = "public.ai_handoff_replies"
    schema_editor.execute(f"REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON {table} FROM {role}")
    schema_editor.execute(f"GRANT INSERT (organization_id, {MUTABLE}) ON {table} TO {role}")
    schema_editor.execute(f"GRANT UPDATE ({MUTABLE}) ON {table} TO {role}")


def _fk(name: str, columns: str, target: str) -> migrations.RunSQL:
    return migrations.RunSQL(
        f"ALTER TABLE public.ai_handoff_replies ADD CONSTRAINT {name} FOREIGN KEY {columns} "
        f"REFERENCES {target}",
        f"ALTER TABLE public.ai_handoff_replies DROP CONSTRAINT IF EXISTS {name}",
    )


class Migration(migrations.Migration):
    dependencies = [("ai_agents", "0003_findings_types")]

    operations = [
        migrations.CreateModel(
            name="AIHandoffReply",
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
                ("text", models.TextField()),
                ("changed_by_user_id", models.UUIDField(null=True)),
                ("changed_at", models.DateTimeField()),
            ],
            options={
                "db_table": "ai_handoff_replies",
                "constraints": [
                    models.UniqueConstraint(
                        fields=("organization_id",), name="ai_handoff_replies_org_uq"
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            django.db.models.lookups.GreaterThanOrEqual(
                                django.db.models.functions.text.Length("text"), 1
                            ),
                            django.db.models.lookups.LessThanOrEqual(
                                django.db.models.functions.text.Length("text"), 500
                            ),
                        ),
                        name="ai_handoff_replies_text_ck",
                    ),
                ],
            },
        ),
        EnableRLS("AIHandoffReply"),
        _fk("ai_handoff_replies_organization_fk", "(organization_id)", "organizations (id)"),
        # Quien lo cambia es miembro de la organización (ADR-001 §3), como en `ai_switches`.
        _fk(
            "ai_handoff_replies_changed_by_fk",
            "(organization_id, changed_by_user_id)",
            "organization_memberships (organization_id, user_id)",
        ),
        migrations.RunPython(_grants, migrations.RunPython.noop),
    ]
