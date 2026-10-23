import django.db.models.deletion
from django.db import migrations, models

import core.ids
from core.db.operations import CompositeTenantFK, EnableRLS


def fk(name: str, columns: str, target: str) -> migrations.RunSQL:
    return migrations.RunSQL(
        f"ALTER TABLE contact_tags ADD CONSTRAINT {name} "
        f"FOREIGN KEY ({columns}) REFERENCES {target}",
        f"ALTER TABLE contact_tags DROP CONSTRAINT IF EXISTS {name}",
    )


class Migration(migrations.Migration):
    dependencies = [("contacts", "0005_tags")]

    operations = [
        migrations.CreateModel(
            name="ContactTag",
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
                ("added_by_user_id", models.UUIDField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "contact",
                    models.ForeignKey(
                        db_constraint=False,
                        db_index=False,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="tag_links",
                        to="contacts.contact",
                    ),
                ),
                (
                    "tag",
                    models.ForeignKey(
                        db_constraint=False,
                        db_index=False,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="contact_links",
                        to="contacts.tag",
                    ),
                ),
            ],
            options={
                "db_table": "contact_tags",
                "indexes": [
                    models.Index(
                        fields=["organization_id", "tag", "contact"],
                        name="contact_tags_org_tag_idx",
                    )
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("organization_id", "contact", "tag"),
                        name="contact_tags_org_contact_tag_uq",
                    )
                ],
            },
        ),
        # FK de tenant, como en el resto de tablas (OBS-F2-02-2)
        fk("contact_tags_organization_fk", "organization_id", "organizations (id)"),
        # El contacto y la etiqueta son de la misma organización (ADR-001 §3)
        CompositeTenantFK("ContactTag", "contact", "Contact", "contact_tags_contact_fk"),
        CompositeTenantFK("ContactTag", "tag", "Tag", "contact_tags_tag_fk"),
        # Quien la puso es miembro de esta organización (ADR-001 §3)
        fk(
            "contact_tags_added_by_fk",
            "organization_id, added_by_user_id",
            "organization_memberships (organization_id, user_id)",
        ),
        EnableRLS("ContactTag"),
    ]
