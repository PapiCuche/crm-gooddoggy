"""F7-08b: valida el `CHECK` de `messages.provider_error_code` que la `0018` añadió `NOT VALID`.
`VALIDATE CONSTRAINT` recorre la tabla con un bloqueo que no impide leer ni escribir. Hacia atrás
no hace nada (el `CHECK` queda, validado)."""

from django.db import migrations

NAME = "messages_error_code_ck"


class Migration(migrations.Migration):
    dependencies = [
        ("conversations", "0018_consent_revoked_code"),
    ]

    operations = [
        migrations.RunSQL(
            f"ALTER TABLE public.messages VALIDATE CONSTRAINT {NAME}", migrations.RunSQL.noop
        ),
    ]
