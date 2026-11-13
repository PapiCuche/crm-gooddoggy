"""F7-09a2b: valida los dos `CHECK` de `messages` que la `0024` añadió `NOT VALID`. `VALIDATE
CONSTRAINT` recorre la tabla con un bloqueo `SHARE UPDATE EXCLUSIVE`, que no impide leer ni
escribir; va en su propia migración (su propia transacción) para no recorrerla bajo el bloqueo
exclusivo de la `0024`. Hacia atrás no hace nada (los `CHECK` quedan, validados)."""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("conversations", "0024_media_message_types"),
    ]

    operations = [
        migrations.RunSQL(
            [
                f"ALTER TABLE public.messages VALIDATE CONSTRAINT {name}"
                for name in ("messages_type_ck", "messages_unsupported_ck")
            ],
            migrations.RunSQL.noop,
        ),  # fmt: skip
    ]
