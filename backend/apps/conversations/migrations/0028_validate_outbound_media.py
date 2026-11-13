"""F7-09b: valida lo que la `0027` añadió `NOT VALID` (`VALIDATE CONSTRAINT` recorre la tabla con
`SHARE UPDATE EXCLUSIVE`, que no impide leer ni escribir) y crea, `CONCURRENTLY`, el índice parcial
que respalda la FK a `files` (solo las filas con archivo). No atómica: cada sentencia en su propia
transacción; si el índice falla a medias queda `INVALID` y se borra a mano
(`DROP INDEX CONCURRENTLY messages_file_idx`) antes de repetir. Hacia atrás: quita el índice."""

from django.db import migrations

VALIDATED = (
    "messages_file_fk", "messages_type_ck", "messages_unsupported_ck", "messages_error_code_ck",
    "messages_file_ck",
)  # fmt: skip


class Migration(migrations.Migration):
    atomic = False
    dependencies = [
        ("conversations", "0027_outbound_media"),
    ]

    operations = [
        migrations.RunSQL(
            [f"ALTER TABLE public.messages VALIDATE CONSTRAINT {name}" for name in VALIDATED],
            migrations.RunSQL.noop,
        ),
        migrations.RunSQL(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS messages_file_idx ON public.messages "
            "(organization_id, file_id) WHERE file_id IS NOT NULL",
            "DROP INDEX CONCURRENTLY IF EXISTS public.messages_file_idx",
        ),
    ]
