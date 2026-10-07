"""F2-79: `user_invitations` (ADR-020; modelo de datos §E.2). Tenant-owned, con RLS forzado y FK a
su organización. Una sola invitación pendiente por correo; el correo, en minúsculas y en ASCII;
al menos un rol; del enlace solo cabe un SHA-256.
"""

import django.contrib.postgres.fields
import django.db.models.deletion
import django.db.models.lookups
from django.conf import settings
from django.db import migrations, models

import core.ids
from core.db.operations import EnableRLS

FK = "user_invitations_organization_fk"
INVITER = "user_invitations_invited_by_org_fk"


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
                ("send_count", models.SmallIntegerField(db_default=0, default=0)),
                ("accepted_at", models.DateTimeField(null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "invited_by",
                    models.ForeignKey(
                        db_column="invited_by_user_id",
                        db_constraint=False,
                        db_index=False,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "db_table": "user_invitations",
                "indexes": [
                    models.Index(
                        fields=["organization_id", "invited_by"],
                        name="user_invitations_inviter_idx",
                    )
                ],
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
                        condition=models.Q(
                            ("role_ids__len__gte", 1),
                            ("role_ids__len__lte", 20),
                            django.db.models.lookups.Exact(
                                models.Func(
                                    models.F("role_ids"),
                                    function="array_ndims",
                                    output_field=models.IntegerField(),
                                ),
                                1,
                            ),
                            django.db.models.lookups.IsNull(
                                models.Func(
                                    models.F("role_ids"),
                                    models.Value(None),
                                    function="array_position",
                                    output_field=models.IntegerField(),
                                ),
                                True,
                            ),
                        ),
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
                    models.UniqueConstraint(
                        condition=models.Q(("token_hash__isnull", False)),
                        fields=("token_hash",),
                        name="user_invitations_token_hash_uq",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("send_count__gte", 0), ("send_count__lte", 5)),
                        name="user_invitations_send_count_ck",
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
        migrations.RunSQL(  # ADR-001 §3: quien invita es miembro de esta organización
            f"ALTER TABLE user_invitations ADD CONSTRAINT {INVITER} "
            "FOREIGN KEY (organization_id, invited_by_user_id) "
            "REFERENCES organization_memberships (organization_id, user_id)",
            f"ALTER TABLE user_invitations DROP CONSTRAINT IF EXISTS {INVITER}",
        ),
    ]
