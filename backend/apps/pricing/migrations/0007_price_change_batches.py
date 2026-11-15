"""F3-49: `price_change_batches` y `price_change_batch_items` (§E.8), tablas nuevas y vacías.

Las FKs compuestas a tablas que ya existen (variantes, listas, precios, membresías) toman un
bloqueo `SHARE ROW EXCLUSIVE` sobre ellas: con `lock_timeout` de 5 s la migración falla en lugar
de esperar sin fin detrás de una transacción larga (`SET LOCAL`: dura lo que la migración, que es
atómica). No hay filas que validar ni índices sobre tablas existentes.
"""

from typing import Any

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

import core.ids
from core.db.operations import EnableRLS

MEMBERS = "organization_memberships (organization_id, user_id)"


def fk(table: str, name: str, columns: str, target: str) -> migrations.RunSQL:
    return migrations.RunSQL(
        f"ALTER TABLE public.{table} ADD CONSTRAINT {name} FOREIGN KEY ({columns}) "
        f"REFERENCES {target}",
        f"ALTER TABLE public.{table} DROP CONSTRAINT IF EXISTS {name}",
    )


def _privileges(apps: Any, schema_editor: Any) -> None:
    """Un lote no se borra; lo calculado de una línea no cambia: aplicar solo pone `status` y
    `error` (y el lote, su estado)."""
    role = schema_editor.quote_name(settings.DB_APP_ROLE)
    schema_editor.execute(f"REVOKE DELETE, TRUNCATE ON public.price_change_batches FROM {role}")
    items = "public.price_change_batch_items"
    schema_editor.execute(f"REVOKE UPDATE, DELETE, TRUNCATE ON {items} FROM {role}")
    schema_editor.execute(f"GRANT UPDATE (status, error) ON {items} TO {role}")


def _restore(apps: Any, schema_editor: Any) -> None:
    role = schema_editor.quote_name(settings.DB_APP_ROLE)
    items = "public.price_change_batch_items"
    schema_editor.execute(f"REVOKE UPDATE (status, error) ON {items} FROM {role}")
    for table in ("public.price_change_batches", items):
        schema_editor.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {role}")


