"""F7-06a: `messages.provider_error_code`, por qué un mensaje saliente quedó `FAILED` (ADR-029 §8):
un código cerrado propio (nunca el texto del proveedor), solo con `FAILED`. Lo escribe
`advance_delivery` junto al estado, así que `crm_app` actualiza la columna como las demás marcas de
entrega. Los `FAILED` anteriores quedan sin código."""

from typing import Any

from django.conf import settings
from django.db import migrations, models

CODES = (
    "CONVERSATION_CLOSED", "CHANNEL_UNAVAILABLE", "CONTACT_BLOCKED", "WINDOW_CLOSED",
    "ADAPTER_REJECTED", "RETRIES_EXHAUSTED", "TOKEN_INVALID", "RECIPIENT_UNREACHABLE",
    "PROVIDER_REJECTED", "PROVIDER_UNKNOWN", "RATE_LIMITED", "QUALITY_BLOCKED",
    "TEMPLATE_UNAVAILABLE", "PROVIDER_ERROR",
)  # fmt: skip


def _grant(apps: Any, schema_editor: Any) -> None:
    role = schema_editor.quote_name(settings.DB_APP_ROLE)
    schema_editor.execute(f"GRANT UPDATE (provider_error_code) ON public.messages TO {role}")


def _revoke(apps: Any, schema_editor: Any) -> None:
    role = schema_editor.quote_name(settings.DB_APP_ROLE)
    schema_editor.execute(f"REVOKE UPDATE (provider_error_code) ON public.messages FROM {role}")


class Migration(migrations.Migration):
    dependencies = [
        ("conversations", "0014_sending_sweeper"),
    ]

    operations = [
        migrations.AddField(
            model_name="message",
            name="provider_error_code",
            field=models.CharField(blank=True, max_length=24, null=True),
        ),
        migrations.RunPython(_grant, _revoke),
        migrations.AddConstraint(
            model_name="message",
            constraint=models.CheckConstraint(
                condition=models.Q(("provider_error_code__isnull", True))
                | models.Q(("delivery_status", "FAILED"), ("provider_error_code__in", CODES)),
                name="messages_error_code_ck",
            ),
        ),
    ]
