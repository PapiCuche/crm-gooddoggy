"""F2-79: `user_invitations` (ADR-020; modelo de datos §E.2). Tenant-owned, con RLS forzado y FK a
su organización. Una sola invitación pendiente por correo; el correo, en minúsculas y en ASCII;
al menos un rol; del enlace solo cabe un SHA-256.
"""

import django.contrib.postgres.fields
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

import core.ids
from core.db.operations import EnableRLS

FK = "user_invitations_organization_fk"


class Migration(migrations.Migration):
    dependencies = [
        ("organizations", "0008_membership_default_branch"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="UserInvitation",
            fields=[
                ("organization_id", models.UUIDField(db_index=True, editable=False)),
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
                ("email", models.CharField(max_length=254)),
                (
                    "role_ids",
                    django.contrib.postgres.fields.ArrayField(
                        base_field=models.UUIDField(), size=20
                    ),
                ),
                ("token_hash", models.CharField(max_length=64, null=True)),
                ("expires_at", models.DateTimeField()),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("PENDING", "Pending"),
                            ("ACCEPTED", "Accepted"),
                            ("REVOKED", "Revoked"),
                            ("EXPIRED", "Expired"),
                        ],
                        default="PENDING",
                        max_length=16,
                    ),
                ),
                ("sent_at", models.DateTimeField(null=True)),
                ("accepted_at", models.DateTimeField(null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "invited_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "db_table": "user_invitations",
                "constraints": [
                    models.UniqueConstraint(
                        condition=models.Q(("status", "PENDING")),
                        fields=("organization_id", "email"),
                        name="user_invitations_pending_uq",
                    ),
                    models.UniqueConstraint(
                        fields=("organization_id", "id"), name="user_invitations_org_id_uq"
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            ("status__in", ["PENDING", "ACCEPTED", "REVOKED", "EXPIRED"])
                        ),
                        name="user_invitations_status_ck",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            (
                                "email__regex",
                                "^[\\x21-\\x3f\\x5b-\\x7e]+@[\\x21-\\x3f\\x5b-\\x7e]+$",
                            )
                        ),
                        name="user_invitations_email_ck",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("role_ids__len__gte", 1), ("role_ids__len__lte", 20)),
                        name="user_invitations_roles_ck",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            ("token_hash__isnull", True),
                            ("token_hash__regex", "^[0-9a-f]{64}$"),
                            _connector="OR",
                        ),
                        name="user_invitations_token_hash_ck",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            models.Q(("accepted_at__isnull", False), ("status", "ACCEPTED")),
                            models.Q(
                                models.Q(("status", "ACCEPTED"), _negated=True),
                                ("accepted_at__isnull", True),
                            ),
                            _connector="OR",
                        ),
                        name="user_invitations_accepted_ck",
                    ),
                ],
            },
        ),
        migrations.RunSQL(  # FK de tenant, como en el resto de tablas (OBS-F2-02-2)
            f"ALTER TABLE user_invitations ADD CONSTRAINT {FK} "
            "FOREIGN KEY (organization_id) REFERENCES organizations (id)",
            f"ALTER TABLE user_invitations DROP CONSTRAINT IF EXISTS {FK}",
        ),
        EnableRLS("UserInvitation"),
    ]
