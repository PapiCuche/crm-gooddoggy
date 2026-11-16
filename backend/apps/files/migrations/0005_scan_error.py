"""F7-15 (ADR-008): el antivirus. En el estado del modelo, `scan_status` admite `ERROR` (sin
veredicto tras los reintentos; nunca se sirve) y `scan_checked_at` guarda el último intento, para
que el barredor vuelva a mirar un `PENDING` sin tarea viva. El `CHECK` de la base
(`files_values_ck`) lo cambia `conversations.0035`, como hizo la `0029`: deshacer y rehacer
`conversations` lo lleva consigo."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("files", "0004_created_by_member_fk"),
    ]

    operations = [
        migrations.AddField(
            model_name="file",
            name="scan_checked_at",
            field=models.DateTimeField(null=True),
        ),
        migrations.AlterField(
            model_name="file",
            name="scan_status",
            field=models.CharField(
                choices=[
                    ("PENDING", "Pending"),
                    ("CLEAN", "Clean"),
                    ("INFECTED", "Infected"),
                    ("ERROR", "Error"),
                ],
                default="PENDING",
                max_length=10,
            ),
        ),
    ]
