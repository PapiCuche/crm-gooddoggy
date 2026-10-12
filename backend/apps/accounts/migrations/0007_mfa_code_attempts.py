"""F2-102 (ADR-023 §5): `user_mfa_code_attempts`, los intentos de código del segundo factor
contados contra una cuenta. Platform-owned (ADR-001 §2): sin `organization_id` ni política de
tenant."""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

import core.ids


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0006_mfa_enrollment_failed_codes"),
    ]

    operations = [
        migrations.CreateModel(
            name="MfaCodeAttempt",
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
                ("created_at", models.DateTimeField()),
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
                "db_table": "user_mfa_code_attempts",
                "indexes": [
                    models.Index(
                        fields=["user", "created_at"], name="user_mfa_code_attempts_user_ix"
                    )
                ],
            },
        ),
    ]
