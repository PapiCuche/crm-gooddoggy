"""F2-127: `users.password_change_required`, la marca de una contraseña que escribió un operador
(`add_member`). Con `db_default`, las cuentas que ya existen quedan en `False` sin reescribirse."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0007_mfa_code_attempts"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="password_change_required",
            field=models.BooleanField(db_default=False, default=False),
        ),
    ]
