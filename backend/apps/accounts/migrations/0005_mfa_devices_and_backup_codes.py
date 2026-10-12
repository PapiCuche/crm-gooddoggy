"""F2-95 (ADR-023): `user_mfa_devices` y `user_mfa_backup_codes`, el segundo factor de una
cuenta. Platform-owned (ADR-001 §2): sin `organization_id` ni política de tenant."""

import django.db.models.deletion
import django.db.models.functions.text
import django.db.models.lookups
from django.conf import settings
from django.db import migrations, models

import core.ids


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0004_password_resets"),
    ]

    operations = [
        migrations.CreateModel(
            name="MfaBackupCode",
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
                ("code_hash", models.CharField(max_length=64)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
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
                "db_table": "user_mfa_backup_codes",
                "constraints": [
                    models.UniqueConstraint(
                        fields=("user", "code_hash"), name="user_mfa_backup_codes_user_hash_uq"
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("code_hash__regex", "^[0-9a-f]{64}$")),
                        name="user_mfa_backup_codes_code_hash_ck",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            ("used_at__isnull", True),
                            ("used_at__gte", models.F("created_at")),
                            _connector="OR",
                        ),
                        name="user_mfa_backup_codes_used_ck",
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="MfaDevice",
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
                ("type", models.CharField(choices=[("TOTP", "Totp")], max_length=16)),
                ("secret", models.BinaryField(max_length=4127)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("confirmed_at", models.DateTimeField(null=True)),
                ("last_step", models.BigIntegerField(null=True)),
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
                "db_table": "user_mfa_devices",
                "constraints": [
                    models.UniqueConstraint(
                        fields=("user", "type"), name="user_mfa_devices_user_type_uq"
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("type__in", ["TOTP"])), name="user_mfa_devices_type_ck"
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            django.db.models.lookups.GreaterThanOrEqual(
                                django.db.models.functions.text.Length("secret"), 32
                            ),
                            django.db.models.lookups.LessThanOrEqual(
                                django.db.models.functions.text.Length("secret"), 4127
                            ),
                        ),
                        name="user_mfa_devices_secret_ck",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            ("confirmed_at__isnull", True),
                            ("confirmed_at__gte", models.F("created_at")),
                            _connector="OR",
                        ),
                        name="user_mfa_devices_confirmed_ck",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            ("last_step__isnull", True),
                            models.Q(("last_step__gt", 0), ("confirmed_at__isnull", False)),
                            _connector="OR",
                        ),
                        name="user_mfa_devices_last_step_ck",
                    ),
                ],
            },
        ),
    ]
