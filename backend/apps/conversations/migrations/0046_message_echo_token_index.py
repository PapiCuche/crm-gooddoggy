"""F7b-05c: valida el `messages_echo_token_ck` que la `0045` añadió `NOT VALID` (`VALIDATE
CONSTRAINT` toma `SHARE UPDATE EXCLUSIVE`, que no impide leer ni escribir) y crea, `CONCURRENTLY`,
el índice único de la ficha del eco por cuenta (solo las filas con ficha). No atómica: cada
sentencia en su propia transacción; si el índice falla a medias queda `INVALID` y se borra a mano
(`DROP INDEX CONCURRENTLY messages_echo_token_uq`) antes de repetir. Hacia atrás, quita el
índice."""

from django.db import migrations, models

INDEX = "messages_echo_token_uq"


class Migration(migrations.Migration):
    atomic = False
    dependencies = [
        ("conversations", "0045_message_echo_token"),
    ]

    operations = [
        migrations.RunSQL(
            "ALTER TABLE public.messages VALIDATE CONSTRAINT messages_echo_token_ck",
            migrations.RunSQL.noop,
        ),
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AddConstraint(
                    model_name="message",
                    constraint=models.UniqueConstraint(
                        condition=models.Q(("echo_token__isnull", False)),
                        fields=("organization_id", "channel_account", "echo_token"),
                        name=INDEX,
                    ),
                ),
            ],
            database_operations=[
                migrations.RunSQL(
                    f"CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS {INDEX} ON public.messages"
                    " (organization_id, channel_account_id, echo_token)"
                    " WHERE echo_token IS NOT NULL",
                    f"DROP INDEX CONCURRENTLY IF EXISTS public.{INDEX}",
                ),
            ],
        ),
    ]
