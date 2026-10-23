from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("contacts", "0001_contacts")]

    operations = [
        # F4-02: el alcance `OWN` filtra por el responsable o por el creador.
        migrations.AddIndex(
            model_name="contact",
            index=models.Index(
                fields=["organization_id", "created_by_user_id"], name="contacts_org_created_by_idx"
            ),
        ),
    ]
