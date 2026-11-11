"""F7-06a: un estado que llega tarde reconcilia un `FAILED` con `PROVIDER_UNKNOWN`. El barredor de
`SENDING` cierra con ese motivo lo que no se supo si salió (ADR-029 §6); si Meta confirma después
que sí salió, el mensaje pasa a `SENT`, `DELIVERED` o `READ`. Es la **única** salida de `FAILED`: el
disparador `messages_delivery_advance` solo la admite desde `PROVIDER_UNKNOWN` (cualquier otro
`FAILED` sigue terminal) y hacia un estado de entrega (nunca hacia atrás ni a `QUEUED`)."""

from importlib import import_module

from django.db import migrations

BEFORE = import_module("apps.conversations.migrations.0013_message_sending").NEW
GUARD = "OLD.delivery_status IS NULL OR OLD.delivery_status = 'FAILED' OR ("
LATE = (
    "OLD.delivery_status IS NULL OR (OLD.delivery_status = 'FAILED' AND NOT ("
    "coalesce(OLD.provider_error_code, '') = 'PROVIDER_UNKNOWN' "
    "AND NEW.delivery_status IN ('SENT', 'DELIVERED', 'READ'))) OR ("
)
AFTER = BEFORE.replace(GUARD, LATE)
if AFTER == BEFORE:
    raise RuntimeError("el disparador de 0013 cambió: revisar esta migración")


class Migration(migrations.Migration):
    dependencies = [
        ("conversations", "0015_delivery_error_code"),
    ]

    operations = [migrations.RunSQL(AFTER, BEFORE)]
