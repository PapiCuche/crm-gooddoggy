"""F7-05a: la decisión de la política de envío (ADR-010) se guarda en el mensaje saliente. Se
escribe al encolar y la despachadora la reescribe al revaluar justo antes de enviar, así que
`crm_app` puede actualizar esa columna (como las de entrega, `0002`); el resto no se reescribe."""

from typing import Any

from django.conf import settings
from django.db import migrations, models


def _grant(apps: Any, schema_editor: Any) -> None:
    role = schema_editor.quote_name(settings.DB_APP_ROLE)
    schema_editor.execute(f"GRANT UPDATE (policy_decision) ON public.messages TO {role}")


def _revoke(apps: Any, schema_editor: Any) -> None:
    role = schema_editor.quote_name(settings.DB_APP_ROLE)
    schema_editor.execute(f"REVOKE UPDATE (policy_decision) ON public.messages FROM {role}")


class Migration(migrations.Migration):
    dependencies = [
        ("conversations", "0009_webhook_processing"),
    ]

    operations = [
        migrations.AddField(
            model_name="message",
            name="policy_decision",
            field=models.JSONField(blank=True, null=True),
        ),
        migrations.RunPython(_grant, _revoke),
        migrations.AddConstraint(
            model_name="message",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("policy_decision__isnull", True), ("direction", "OUTBOUND"), _connector="OR"
                ),
                name="messages_policy_ck",
            ),
        ),
    ]
