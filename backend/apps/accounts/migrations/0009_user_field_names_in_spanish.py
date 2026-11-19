"""F2-131: el nombre en español de `email`, `first_name` y `last_name`, que el mensaje de
`password_too_similar` repite («…similar a la de nombre»). Solo estado: `sqlmigrate` no emite SQL
(un `verbose_name` no llega a la base)."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0008_user_password_change_required"),
    ]

    operations = [
        migrations.AlterField(
            model_name="user",
            name="email",
            field=models.EmailField(max_length=254, unique=True, verbose_name="correo electrónico"),
        ),
        migrations.AlterField(
            model_name="user",
            name="first_name",
            field=models.CharField(blank=True, max_length=150, verbose_name="nombre"),
        ),
        migrations.AlterField(
            model_name="user",
            name="last_name",
            field=models.CharField(blank=True, max_length=150, verbose_name="apellido"),
        ),
    ]
