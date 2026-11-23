"""F7b-13: valida el `CHECK` de `messages.provider_error_detail` que la `0057` añadió `NOT VALID`
(con `lock_timeout` de 5 s que se quita aunque falle; hacia atrás queda, validado). **No atómica**,
como la `0056`."""

from typing import Any

from django.db import migrations

NAME = "messages_error_detail_ck"


def validate(apps: Any, schema_editor: Any) -> None:
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("SET lock_timeout = '5s'")
        try:
            cursor.execute(f"ALTER TABLE public.messages VALIDATE CONSTRAINT {NAME}")
        finally:
            cursor.execute("RESET lock_timeout")


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ("conversations", "0057_message_provider_error_detail"),
    ]

    operations = [
        migrations.RunPython(validate, migrations.RunPython.noop),
    ]
