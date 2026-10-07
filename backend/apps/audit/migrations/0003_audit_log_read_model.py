"""F2-73: leer `audit_logs`. El modelo `AuditLog` no gestiona la tabla (`managed = False`): esta
migración solo lo da de alta en el estado de Django. El índice sirve el listado de la API, que
ordena por `id` dentro de la organización; en una tabla particionada se crea en la tabla padre
y PostgreSQL lo repite en cada partición, también en las que se creen después.
"""

from django.db import migrations, models

import core.ids

INDEX = "CREATE INDEX audit_logs_org_id_idx ON public.audit_logs (organization_id, id)"


class Migration(migrations.Migration):
    dependencies = [("audit", "0002_platform_audit_logs")]
    operations = [
        migrations.CreateModel(
            name="AuditLog",
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
                ("occurred_at", models.DateTimeField()),
                ("actor_type", models.CharField(max_length=16)),
                ("actor_id", models.UUIDField(null=True)),
                ("actor_label", models.CharField(max_length=200, null=True)),
                ("action", models.CharField(max_length=100)),
                ("entity_type", models.CharField(max_length=50)),
                ("entity_id", models.UUIDField(null=True)),
                ("entity_label", models.CharField(max_length=200, null=True)),
                ("changes", models.JSONField()),
                ("metadata", models.JSONField()),
                ("correlation_id", models.CharField(max_length=64, null=True)),
                ("result", models.CharField(max_length=8)),
            ],
            options={"db_table": "audit_logs", "managed": False},
        ),
        migrations.RunSQL(INDEX, "DROP INDEX IF EXISTS public.audit_logs_org_id_idx"),
    ]
