from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("contacts", "0002_contacts_created_by_index")]

    operations = [
        # F4-07: el teléfono se busca por prefijo E.164 (`^@`). Con `varchar_pattern_ops` el índice
        # sirve ese prefijo con cualquier collation, y sigue sirviendo la igualdad.
        migrations.RemoveIndex(model_name="contact", name="contacts_org_phone_idx"),
        migrations.AddIndex(
            model_name="contact",
            index=models.Index(
                fields=["organization_id", "primary_phone_e164"],
                opclasses=["uuid_ops", "varchar_pattern_ops"],
                name="contacts_org_phone_idx",
            ),
        ),
    ]
