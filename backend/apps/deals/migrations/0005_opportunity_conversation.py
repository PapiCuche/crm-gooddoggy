"""F6-06b: la conversación de origen de una oportunidad. `conversation_id` es nulo, y la FK
compuesta `(organization_id, conversation_id)` a `conversations` impide una de otra organización
(la de otro contacto la rechaza el comando). Atrás: quita la FK y la columna."""

from django.db import migrations, models

FK = "opportunities_conversation_fk"


class Migration(migrations.Migration):
    dependencies = [
        ("deals", "0004_lost_reason_requires_note"),
        ("conversations", "0001_inbox_tables"),
    ]

    operations = [
        migrations.AddField(
            model_name="opportunity",
            name="conversation_id",
            field=models.UUIDField(blank=True, null=True),
        ),
        migrations.RunSQL(
            f"ALTER TABLE public.opportunities ADD CONSTRAINT {FK} FOREIGN KEY "
            "(organization_id, conversation_id) REFERENCES conversations (organization_id, id)",
            f"ALTER TABLE public.opportunities DROP CONSTRAINT IF EXISTS {FK}",
        ),
    ]
