"""F7-11a: los índices que respaldan `meta_ingest_counts` (cada minuto): los fallos de la ingesta
por fecha y los salientes `FAILED` por fecha (solo esas filas). `CONCURRENTLY`, no atómica, con
`lock_timeout` de 5 s (el de la sesión del migrador, que se restablece al acabar). Si un índice
queda `INVALID` (un `CREATE INDEX CONCURRENTLY` que falló a medias o el de un intento anterior),
se borra y la migración falla: repetirla lo crea de nuevo. Hacia atrás: los quita."""

from typing import Any

from django.db import migrations

INDEXES = {
    "webhook_failures_created_idx": "public.webhook_failures (created_at)",
    "messages_failed_idx": "public.messages (failed_at) WHERE delivery_status = 'FAILED'",
}


def _create(apps: Any, schema_editor: Any) -> None:
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("SET lock_timeout = '5s'")
        try:
            for name, target in INDEXES.items():
                try:
                    cursor.execute(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} ON {target}")
                finally:
                    cursor.execute(
                        "SELECT i.indisvalid FROM pg_index i JOIN pg_class c "
                        "ON c.oid = i.indexrelid AND c.relname = %s", [name],
                    )  # fmt: skip
                    found = cursor.fetchone()
                    if found is not None and not found[0]:
                        cursor.execute(f"DROP INDEX CONCURRENTLY IF EXISTS public.{name}")
                        raise RuntimeError(f"{name} quedó INVALID: se borró; repita la migración")
        finally:
            cursor.execute("RESET lock_timeout")


def _drop(apps: Any, schema_editor: Any) -> None:
    for name in INDEXES:
        schema_editor.execute(f"DROP INDEX CONCURRENTLY IF EXISTS public.{name}")


class Migration(migrations.Migration):
    atomic = False
    dependencies = [
        ("conversations", "0031_meta_ingest_counts"),
    ]

    operations = [migrations.RunPython(_create, _drop)]
