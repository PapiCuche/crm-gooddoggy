"""F7-15: valida el `files_values_ck` que la `0035` añadió `NOT VALID` (`SHARE UPDATE EXCLUSIVE`:
no impide leer ni escribir)."""

from django.db import migrations


class Migration(migrations.Migration):
    atomic = False
    dependencies = [
        ("conversations", "0035_file_scan_error"),
    ]

    operations = [
        migrations.RunSQL(
            "ALTER TABLE public.files VALIDATE CONSTRAINT files_values_ck",
            migrations.RunSQL.noop,
        ),
    ]
