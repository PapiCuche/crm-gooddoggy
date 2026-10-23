import django.db.models.functions.text
from django.db import migrations, models

import core.ids
from core.db.operations import EnableRLS


class Migration(migrations.Migration):
    dependencies = [
        ("contacts", "0004_notes"),
        ("organizations", "0010_invitations_are_not_deleted"),
    ]

    operations = [
        migrations.CreateModel(
            name="Tag",
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
                ("name", models.CharField(max_length=50)),
                ("color", models.CharField(max_length=7)),
                ("applies_to", models.CharField(choices=[("CONTACT", "Contact")], max_length=16)),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "db_table": "tags",
                "constraints": [
                    models.UniqueConstraint(
                        models.F("organization_id"),
                        models.F("applies_to"),
                        django.db.models.functions.text.Lower("name"),
                        name="tags_org_target_name_uq",
                    ),
                    models.UniqueConstraint(
                        fields=("organization_id", "id"), name="tags_org_id_uq"
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("color__regex", "^#[0-9a-f]{6}$")), name="tags_color_ck"
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("applies_to__in", ["CONTACT"])),
                        name="tags_applies_to_ck",
                    ),
                ],
            },
        ),
        migrations.RunSQL(  # FK de tenant, como en el resto de tablas (OBS-F2-02-2)
            "ALTER TABLE tags ADD CONSTRAINT tags_organization_fk FOREIGN KEY (organization_id) "
            "REFERENCES organizations (id)",
            "ALTER TABLE tags DROP CONSTRAINT IF EXISTS tags_organization_fk",
        ),
        EnableRLS("Tag"),
    ]
