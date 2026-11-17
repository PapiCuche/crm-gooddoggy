"""F7b-05a: valida el `messages_error_code_ck` que la `0043` añadió `NOT VALID` (`VALIDATE
CONSTRAINT` toma `SHARE UPDATE EXCLUSIVE`, que no impide leer ni escribir). No atómica, como la
`0019`. Hacia atrás no hace nada: la `0043` vuelve al `CHECK` anterior."""

from django.db import migrations


class Migration(migrations.Migration):
    atomic = False
    dependencies = [
        ("conversations", "0043_messages_access_off"),
    ]

    operations = [
        migrations.RunSQL(
            "ALTER TABLE public.messages VALIDATE CONSTRAINT messages_error_code_ck",
            migrations.RunSQL.noop,
        ),
    ]
