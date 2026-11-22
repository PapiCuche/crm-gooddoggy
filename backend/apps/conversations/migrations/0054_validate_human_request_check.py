"""F8-08n: valida el `CHECK` de la solicitud de atención humana que la `0053` añadió `NOT VALID`.
`VALIDATE CONSTRAINT` recorre la tabla con un bloqueo que no impide leer ni escribir. Hacia atrás
no hace nada (el `CHECK` queda, validado)."""

from django.db import migrations

NAME = "conversations_human_request_ck"


class Migration(migrations.Migration):
    dependencies = [
        ("conversations", "0053_conversation_human_request"),
    ]

    operations = [
        migrations.RunSQL(
            f"ALTER TABLE public.conversations VALIDATE CONSTRAINT {NAME}", migrations.RunSQL.noop
        ),
    ]
