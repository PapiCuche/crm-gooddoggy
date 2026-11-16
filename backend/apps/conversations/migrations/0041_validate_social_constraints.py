"""F7b-04a: valida `conversations_contact_identity_fk` y `webhook_ingress_route_ck`, que la `0040`
añadió `NOT VALID`. `VALIDATE CONSTRAINT` recorre la tabla con un bloqueo que no impide leer ni
escribir. Hacia atrás no hace nada (quedan, validados)."""

from django.db import migrations

FK = "conversations_contact_identity_fk"


class Migration(migrations.Migration):
    dependencies = [
        ("conversations", "0040_social_channels"),
    ]

    operations = [
        migrations.RunSQL(
            f"ALTER TABLE public.conversations VALIDATE CONSTRAINT {FK}",
            migrations.RunSQL.noop,
        ),
        migrations.RunSQL(
            "ALTER TABLE public.webhook_ingress VALIDATE CONSTRAINT webhook_ingress_route_ck",
            migrations.RunSQL.noop,
        ),
    ]
