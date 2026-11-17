"""F7b-05c (ADR-035 §6): `messages.echo_token`, la ficha aleatoria (128 bits, `secrets`) que un
envío de Instagram o Messenger lleva en `message.metadata` y que su eco devuelve. `crm_app` solo la
escribe (`GRANT UPDATE` de esa columna): el despachador la fija al reclamar el envío.

`messages` es grande: la columna nace nula (solo catálogo), el `CHECK` (solo lo saliente, y con la
forma de `token_hex(16)`) nace `NOT VALID` y el índice único por cuenta lo crea la `0046`,
`CONCURRENTLY`, que también valida el `CHECK`. `lock_timeout` de 5 s en los dos sentidos. Hacia
atrás se niega si algún mensaje ya tiene ficha y dice cuántos, en vez de borrarlas."""

from typing import Any

from django.conf import settings
from django.db import migrations, models

LOCK = "SET LOCAL lock_timeout = '5s'"
CK = "messages_echo_token_ck"


def _grant(apps: Any, schema_editor: Any) -> None:
    role = schema_editor.quote_name(settings.DB_APP_ROLE)
    schema_editor.execute(LOCK)
    schema_editor.execute(f"GRANT UPDATE (echo_token) ON public.messages TO {role}")


def _revoke(apps: Any, schema_editor: Any) -> None:
    role = schema_editor.quote_name(settings.DB_APP_ROLE)
    schema_editor.execute(LOCK)  # hacia atrás corre antes que el `RunSQL`
    schema_editor.execute(f"REVOKE UPDATE (echo_token) ON public.messages FROM {role}")


def refuse_tokens(apps: Any, schema_editor: Any) -> None:
    """Hacia atrás: borrar la columna perdería la correlación de los ecos pendientes; se niega."""
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user")
        if not cursor.fetchone()[0]:
            raise RuntimeError(
                "conversations 0045: run as crm_migrator with BYPASSRLS (RLS hides rows)"
            )
        cursor.execute("SELECT count(*) FROM public.messages WHERE echo_token IS NOT NULL")
        count = cursor.fetchone()[0]
    if count:
        raise RuntimeError(
            f"conversations 0045: {count} messages have an echo token; going back would drop them"
        )


class Migration(migrations.Migration):
    dependencies = [
        ("conversations", "0044_validate_messages_access_off"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AddField(
                    model_name="message",
                    name="echo_token",
                    field=models.CharField(blank=True, max_length=32, null=True),
                ),
                migrations.AddConstraint(
                    model_name="message",
                    constraint=models.CheckConstraint(
                        condition=models.Q(("echo_token__isnull", True))
                        | models.Q(
                            ("direction", "OUTBOUND"), ("echo_token__regex", "^[0-9a-f]{32}$")
                        ),
                        name=CK,
                    ),
                ),
            ],
            database_operations=[
                migrations.RunSQL(
                    [
                        LOCK,
                        "ALTER TABLE public.messages ADD COLUMN echo_token varchar(32) NULL",
                        f"ALTER TABLE public.messages ADD CONSTRAINT {CK} CHECK (echo_token IS "
                        "NULL OR (direction = 'OUTBOUND' AND echo_token ~ '^[0-9a-f]{32}$'))"
                        " NOT VALID",
                    ],
                    [
                        LOCK,
                        f"ALTER TABLE public.messages DROP CONSTRAINT {CK}",
                        "ALTER TABLE public.messages DROP COLUMN echo_token",
                    ],
                ),
            ],
        ),
        migrations.RunPython(_grant, _revoke),
        migrations.RunPython(migrations.RunPython.noop, refuse_tokens),
    ]
