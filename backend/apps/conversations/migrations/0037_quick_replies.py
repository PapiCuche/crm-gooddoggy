"""F6-08a1: `quick_reply_categories` y `quick_replies` (02 §E.6), tablas nuevas y vacías.

Tenant-owned con RLS y FORCE (`EnableRLS`). FKs compuestas: la organización, la categoría de la
misma organización y quien crea, cambia o borra, miembro de ella (ADR-001 §3). Las FKs a tablas
que ya existen (organizaciones, membresías) toman `SHARE ROW EXCLUSIVE` sobre ellas: con
`lock_timeout` de 5 s la migración falla en lugar de esperar sin fin (`SET LOCAL`: dura lo que la
migración, que es atómica). No hay filas que validar. Nada se borra: `crm_app` no tiene DELETE.
"""

from typing import Any

import django.db.models.deletion
import django.db.models.functions.text
import django.db.models.lookups
from django.conf import settings
from django.db import migrations, models

import core.ids
from core.db.operations import EnableRLS

TABLES = ("quick_reply_categories", "quick_replies")
MEMBERS = "organization_memberships (organization_id, user_id)"


def fk(table: str, name: str, columns: str, target: str) -> migrations.RunSQL:
    return migrations.RunSQL(
        f"ALTER TABLE public.{table} ADD CONSTRAINT {name} FOREIGN KEY ({columns}) "
        f"REFERENCES {target}",
        f"ALTER TABLE public.{table} DROP CONSTRAINT IF EXISTS {name}",
    )


def _privileges(apps: Any, schema_editor: Any) -> None:
    role = schema_editor.quote_name(settings.DB_APP_ROLE)
    for table in TABLES:
        schema_editor.execute(f"REVOKE DELETE, TRUNCATE ON public.{table} FROM {role}")


def _restore(apps: Any, schema_editor: Any) -> None:
    role = schema_editor.quote_name(settings.DB_APP_ROLE)
    for table in TABLES:
        schema_editor.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON public.{table} TO {role}")


class Migration(migrations.Migration):
    dependencies = [
        ("conversations", "0036_validate_file_scan_error"),
        ("organizations", "0010_invitations_are_not_deleted"),
    ]

    operations = [
        migrations.RunSQL("SET LOCAL lock_timeout = '5s'", migrations.RunSQL.noop),
        migrations.CreateModel(
            name="QuickReplyCategory",
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
                ("name", models.CharField(max_length=100)),
                ("sort_order", models.IntegerField(default=0)),
                ("is_active", models.BooleanField(default=True)),
                ("created_by_user_id", models.UUIDField(blank=True, null=True)),
                ("updated_by_user_id", models.UUIDField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("deleted_at", models.DateTimeField(blank=True, null=True)),
                ("deleted_by_user_id", models.UUIDField(blank=True, null=True)),
                ("version", models.PositiveIntegerField(default=1)),
            ],
            options={
                "db_table": "quick_reply_categories",
                "constraints": [
                    models.UniqueConstraint(
                        fields=("organization_id", "id"), name="qr_categories_org_id_uq"
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            ("deleted_at__isnull", True), ("is_active", False), _connector="OR"
                        ),
                        name="qr_categories_deleted_ck",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("version__gte", 1)), name="qr_categories_version_ck"
                    ),
                    models.UniqueConstraint(
                        models.F("organization_id"),
                        django.db.models.functions.text.Lower("name"),
                        condition=models.Q(("deleted_at__isnull", True)),
                        name="qr_categories_name_uq",
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="QuickReply",
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
                ("shortcut", models.CharField(max_length=32)),
                ("title", models.CharField(max_length=100)),
                ("body", models.TextField()),
                ("is_active", models.BooleanField(default=True)),
                ("usage_count", models.PositiveIntegerField(default=0)),
                ("created_by_user_id", models.UUIDField(blank=True, null=True)),
                ("updated_by_user_id", models.UUIDField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("deleted_at", models.DateTimeField(blank=True, null=True)),
                ("deleted_by_user_id", models.UUIDField(blank=True, null=True)),
                ("version", models.PositiveIntegerField(default=1)),
                (
                    "category",
                    models.ForeignKey(
                        db_constraint=False,
                        db_index=False,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="conversations.quickreplycategory",
                    ),
                ),
            ],
            options={
                "db_table": "quick_replies",
                "indexes": [
                    models.Index(
                        fields=["organization_id", "category"], name="quick_replies_category_idx"
                    )
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("organization_id", "id"), name="quick_replies_org_id_uq"
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            ("deleted_at__isnull", True), ("is_active", False), _connector="OR"
                        ),
                        name="quick_replies_deleted_ck",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("version__gte", 1)), name="quick_replies_version_ck"
                    ),
                    models.UniqueConstraint(
                        condition=models.Q(("deleted_at__isnull", True), ("is_active", True)),
                        fields=("organization_id", "shortcut"),
                        name="quick_replies_shortcut_uq",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("shortcut__regex", "^[a-z0-9_-]{1,32}$")),
                        name="quick_replies_shortcut_ck",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            ("body__regex", "\\S"),
                            django.db.models.lookups.LessThanOrEqual(
                                django.db.models.functions.text.Length("body"), 4096
                            ),
                        ),
                        name="quick_replies_body_ck",
                    ),
                ],
            },
        ),
        *(
            fk(table, f"{table}_organization_fk", "organization_id", "organizations (id)")
            for table in TABLES
        ),
        *(
            fk(table, f"{table}_{who}_fk", f"organization_id, {who}_user_id", MEMBERS)
            for table in TABLES
            for who in ("created_by", "updated_by", "deleted_by")
        ),
        fk(
            "quick_replies",
            "quick_replies_category_fk",
            "organization_id, category_id",
            "quick_reply_categories (organization_id, id)",
        ),
        EnableRLS("QuickReplyCategory"),
        EnableRLS("QuickReply"),
        migrations.RunPython(_privileges, _restore),
    ]
