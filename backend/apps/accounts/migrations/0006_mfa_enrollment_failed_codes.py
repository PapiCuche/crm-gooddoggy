"""F2-101 (ADR-024): `user_mfa_devices.failed_codes`, los códigos que no valieron al confirmar
un alta sin confirmar. El que agota el alta la borra: la fila nunca guarda el tope."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0005_mfa_devices_and_backup_codes"),
    ]

    operations = [
        migrations.AddField(
            model_name="mfadevice",
            name="failed_codes",
            field=models.PositiveSmallIntegerField(db_default=0, default=0),
        ),
        migrations.AddConstraint(
            model_name="mfadevice",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("confirmed_at__isnull", True), ("failed_codes__lt", 5)),
                    models.Q(("confirmed_at__isnull", False), ("failed_codes", 0)),
                    _connector="OR",
                ),
                name="user_mfa_devices_failed_codes_ck",
            ),
        ),
    ]
