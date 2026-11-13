"""F7-09b2: el propósito `message-media-out` en el estado del modelo. El `CHECK` de la base
(`files_values_ck`) lo cambia `conversations.0029`, junto a la regla de los mensajes salientes."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("files", "0002_files_org_id_uq"),
    ]

    operations = [
        migrations.AlterField(
            model_name="file",
            name="purpose",
            field=models.CharField(
                choices=[
                    ("message-media", "Message Media"),
                    ("message-media-out", "Message Media Out"),
                    ("quote-pdf", "Quote Pdf"),
                    ("import", "Import"),
                    ("avatar", "Avatar"),
                    ("kb", "Kb"),
                    ("attachment", "Attachment"),
                ],
                max_length=20,
            ),
        ),
    ]
