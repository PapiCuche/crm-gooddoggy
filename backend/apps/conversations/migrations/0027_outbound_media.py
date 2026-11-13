"""F7-09b: medios salientes (imagen y documento) con su archivo (ADR-029 §7, ADR-008).

`messages.file_id` (nulo) y la FK compuesta `(organization_id, file_id)` a `files`: el archivo es
de la misma organización. `crm_app` no recibe `UPDATE` de la columna (la `0002` solo le da las
columnas de entrega): el archivo de un mensaje se pone al crearlo y no cambia. `CHECK`:
`messages_file_ck` (un archivo si y solo si es un medio saliente, `IMAGE` o `DOCUMENT`; un
documento nunca es entrante), `messages_type_ck` admite `DOCUMENT`, `messages_unsupported_ck` deja
`IMAGE` salir y `messages_error_code_ck` admite `MEDIA_NOT_SCANNED`.

`messages` es la tabla más grande: como la `0024`, cada `CHECK` y la FK se añaden `NOT VALID` con
`lock_timeout` de 5 s (un `ACCESS EXCLUSIVE` breve, solo de catálogo) y la `0028` los valida sin
impedir leer ni escribir. Ninguna fila anterior los incumple: `file_id` nace nulo, `DOCUMENT` no
existía y un `IMAGE` saliente lo prohibía el `CHECK` anterior; la validación lo comprueba igual.
Los `CHECK` se escriben desde la definición del modelo (el estado y la base no divergen). Hacia
atrás vuelven los de la `0024` y la `0019`, que fallan mientras haya un medio saliente o un
`MEDIA_NOT_SCANNED` (no se pierde nada)."""

from typing import Any

from django.db import migrations, models

TABLE = "public.messages"
CHANGED = ("messages_type_ck", "messages_unsupported_ck", "messages_error_code_ck")
NEW = "messages_file_ck"
FK = "messages_file_fk"
CODES = (
    "CONVERSATION_CLOSED", "CHANNEL_UNAVAILABLE", "CONTACT_BLOCKED", "WINDOW_CLOSED",
    "CONSENT_REVOKED", "ADAPTER_REJECTED", "RETRIES_EXHAUSTED", "TOKEN_INVALID",
    "RECIPIENT_UNREACHABLE", "PROVIDER_REJECTED", "PROVIDER_UNKNOWN", "RATE_LIMITED",
    "QUALITY_BLOCKED", "TEMPLATE_UNAVAILABLE", "PROVIDER_ERROR",
)  # fmt: skip
TYPES = ("TEXT", "NOTE", "UNSUPPORTED", "IMAGE", "AUDIO", "VIDEO")
OUT_MEDIA = ("IMAGE", "DOCUMENT")


def type_ck(types: tuple[str, ...]) -> models.CheckConstraint:
    return models.CheckConstraint(condition=models.Q(message_type__in=types), name=CHANGED[0])


def unsupported_ck(inbound_only: tuple[str, ...]) -> models.CheckConstraint:
    return models.CheckConstraint(
        condition=~models.Q(message_type__in=inbound_only) | models.Q(direction="INBOUND"),
        name=CHANGED[1],
    )


def error_code_ck(codes: tuple[str, ...]) -> models.CheckConstraint:
    return models.CheckConstraint(
        condition=models.Q(provider_error_code__isnull=True)
        | (models.Q(delivery_status="FAILED") & models.Q(provider_error_code__in=codes)),
        name=CHANGED[2],
    )


FILE_CK = models.CheckConstraint(
    condition=models.Q(file_id__isnull=False, direction="OUTBOUND", message_type__in=OUT_MEDIA)
    | (
        models.Q(file_id__isnull=True)
        & ~models.Q(message_type="DOCUMENT")
        & ~models.Q(direction="OUTBOUND", message_type="IMAGE")
    ),
    name=NEW,
)
AFTER = (
    type_ck((*TYPES, "DOCUMENT")),
    unsupported_ck(("UNSUPPORTED", "AUDIO", "VIDEO")),
    error_code_ck((*CODES, "MEDIA_NOT_SCANNED")),
)
BEFORE = (
    type_ck(TYPES),
    unsupported_ck(("UNSUPPORTED", "AUDIO", "VIDEO", "IMAGE")),
    error_code_ck(CODES),
)


def _swap(apps: Any, editor: Any, old: tuple[Any, ...], new: tuple[Any, ...], tail: str) -> None:
    model = apps.get_model("conversations", "Message")
    editor.execute("SET LOCAL lock_timeout = '5s'")
    for constraint in old:
        editor.execute(f"ALTER TABLE {TABLE} DROP CONSTRAINT {constraint.name}")
    for constraint in new:
        editor.execute(f"ALTER TABLE {TABLE} ADD {constraint.constraint_sql(model, editor)}{tail}")


def forward(apps: Any, editor: Any) -> None:
    _swap(apps, editor, BEFORE, (), "")
    editor.execute(f"ALTER TABLE {TABLE} ADD COLUMN file_id uuid NULL")
    editor.execute(
        f"ALTER TABLE {TABLE} ADD CONSTRAINT {FK} FOREIGN KEY (organization_id, file_id) "
        "REFERENCES public.files (organization_id, id) NOT VALID"
    )
    _swap(apps, editor, (), (*AFTER, FILE_CK), " NOT VALID")


def backward(apps: Any, editor: Any) -> None:
    _swap(apps, editor, (*AFTER, FILE_CK), (), "")
    editor.execute(f"ALTER TABLE {TABLE} DROP COLUMN file_id")  # con ella, la FK
    _swap(apps, editor, (), BEFORE, "")


class Migration(migrations.Migration):
    dependencies = [
        ("conversations", "0026_message_attachments_clock_forward"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                *(migrations.RemoveConstraint(model_name="message", name=n) for n in CHANGED),
                migrations.AddField(
                    model_name="message",
                    name="file_id",
                    field=models.UUIDField(blank=True, null=True),
                ),
                migrations.AlterField(
                    model_name="message",
                    name="message_type",
                    field=models.CharField(
                        choices=[(t, t.title()) for t in (*TYPES, "DOCUMENT")],
                        default="TEXT",
                        max_length=16,
                    ),
                ),
                *(migrations.AddConstraint(model_name="message", constraint=c) for c in AFTER),
                migrations.AddConstraint(model_name="message", constraint=FILE_CK),
            ],
        ),
        migrations.RunPython(forward, backward),
    ]
