"""F7-05a: `contacts.is_blocked` (ADR-010 §1): un contacto bloqueado no recibe mensajes. Solo la
columna; el comando que la cambia es de F7-08."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("contacts", "0006_contact_tags"),
    ]

    operations = [
        migrations.AddField(
            model_name="contact",
            name="is_blocked",
            field=models.BooleanField(db_default=False, default=False),
        ),
    ]
