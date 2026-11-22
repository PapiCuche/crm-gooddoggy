"""F8-13: la conversación excluida de la IA (cuándo y quién). Dos columnas que nacen nulas (solo
catálogo) y su `CHECK` `NOT VALID` (van juntas o ninguna), que la `0056` valida. `crm_app` ya tiene
`UPDATE` de toda la tabla (`0001`): una concesión por columna no lo estrecharía, así que solo el
comando las escribe por la aplicación. La tabla ya tiene RLS con FORCE. `lock_timeout` de 5 s.
Hacia atrás se niega si alguna conversación está excluida: borrar la marca la volvería a mandar a la
IA sin que nadie lo decidiera."""

from typing import Any

from django.db import migrations, models

LOCK = "SET LOCAL lock_timeout = '5s'"
NAME = "conversations_ai_excluded_ck"
ADD = (
    LOCK,
    "ALTER TABLE public.conversations ADD COLUMN ai_excluded_at timestamp with time zone NULL,"
    " ADD COLUMN ai_excluded_by_user_id uuid NULL",
    f"ALTER TABLE public.conversations ADD CONSTRAINT {NAME} CHECK ((ai_excluded_at IS NULL"
    " AND ai_excluded_by_user_id IS NULL) OR (ai_excluded_at IS NOT NULL AND"
    " ai_excluded_by_user_id IS NOT NULL)) NOT VALID",
)
DROP = (
    LOCK,
    f"ALTER TABLE public.conversations DROP CONSTRAINT {NAME}, DROP COLUMN ai_excluded_at,"
    " DROP COLUMN ai_excluded_by_user_id",
)


def refuse_excluded(apps: Any, schema_editor: Any) -> None:
    """Hacia atrás: borrar las columnas quitaría exclusiones vigentes; se niega."""
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user")
        if not cursor.fetchone()[0]:
            raise RuntimeError(
                "conversations 0055: run as crm_migrator with BYPASSRLS (RLS hides rows)"
            )
        cursor.execute("SELECT count(*) FROM public.conversations WHERE ai_excluded_at IS NOT NULL")
        count = cursor.fetchone()[0]
    if count:
        raise RuntimeError(
            f"conversations 0055: {count} conversations are excluded from AI; going back would"
            " send them to the AI again"
        )


class Migration(migrations.Migration):
    dependencies = [
        ("conversations", "0054_validate_human_request_check"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AddField(
                    model_name="conversation",
                    name="ai_excluded_at",
                    field=models.DateTimeField(blank=True, null=True),
                ),
                migrations.AddField(
                    model_name="conversation",
                    name="ai_excluded_by_user_id",
                    field=models.UUIDField(blank=True, null=True),
                ),
                migrations.AddConstraint(
                    model_name="conversation",
                    constraint=models.CheckConstraint(
                        condition=models.Q(
                            ai_excluded_at__isnull=True, ai_excluded_by_user_id__isnull=True
                        )
                        | models.Q(
                            ai_excluded_at__isnull=False, ai_excluded_by_user_id__isnull=False
                        ),
                        name=NAME,
                    ),
                ),
            ],
            database_operations=[migrations.RunSQL(ADD, DROP)],
        ),
        migrations.RunPython(migrations.RunPython.noop, refuse_excluded),
    ]
