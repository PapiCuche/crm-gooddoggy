"""F7-09a1b: `message_attachments` (ADR-029 §7), el medio de un mensaje entrante y su descarga.
FORCE RLS con una política, FKs compuestas a `messages` y a `files` (las restricciones destino las
crean, sin bloquear la tabla, `0020` y `files.0002`), un adjunto por mensaje y privilegios por
columna: `crm_app` lee, inserta y cambia solo el estado de la descarga; nunca borra.

El disparador `message_attachments_advance` hace cumplir la máquina de estados para **todos** los
roles (no hay salida temprana por `current_user`: el migrador tampoco reescribe un adjunto; una
corrección de datos exigiría una migración nueva que lo reemplace a la vista):
- `DONE`, `FAILED` y `EXPIRED` son terminales: ningún UPDATE los cambia.
- La identidad (organización, id, mensaje, clase, id del medio, alta) no se reescribe.
- `PENDING` pasa a `DOWNLOADING` o `FAILED`; `DOWNLOADING` a `PENDING` (reintento), `DOWNLOADING`
  (otro intento reclama un plazo vencido), `DONE`, `FAILED` o `EXPIRED`. Los intentos no bajan.
`DONE` solo con `file_id` y `file_id` solo con `DONE` (CHECK)."""

from typing import Any

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

import core.ids
from core.db.operations import EnableRLS

ADVANCE = """
CREATE FUNCTION public.message_attachments_advance() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public, pg_temp AS $fn$
BEGIN
    IF OLD.download_status IN ('DONE', 'FAILED', 'EXPIRED') THEN
        RAISE EXCEPTION 'message_attachments_advance: un adjunto cerrado no cambia'
            USING ERRCODE = 'check_violation';
    END IF;
    IF (NEW.organization_id, NEW.id, NEW.message_id, NEW.kind, NEW.external_media_id,
        NEW.created_at) IS DISTINCT FROM (OLD.organization_id, OLD.id, OLD.message_id, OLD.kind,
        OLD.external_media_id, OLD.created_at)
       OR NEW.attempts < OLD.attempts
       OR NOT (
           (OLD.download_status = 'PENDING' AND NEW.download_status IN ('PENDING', 'DOWNLOADING',
            'FAILED'))
           OR (OLD.download_status = 'DOWNLOADING' AND NEW.download_status IN ('PENDING',
            'DOWNLOADING', 'DONE', 'FAILED', 'EXPIRED'))
       ) THEN
        RAISE EXCEPTION 'message_attachments_advance: transición no permitida'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$fn$;
CREATE TRIGGER message_attachments_advance BEFORE UPDATE ON public.message_attachments
    FOR EACH ROW EXECUTE FUNCTION public.message_attachments_advance();
"""
DROP = (
    "DROP TRIGGER IF EXISTS message_attachments_advance ON public.message_attachments; "
    "DROP FUNCTION IF EXISTS public.message_attachments_advance()"
)
MUTABLE = (
    "download_status, failure_code, attempts, attempt_id, lease_until, not_before, file_id, "
    "updated_at"
)


def fk(name: str, columns: str, target: str) -> migrations.RunSQL:
    return migrations.RunSQL(
        f"ALTER TABLE public.message_attachments ADD CONSTRAINT {name} FOREIGN KEY ({columns}) "
        f"REFERENCES {target}",
        f"ALTER TABLE public.message_attachments DROP CONSTRAINT IF EXISTS {name}",
    )


def _privileges(apps: Any, schema_editor: Any) -> None:
    role = schema_editor.quote_name(settings.DB_APP_ROLE)
    table = "public.message_attachments"
    schema_editor.execute(f"REVOKE DELETE, TRUNCATE, UPDATE ON {table} FROM {role}")
    schema_editor.execute(f"GRANT UPDATE ({MUTABLE}) ON {table} TO {role}")


def _restore(apps: Any, schema_editor: Any) -> None:
    role = schema_editor.quote_name(settings.DB_APP_ROLE)
    schema_editor.execute(f"GRANT UPDATE, DELETE ON public.message_attachments TO {role}")


class Migration(migrations.Migration):
    dependencies = [
        ("conversations", "0020_messages_org_id_uq"),
        ("files", "0002_files_org_id_uq"),
        ("organizations", "0010_invitations_are_not_deleted"),
    ]

    operations = [
        migrations.CreateModel(
            name="MessageAttachment",
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
                ("kind", models.CharField(max_length=8)),
                ("external_media_id", models.CharField(max_length=30)),
                ("download_status", models.CharField(default="PENDING", max_length=12)),
                ("failure_code", models.CharField(blank=True, max_length=24, null=True)),
                ("attempts", models.PositiveSmallIntegerField(default=0)),
                ("attempt_id", models.UUIDField(blank=True, null=True)),
                ("lease_until", models.DateTimeField(blank=True, null=True)),
                ("not_before", models.DateTimeField(blank=True, null=True)),
                ("file_id", models.UUIDField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "message",
                    models.ForeignKey(
                        db_constraint=False,
                        db_index=False,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="conversations.message",
                    ),
                ),
            ],
            options={
                "db_table": "message_attachments",
                "indexes": [
                    models.Index(
                        condition=models.Q(("download_status", "DOWNLOADING")),
                        fields=["organization_id", "lease_until"],
                        name="message_attachments_busy_idx",
                    )
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("organization_id", "message"), name="message_attachments_message_uq"
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            ("attempts__lte", 5),
                            (
                                "download_status__in",
                                ("PENDING", "DOWNLOADING", "DONE", "FAILED", "EXPIRED"),
                            ),
                            ("external_media_id__regex", "^[0-9]{5,30}$"),
                            ("kind__in", ("IMAGE", "AUDIO", "VIDEO", "STICKER")),
                        ),
                        name="message_attachments_values_ck",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            models.Q(
                                ("download_status", "FAILED"),
                                (
                                    "failure_code__in",
                                    (
                                        "TOKEN_INVALID",
                                        "CHANNEL_UNAVAILABLE",
                                        "UNAVAILABLE",
                                        "TOO_LARGE",
                                        "TYPE_NOT_ALLOWED",
                                        "TYPE_MISMATCH",
                                    ),
                                ),
                                ("failure_code__isnull", False),
                            ),
                            models.Q(
                                models.Q(("download_status", "FAILED"), _negated=True),
                                ("failure_code__isnull", True),
                            ),
                            _connector="OR",
                        ),
                        name="message_attachments_failure_ck",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            models.Q(("download_status", "DONE"), ("file_id__isnull", False)),
                            models.Q(
                                models.Q(("download_status", "DONE"), _negated=True),
                                ("file_id__isnull", True),
                            ),
                            _connector="OR",
                        ),
                        name="message_attachments_file_ck",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            models.Q(("download_status", "DOWNLOADING"), _negated=True),
                            models.Q(("attempt_id__isnull", False), ("lease_until__isnull", False)),
                            _connector="OR",
                        ),
                        name="message_attachments_lease_ck",
                    ),
                ],
            },
        ),
        fk("message_attachments_organization_fk", "organization_id", "organizations (id)"),
        fk(
            "message_attachments_message_fk",
            "organization_id, message_id",
            "public.messages (organization_id, id)",
        ),
        fk(
            "message_attachments_file_fk",
            "organization_id, file_id",
            "public.files (organization_id, id)",
        ),
        EnableRLS("MessageAttachment"),
        migrations.RunPython(_privileges, _restore),
        migrations.RunSQL(ADVANCE, DROP),
    ]
