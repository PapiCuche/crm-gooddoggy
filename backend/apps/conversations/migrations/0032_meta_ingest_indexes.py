"""F7-11a: los índices que respaldan `meta_ingest_counts` (cada minuto): los fallos de la ingesta
por fecha y los salientes `FAILED` por fecha (solo esas filas). `CONCURRENTLY`, no atómica: si un
índice falla a medias queda `INVALID` y se borra a mano antes de repetir. Hacia atrás: los quita."""

from django.db import migrations

INDEXES = {
    "webhook_failures_created_idx": "public.webhook_failures (created_at)",
    "messages_failed_idx": "public.messages (failed_at) WHERE delivery_status = 'FAILED'",
}


class Migration(migrations.Migration):
    atomic = False
    dependencies = [
        ("conversations", "0031_meta_ingest_counts"),
    ]

    operations = [
        migrations.RunSQL(
            f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} ON {target}",
            f"DROP INDEX CONCURRENTLY IF EXISTS public.{name}",
        )
        for name, target in INDEXES.items()
    ]
