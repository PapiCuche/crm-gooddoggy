"""F7b-14: una respuesta que falla no da la conversación por contestada. Dos columnas de
`conversations`: `last_reply_failed` (`boolean NOT NULL DEFAULT false`: un valor por omisión
constante, solo catálogo, sin reescribir la tabla) y `human_request_reply_id` (nula: la respuesta
que atendió la solicitud de persona, que se reabre si esa respuesta falla). Sus dos `CHECK` y la FK
compuesta a `messages` (`ON DELETE SET NULL` solo de la columna: un mensaje nunca se borra, salvo al
limpiar las pruebas) nacen `NOT VALID` (rigen ya para toda fila nueva o modificada); la `0060` los
valida. `crm_app` ya tiene `UPDATE` de la tabla (`0001`); la tabla ya tiene RLS con FORCE.
`lock_timeout` de 5 s en los dos sentidos. Hacia atrás, una solicitud atendida por una respuesta
vuelve a la forma de antes (cerrada: sin quién, cuándo ni por qué) en vez de reaparecer abierta."""

from typing import Any

from django.db import migrations, models

LOCK = "SET LOCAL lock_timeout = '5s'"
REPLY_CK, FAILED_CK, FK = (
    "conversations_human_reply_ck",
    "conversations_reply_failed_ck",
    "conversations_human_reply_fk",
)
ADD = (
    LOCK,
    "ALTER TABLE public.conversations"
    " ADD COLUMN last_reply_failed boolean NOT NULL DEFAULT false,"
    " ADD COLUMN human_request_reply_id uuid NULL,"
    f" ADD CONSTRAINT {REPLY_CK} CHECK (human_request_reply_id IS NULL"
    " OR human_requested_at IS NOT NULL) NOT VALID,"
    f" ADD CONSTRAINT {FAILED_CK} CHECK (NOT last_reply_failed OR"
    " (last_message_direction IS NOT NULL AND last_message_direction = 'OUTBOUND')) NOT VALID,"
    f" ADD CONSTRAINT {FK} FOREIGN KEY (organization_id, human_request_reply_id)"
    " REFERENCES public.messages (organization_id, id)"
    " ON DELETE SET NULL (human_request_reply_id) NOT VALID",
)
DROP = (
    LOCK,
    f"ALTER TABLE public.conversations DROP CONSTRAINT {FK}, DROP CONSTRAINT {FAILED_CK},"
    f" DROP CONSTRAINT {REPLY_CK}, DROP COLUMN human_request_reply_id,"
    " DROP COLUMN last_reply_failed",
)


def restore_closed(apps: Any, schema_editor: Any) -> None:
    """Hacia atrás: una solicitud atendida por una respuesta, cerrada como antes de F7b-14."""
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user")
        if not cursor.fetchone()[0]:
            raise RuntimeError(
                "conversations 0059: run as crm_migrator with BYPASSRLS (RLS hides rows)"
            )
        cursor.execute(LOCK)
        cursor.execute(
            "UPDATE public.conversations SET human_requested_at = NULL,"
            " human_request_reason = NULL, human_requested_by_user_id = NULL,"
            " human_request_reply_id = NULL"
            " WHERE human_request_reply_id IS NOT NULL"
        )


class Migration(migrations.Migration):
    dependencies = [
        ("conversations", "0058_validate_error_detail"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AddField(
                    model_name="conversation",
                    name="human_request_reply_id",
                    field=models.UUIDField(blank=True, null=True),
                ),
                migrations.AddField(
                    model_name="conversation",
                    name="last_reply_failed",
                    field=models.BooleanField(db_default=False, default=False),
                ),
                migrations.AddConstraint(
                    model_name="conversation",
                    constraint=models.CheckConstraint(
                        condition=models.Q(human_request_reply_id__isnull=True)
                        | models.Q(human_requested_at__isnull=False),
                        name=REPLY_CK,
                    ),
                ),
                migrations.AddConstraint(
                    model_name="conversation",
                    constraint=models.CheckConstraint(
                        condition=models.Q(last_reply_failed=False)
                        | models.Q(
                            last_message_direction__isnull=False,
                            last_message_direction="OUTBOUND",
                        ),
                        name=FAILED_CK,
                    ),
                ),
            ],
            database_operations=[migrations.RunSQL(ADD, DROP)],
        ),
        migrations.RunPython(migrations.RunPython.noop, restore_closed),
    ]
