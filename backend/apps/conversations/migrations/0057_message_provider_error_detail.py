"""F7b-13: `messages.provider_error_detail`, el error del proveedor en números (`131031`,
`10/2018278`) junto al motivo cerrado de un `FAILED`: nunca el texto, el título ni los detalles de
Meta. Lo escribe `advance_delivery` con el estado, así que `crm_app` actualiza la columna como
`provider_error_code` (`GRANT UPDATE` de esa columna).

`messages` es grande: la columna nace nula (solo catálogo) y su `CHECK` (solo con `FAILED`, solo
dígitos y una `/`) nace `NOT VALID`, con `lock_timeout` de 5 s en los dos sentidos; la `0058` lo
valida. Hacia atrás se borra la columna: es un dato de diagnóstico, nada depende de él."""

from typing import Any

from django.conf import settings
from django.db import migrations, models

LOCK = "SET LOCAL lock_timeout = '5s'"
CK = "messages_error_detail_ck"
PATTERN = "^[0-9]{1,10}(/[0-9]{1,10})?$"


def _grant(apps: Any, schema_editor: Any) -> None:
    role = schema_editor.quote_name(settings.DB_APP_ROLE)
    schema_editor.execute(LOCK)
    schema_editor.execute(f"GRANT UPDATE (provider_error_detail) ON public.messages TO {role}")


def _revoke(apps: Any, schema_editor: Any) -> None:
    role = schema_editor.quote_name(settings.DB_APP_ROLE)
    schema_editor.execute(LOCK)  # hacia atrás corre antes que el `RunSQL`
    schema_editor.execute(f"REVOKE UPDATE (provider_error_detail) ON public.messages FROM {role}")


class Migration(migrations.Migration):
    dependencies = [
        ("conversations", "0056_validate_ai_exclusion"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AddField(
                    model_name="message",
                    name="provider_error_detail",
                    field=models.CharField(blank=True, max_length=24, null=True),
                ),
                migrations.AddConstraint(
                    model_name="message",
                    constraint=models.CheckConstraint(
                        condition=models.Q(provider_error_detail__isnull=True)
                        | models.Q(delivery_status="FAILED", provider_error_detail__regex=PATTERN),
                        name=CK,
                    ),
                ),
            ],
            database_operations=[
                migrations.RunSQL(
                    [
                        LOCK,
                        "ALTER TABLE public.messages ADD COLUMN provider_error_detail varchar(24)"
                        " NULL",
                        f"ALTER TABLE public.messages ADD CONSTRAINT {CK} CHECK"
                        " (provider_error_detail IS NULL OR (delivery_status = 'FAILED' AND"
                        f" provider_error_detail ~ '{PATTERN}')) NOT VALID",
                    ],
                    [
                        LOCK,
                        f"ALTER TABLE public.messages DROP CONSTRAINT {CK}",
                        "ALTER TABLE public.messages DROP COLUMN provider_error_detail",
                    ],
                ),
            ],
        ),
        migrations.RunPython(_grant, _revoke),
    ]
