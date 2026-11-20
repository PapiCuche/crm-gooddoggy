"""F2-130e (ADR-020): si el servidor de correo aceptó el enlace vigente de una invitación
(`delivered_at`, que la tarea de envío anota después de entregar: un enlace emitido sin anotar se
enseña como no entregado) y cuándo salió cada envío (`send_times`), para el tope diario de envíos a
un mismo correo. De lo enviado antes solo se sabe el último envío; su entrega, no."""

import django.contrib.postgres.fields
import django.db.models.lookups
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("organizations", "0011_invitation_lookup"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="userinvitation",
            name="delivered_at",
            field=models.DateTimeField(null=True),
        ),
        migrations.AddField(
            model_name="userinvitation",
            name="send_times",
            field=django.contrib.postgres.fields.ArrayField(
                base_field=models.DateTimeField(),
                db_default=models.Value(
                    [],
                    django.contrib.postgres.fields.ArrayField(
                        base_field=models.DateTimeField(), size=None
                    ),
                ),
                default=list,
                size=None,
            ),
        ),
        migrations.RunSQL(
            "UPDATE user_invitations SET send_times = ARRAY[sent_at] WHERE sent_at IS NOT NULL",
            migrations.RunSQL.noop,
        ),
        migrations.AddConstraint(
            model_name="userinvitation",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("delivered_at__isnull", True),
                    models.Q(
                        ("delivered_at__gte", models.F("sent_at")), ("sent_at__isnull", False)
                    ),
                    _connector="OR",
                ),
                name="user_invitations_delivered_ck",
            ),
        ),
        migrations.AddConstraint(
            model_name="userinvitation",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    django.db.models.lookups.LessThanOrEqual(
                        models.Func(
                            models.F("send_times"),
                            function="cardinality",
                            output_field=models.IntegerField(),
                        ),
                        models.F("send_count"),
                    )
                ),
                name="user_invitations_send_times_ck",
            ),
        ),
    ]
