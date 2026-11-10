"""F7-04b: un ítem frenado por el tope de contactos nuevos (`RATE_LIMITED`) no es un fallo: espera
al siguiente minuto sin gastar un intento (con la espera creciente se iría a `DEAD` bajo un pico
sostenido) y no deja fila en `webhook_failures`. Reemplaza `webhook_ingress_mark` de `0009`; volver
atrás la deja como estaba."""

from importlib import import_module
from typing import Any

from django.db import migrations

from core.db.operations import SecurityDefinerFunction

OLD: SecurityDefinerFunction = next(
    op
    for op in import_module(
        "apps.conversations.migrations.0009_webhook_processing"
    ).Migration.operations
    if getattr(op, "name", None) == "webhook_ingress_mark"
)
STATUS = (
    "SET status = CASE WHEN w.attempts >= 8 THEN 'DEAD' ELSE 'FAILED' END, claimed_until = NULL,"
)
WAIT = "next_attempt_at = now() + make_interval("
INSERT = "SELECT w.id, w.attempts, coalesce(p_reason, 'ERROR')"
ARRAY = "(ARRAY[30, 120, 600, 3600, 21600])[least(greatest(w.attempts, 1), 5)])"
LIMITED = "p_reason IS NOT DISTINCT FROM 'RATE_LIMITED'"


def deferral(body: str) -> str:
    for old in (STATUS, WAIT, INSERT, "    IF v_status IS NOT NULL THEN", ARRAY):
        if body.count(old) != 1:
            raise ValueError(old)
    body = body.replace(
        STATUS,
        f"SET status = CASE WHEN w.attempts >= 8 AND NOT ({LIMITED})\n"
        "          THEN 'DEAD' ELSE 'FAILED' END,\n"
        f"        attempts = CASE WHEN {LIMITED} THEN w.attempts - 1 ELSE w.attempts END,\n"
        "        claimed_until = NULL,",
    )
    body = body.replace(
        WAIT,
        f"next_attempt_at = CASE WHEN {LIMITED}\n"
        "          THEN date_trunc('minute', now()) + interval '1 minute'\n"
        "          ELSE now() + make_interval(",
    )
    body = body.replace(
        "(ARRAY[30, 120, 600, 3600, 21600])[least(greatest(w.attempts, 1), 5)])",
        "(ARRAY[30, 120, 600, 3600, 21600])[least(greatest(w.attempts, 1), 5)]) END",
    )
    return body.replace(
        "    IF v_status IS NOT NULL THEN", f"    IF v_status IS NOT NULL AND NOT ({LIMITED}) THEN"
    )


class Replace(SecurityDefinerFunction):
    """Cambia el cuerpo de una función de `0009`, con el mismo propietario y los mismos permisos."""

    def database_forwards(self, app_label: str, schema_editor: Any, *a: Any) -> None:
        OLD.database_backwards(app_label, schema_editor, *a)
        super().database_forwards(app_label, schema_editor, *a)

    def database_backwards(self, app_label: str, schema_editor: Any, *a: Any) -> None:
        super().database_backwards(app_label, schema_editor, *a)
        OLD.database_forwards(app_label, schema_editor, *a)


class Migration(migrations.Migration):
    dependencies = [
        ("conversations", "0010_inbound_whatsapp_messages"),
    ]

    operations = [
        Replace(
            OLD.name,
            OLD.arguments,
            OLD.argtypes,
            OLD.returns,
            deferral(OLD.body),
            language=OLD.language,
            search_path=OLD.search_path,
        )
    ]
