"""F7b-06b (ADR-035 §4): `conversations.standby_since`, desde cuándo otra app controla un hilo de
Instagram o Messenger. `crm_app` la escribe (`GRANT UPDATE` de esa columna) al registrar un
entrante. La columna nace nula (solo catálogo); `lock_timeout` de 5 s en los dos sentidos. Hacia
atrás se niega si alguna conversación está en `standby` y dice cuántas, en vez de borrar el aviso
(el estado no se puede reconstruir: los mensajes no lo guardan)."""

from typing import Any

from django.conf import settings
from django.db import migrations, models

LOCK = "SET LOCAL lock_timeout = '5s'"


def _grant(apps: Any, schema_editor: Any) -> None:
    role = schema_editor.quote_name(settings.DB_APP_ROLE)
    schema_editor.execute(LOCK)
    schema_editor.execute(f"GRANT UPDATE (standby_since) ON public.conversations TO {role}")


def _revoke(apps: Any, schema_editor: Any) -> None:
    role = schema_editor.quote_name(settings.DB_APP_ROLE)
    schema_editor.execute(LOCK)  # hacia atrás corre antes que el `RunSQL`
    schema_editor.execute(f"REVOKE UPDATE (standby_since) ON public.conversations FROM {role}")


def refuse_standby(apps: Any, schema_editor: Any) -> None:
    """Hacia atrás: borrar la columna quitaría el aviso de los hilos en `standby`; se niega."""
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user")
        if not cursor.fetchone()[0]:
            raise RuntimeError(
                "conversations 0047: run as crm_migrator with BYPASSRLS (RLS hides rows)"
            )
        cursor.execute("SELECT count(*) FROM public.conversations WHERE standby_since IS NOT NULL")
        count = cursor.fetchone()[0]
    if count:
        raise RuntimeError(
            f"conversations 0047: {count} conversations are in standby; going back would drop it"
        )


class Migration(migrations.Migration):
    dependencies = [
        ("conversations", "0046_message_echo_token_index"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AddField(
                    model_name="conversation",
                    name="standby_since",
                    field=models.DateTimeField(blank=True, null=True),
                ),
            ],
            database_operations=[
                migrations.RunSQL(
                    [
                        LOCK,
                        "ALTER TABLE public.conversations "
                        "ADD COLUMN standby_since timestamp with time zone NULL",
                    ],
                    [LOCK, "ALTER TABLE public.conversations DROP COLUMN standby_since"],
                ),
            ],
        ),
        migrations.RunPython(_grant, _revoke),
        migrations.RunPython(migrations.RunPython.noop, refuse_standby),
    ]
