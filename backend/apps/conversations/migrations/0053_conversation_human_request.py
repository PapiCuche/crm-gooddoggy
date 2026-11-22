"""F8-08n: la solicitud de atención humana de una conversación (quién, cuándo y por qué). Tres
columnas que nacen nulas (solo catálogo) y su `CHECK` `NOT VALID` (sin recorrer la tabla bajo un
bloqueo exclusivo; rige ya para toda fila nueva o modificada), que la `0054` valida. `crm_app` ya
tiene `UPDATE` de la tabla (`0001`); la tabla ya tiene RLS con FORCE. `lock_timeout` de 5 s en los
dos sentidos. Hacia atrás se niega si alguna conversación tiene la solicitud abierta y dice cuántas,
en vez de borrarla (la auditoría guarda el historial, no el estado actual)."""

from typing import Any

from django.db import migrations, models

LOCK = "SET LOCAL lock_timeout = '5s'"
NAME = "conversations_human_request_ck"
REASONS = ("ASKED_FOR_PERSON", "UPSET", "MODEL", "AGENT")
CHOICES = [(code, code.replace("_", " ").title()) for code in REASONS]
LISTED = ", ".join(f"'{code}'" for code in REASONS)
ADD = (
    LOCK,
    "ALTER TABLE public.conversations ADD COLUMN human_requested_at timestamp with time zone NULL,"
    " ADD COLUMN human_request_reason varchar(20) NULL,"
    " ADD COLUMN human_requested_by_user_id uuid NULL",
    f"ALTER TABLE public.conversations ADD CONSTRAINT {NAME} CHECK ((human_requested_at IS NULL"
    " AND human_request_reason IS NULL AND human_requested_by_user_id IS NULL) OR"
    " (human_requested_at IS NOT NULL AND human_requested_by_user_id IS NOT NULL AND"
    f" human_request_reason IS NOT NULL AND human_request_reason IN ({LISTED}))) NOT VALID",
)
DROP = (
    LOCK,
    f"ALTER TABLE public.conversations DROP CONSTRAINT {NAME}, DROP COLUMN human_requested_at,"
    " DROP COLUMN human_request_reason, DROP COLUMN human_requested_by_user_id",
)


def refuse_open(apps: Any, schema_editor: Any) -> None:
    """Hacia atrás: borrar las columnas quitaría las solicitudes abiertas; se niega."""
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user")
        if not cursor.fetchone()[0]:
            raise RuntimeError(
                "conversations 0053: run as crm_migrator with BYPASSRLS (RLS hides rows)"
            )
        cursor.execute(
            "SELECT count(*) FROM public.conversations WHERE human_requested_at IS NOT NULL"
        )
        count = cursor.fetchone()[0]
    if count:
        raise RuntimeError(
            f"conversations 0053: {count} conversations wait for a person; going back would drop it"
        )


class Migration(migrations.Migration):
    dependencies = [
        ("conversations", "0052_channel_health_future_check"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AddField(
                    model_name="conversation",
                    name="human_requested_at",
                    field=models.DateTimeField(blank=True, null=True),
                ),
                migrations.AddField(
                    model_name="conversation",
                    name="human_request_reason",
                    field=models.CharField(blank=True, choices=CHOICES, max_length=20, null=True),
                ),
                migrations.AddField(
                    model_name="conversation",
                    name="human_requested_by_user_id",
                    field=models.UUIDField(blank=True, null=True),
                ),
                migrations.AddConstraint(
                    model_name="conversation",
                    constraint=models.CheckConstraint(
                        condition=models.Q(
                            human_requested_at__isnull=True,
                            human_request_reason__isnull=True,
                            human_requested_by_user_id__isnull=True,
                        )
                        | models.Q(
                            human_requested_at__isnull=False,
                            human_requested_by_user_id__isnull=False,
                            human_request_reason__isnull=False,
                            human_request_reason__in=list(REASONS),
                        ),
                        name=NAME,
                    ),
                ),
            ],
            database_operations=[migrations.RunSQL(ADD, DROP)],
        ),
        migrations.RunPython(migrations.RunPython.noop, refuse_open),
    ]
