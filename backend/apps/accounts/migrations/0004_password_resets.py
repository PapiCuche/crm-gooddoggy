"""F2-86 (ADR-021): `password_resets`, los enlaces de recuperación de contraseña enviados.
Platform-owned (ADR-001 §2): sin `organization_id` ni política de tenant."""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

import core.ids


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0003_user_session_epoch"),
    ]

    operations = [
        migrations.CreateModel(
            name="PasswordReset",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        db_default=models.Func(function="uuidv7", output_field=models.UUIDField()),
                        default=core.ids.new_id,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("token_hash", models.CharField(max_length=64, unique=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("expires_at", models.DateTimeField()),
                ("used_at", models.DateTimeField(null=True)),
                (
                    "user",
                    models.ForeignKey(
                        db_index=False,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "db_table": "password_resets",
                "indexes": [
                    models.Index(fields=["user", "-created_at"], name="password_resets_user_idx")
                ],
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(("token_hash__regex", "^[0-9a-f]{64}$")),
                        name="password_resets_token_hash_ck",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("expires_at__gt", models.F("created_at"))),
                        name="password_resets_expires_ck",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            ("used_at__isnull", True),
                            ("used_at__gte", models.F("created_at")),
                            _connector="OR",
                        ),
                        name="password_resets_used_ck",
                    ),
                ],
            },
        ),
    ]
