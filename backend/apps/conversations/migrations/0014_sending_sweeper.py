"""F7-05c: lo que el barredor de `SENDING` y el de la cola necesitan (ADR-029 §6).

`dispatch_attempts` y `not_before` (cuántas veces se devolvió el reclamo y no antes de cuándo se
reintenta: el barredor de la cola no reencola antes, así que no amplifica un «demasiado rápido»
persistente), un índice parcial de los `SENDING` y una función `SECURITY DEFINER` que solo devuelve
ids de organizaciones con un `SENDING` más viejo que el plazo (ADR-002 §3.3). `crm_app` actualiza
las dos columnas."""

from typing import Any

from django.conf import settings
from django.db import migrations, models

from core.db.operations import SecurityDefinerFunction


def _grant(apps: Any, schema_editor: Any) -> None:
    role = schema_editor.quote_name(settings.DB_APP_ROLE)
    schema_editor.execute(
        f"GRANT UPDATE (dispatch_attempts, not_before) ON public.messages TO {role}"
    )


def _revoke(apps: Any, schema_editor: Any) -> None:
    role = schema_editor.quote_name(settings.DB_APP_ROLE)
    schema_editor.execute(
        f"REVOKE UPDATE (dispatch_attempts, not_before) ON public.messages FROM {role}"
    )


class Migration(migrations.Migration):
    dependencies = [
        ("conversations", "0013_message_sending"),
    ]

    operations = [
        migrations.AddField(
            model_name="message",
            name="dispatch_attempts",
            field=models.PositiveSmallIntegerField(db_default=0, default=0),
        ),
        migrations.AddField(
            model_name="message",
            name="not_before",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RunPython(_grant, _revoke),
        migrations.AddIndex(
            model_name="message",
            index=models.Index(
                condition=models.Q(("delivery_status", "SENDING")),
                fields=["organization_id", "sending_at"],
                name="messages_sending_idx",
            ),
        ),
        SecurityDefinerFunction(
            "messages_sending_organizations",
            arguments="p_before timestamptz, p_limit integer",
            argtypes="timestamptz, integer",
            returns="SETOF uuid",
            body="SELECT organization_id FROM public.messages WHERE delivery_status = 'SENDING' "
            "AND sending_at <= p_before GROUP BY organization_id ORDER BY min(sending_at) "
            "LIMIT greatest(0, least(coalesce(p_limit, 0), 1000))",
            search_path="pg_catalog, public, pg_temp",
        ),
    ]
