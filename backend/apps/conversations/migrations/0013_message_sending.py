"""F7-05b: el estado de entrega `SENDING` (ADR-029 §6). Un despachador reclama el mensaje
(`QUEUED` → `SENDING`) y llama al canal fuera de la transacción; si el resultado es ambiguo no se
reenvía. Cambia el `CHECK` de los valores y el disparador `messages_delivery_advance` (el orden pasa
a `QUEUED`, `SENDING`, `SENT`, `DELIVERED`, `READ`; `FAILED` también desde `SENDING`, y solo de
`SENDING` se puede volver a `QUEUED`: devolver un reclamo cuando el canal seguro no recibió nada), y
añade `sending_at` (cuándo se reclamó), que `crm_app` puede actualizar como las demás marcas."""

from typing import Any

from django.conf import settings
from django.db import migrations, models

TRIGGER = """
CREATE OR REPLACE FUNCTION public.messages_delivery_advance() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public, pg_temp AS $fn$
DECLARE ranks text[] := ARRAY[{ranks}];
BEGIN
    IF OLD.delivery_status IS NULL OR OLD.delivery_status = 'FAILED' OR (
        NEW.delivery_status = 'FAILED' AND OLD.delivery_status IN ('DELIVERED', 'READ')
    ) OR (
        NEW.delivery_status <> 'FAILED'
        AND pg_catalog.array_position(ranks, NEW.delivery_status)
            <= pg_catalog.array_position(ranks, OLD.delivery_status){release}
    ) THEN
        RAISE EXCEPTION 'messages_delivery_advance: el estado de entrega solo avanza'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$fn$;
"""
OLD = TRIGGER.format(ranks="'QUEUED', 'SENT', 'DELIVERED', 'READ'", release="")
NEW = TRIGGER.format(
    ranks="'QUEUED', 'SENDING', 'SENT', 'DELIVERED', 'READ'",
    release="\n            AND NOT (OLD.delivery_status = 'SENDING' AND NEW.delivery_status = 'QUEUED')",  # noqa: E501
)
# Al volver atrás lo que sigue `SENDING` pasa a `FAILED` (el disparador nuevo aún lo admite).
BACK: list[str | tuple[str, None]] = [
    "UPDATE public.messages SET delivery_status = 'FAILED', failed_at = now() "
    "WHERE delivery_status = 'SENDING'",
    OLD,
]


def _grant(apps: Any, schema_editor: Any) -> None:
    role = schema_editor.quote_name(settings.DB_APP_ROLE)
    schema_editor.execute(f"GRANT UPDATE (sending_at) ON public.messages TO {role}")


def _revoke(apps: Any, schema_editor: Any) -> None:
    role = schema_editor.quote_name(settings.DB_APP_ROLE)
    schema_editor.execute(f"REVOKE UPDATE (sending_at) ON public.messages FROM {role}")


class Migration(migrations.Migration):
    dependencies = [
        ("conversations", "0012_messaging_policy"),
    ]

    operations = [
        migrations.AddField(
            model_name="message",
            name="sending_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RunPython(_grant, _revoke),
        migrations.RemoveConstraint(model_name="message", name="messages_delivery_values_ck"),
        migrations.AddConstraint(
            model_name="message",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("delivery_status__isnull", True),
                    (
                        "delivery_status__in",
                        ["QUEUED", "SENDING", "SENT", "DELIVERED", "READ", "FAILED"],
                    ),
                    _connector="OR",
                ),
                name="messages_delivery_values_ck",
            ),
        ),
        migrations.AlterField(
            model_name="message",
            name="delivery_status",
            field=models.CharField(
                blank=True,
                choices=[
                    ("QUEUED", "Queued"),
                    ("SENDING", "Sending"),
                    ("SENT", "Sent"),
                    ("DELIVERED", "Delivered"),
                    ("READ", "Read"),
                    ("FAILED", "Failed"),
                ],
                max_length=10,
                null=True,
            ),
        ),
        migrations.RunSQL(NEW, list(BACK)),
    ]
