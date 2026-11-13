"""F7-09b2: valida el `files_values_ck` que la `0029` añadió `NOT VALID` (`VALIDATE CONSTRAINT`
recorre la tabla con `SHARE UPDATE EXCLUSIVE`, que no impide leer ni escribir)."""

from django.db import migrations


class Migration(migrations.Migration):
    atomic = False
    dependencies = [
        ("conversations", "0029_outbound_file_purpose"),
    ]

    operations = [
        migrations.RunSQL(
            "ALTER TABLE public.files VALIDATE CONSTRAINT files_values_ck",
            migrations.RunSQL.noop,
        ),
    ]