class Migration(migrations.Migration):
    dependencies = [
        ("catalog", "0005_deleted_by_member_fk"),
        ("pricing", "0006_variant_costs"),
    ]

    operations = [
        migrations.RunSQL("SET LOCAL lock_timeout = '5s'", migrations.RunSQL.noop),
        migrations.CreateModel(
            name="PriceChangeBatch",
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
                (
                    "kind",
                    models.CharField(
                        choices=[
                            ("BULK_ADJUST", "Bulk Adjust"),
                            ("IMPORT", "Import"),
                            ("MANUAL_MULTI", "Manual Multi"),
                        ],
                        max_length=16,
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("DRAFT", "Draft"),
                            ("PREVIEWED", "Previewed"),
                            ("APPLYING", "Applying"),
                            ("APPLIED", "Applied"),
                            ("CANCELLED", "Cancelled"),
                            ("FAILED", "Failed"),
                        ],
                        max_length=10,
                    ),
                ),
                ("params", models.JSONField()),
                ("reason", models.CharField(max_length=500)),
                ("effective_at", models.DateTimeField(blank=True, null=True)),
                ("summary", models.JSONField(default=dict)),
                ("created_by_user_id", models.UUIDField(blank=True, null=True)),
                ("applied_by_user_id", models.UUIDField(blank=True, null=True)),
                ("applied_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("version", models.IntegerField(default=1)),
                (
                    "price_list",
                    models.ForeignKey(
                        db_constraint=False,
                        db_index=False,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="pricing.pricelist",
                    ),
                ),
            ],
            options={
                "db_table": "price_change_batches",
            },
        ),
        migrations.CreateModel(
            name="PriceChangeBatchItem",
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
                (
                    "old_amount",
                    models.DecimalField(blank=True, decimal_places=2, max_digits=14, null=True),
                ),
                (
                    "new_amount",
                    models.DecimalField(blank=True, decimal_places=2, max_digits=14, null=True),
                ),
                (
                    "delta_percent",
                    models.DecimalField(blank=True, decimal_places=2, max_digits=20, null=True),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("PENDING", "Pending"),
                            ("APPLIED", "Applied"),
                            ("SKIPPED", "Skipped"),
                            ("ERROR", "Error"),
                            ("STALE", "Stale"),
                        ],
                        max_length=10,
                    ),
                ),
                ("error", models.CharField(blank=True, max_length=200)),
                (
                    "batch",
                    models.ForeignKey(
                        db_constraint=False,
                        db_index=False,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="pricing.pricechangebatch",
                    ),
                ),
                (
                    "current_price",
                    models.ForeignKey(
                        blank=True,
                        db_constraint=False,
                        db_index=False,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="pricing.productprice",
                    ),
                ),
                (
                    "price_list",
                    models.ForeignKey(
                        db_constraint=False,
                        db_index=False,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="pricing.pricelist",
                    ),
                ),
                (
                    "variant",
                    models.ForeignKey(
                        db_constraint=False,
                        db_index=False,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="catalog.productvariant",
                    ),
                ),
            ],
            options={
                "db_table": "price_change_batch_items",
            },
        ),
        migrations.AddConstraint(
            model_name="pricechangebatch",
            constraint=models.UniqueConstraint(
                fields=("organization_id", "id", "price_list"), name="price_batches_list_uq"
            ),
        ),
        migrations.AddConstraint(
            model_name="pricechangebatch",
            constraint=models.CheckConstraint(
                condition=models.Q(("kind__in", ["BULK_ADJUST", "IMPORT", "MANUAL_MULTI"])),
                name="price_batches_kind_ck",
            ),
        ),
        migrations.AddConstraint(
            model_name="pricechangebatch",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    (
                        "status__in",
                        ["DRAFT", "PREVIEWED", "APPLYING", "APPLIED", "CANCELLED", "FAILED"],
                    )
                ),
                name="price_batches_status_ck",
            ),
        ),
        migrations.AddConstraint(
            model_name="pricechangebatchitem",
            constraint=models.UniqueConstraint(
                fields=("organization_id", "batch", "variant"), name="price_batch_items_pair_uq"
            ),
        ),
        migrations.AddConstraint(
            model_name="pricechangebatchitem",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("status__in", ["PENDING", "APPLIED", "SKIPPED", "ERROR", "STALE"])
                ),
                name="price_batch_items_status_ck",
            ),
        ),
        migrations.AddConstraint(
            model_name="pricechangebatchitem",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("status", "PENDING"), _negated=True),
                    models.Q(
                        ("current_price__isnull", False),
                        ("new_amount__gt", 0),
                        ("old_amount__gt", 0),
                    ),
                    _connector="OR",
                ),
                name="price_batch_items_pending_ck",
            ),
        ),
        fk(
            "price_change_batches",
            "price_batches_organization_fk",
            "organization_id",
            "organizations (id)",
        ),
        fk(
            "price_change_batches",
            "price_batches_price_list_fk",
            "organization_id, price_list_id",
            "price_lists (organization_id, id)",
        ),
        fk(
            "price_change_batches",
            "price_batches_created_by_fk",
            "organization_id, created_by_user_id",
            MEMBERS,
        ),
        fk(
            "price_change_batches",
            "price_batches_applied_by_fk",
            "organization_id, applied_by_user_id",
            MEMBERS,
        ),
        fk(
            "price_change_batch_items",
            "price_batch_items_organization_fk",
            "organization_id",
            "organizations (id)",
        ),
        fk(  # la lista de la línea es la de su lote
            "price_change_batch_items",
            "price_batch_items_batch_fk",
            "organization_id, batch_id, price_list_id",
            "price_change_batches (organization_id, id, price_list_id)",
        ),
        fk(
            "price_change_batch_items",
            "price_batch_items_variant_fk",
            "organization_id, variant_id",
            "product_variants (organization_id, id)",
        ),
        fk(
            "price_change_batch_items",
            "price_batch_items_current_price_fk",
            "organization_id, current_price_id",
            "product_prices (organization_id, id)",
        ),
        EnableRLS("PriceChangeBatch"),
        EnableRLS("PriceChangeBatchItem"),
        migrations.RunPython(_privileges, _restore),
    ]
