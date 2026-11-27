"""F11-03: valida las dos FKs que la `0002` añadió `NOT VALID`. `VALIDATE CONSTRAINT` recorre
`tasks` con `SHARE UPDATE EXCLUSIVE` (no impide leer ni escribir) y toma `ROW SHARE` sobre
`conversations` y `quotes`; `lock_timeout` de 5 s que se quita aunque falle. Hacia atrás quedan
validadas. **No atómica**, como `conversations` 0062."""

from typing import Any

from django.db import migrations

NAMES = ("tasks_conversation_fk", "tasks_quote_fk")


def validate(apps: Any, schema_editor: Any) -> None:
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("SET lock_timeout = '5s'")
        try:
            for name in NAMES:
                cursor.execute(f"ALTER TABLE public.tasks VALIDATE CONSTRAINT {name}")
        finally:
            cursor.execute("RESET lock_timeout")


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ("tasks", "0002_task_links"),
    ]

    operations = [
        migrations.RunPython(validate, migrations.RunPython.noop),
    ]
