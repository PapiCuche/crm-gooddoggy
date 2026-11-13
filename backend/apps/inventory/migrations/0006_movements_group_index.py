"""F3-31: índice parcial `(organization_id, transfer_group_id)` de las patas de transferencia, para
`GET …/inventory/movements/?transfer_group_id=`. Migración **no atómica**: el ledger crece sin
límite, y `CREATE INDEX CONCURRENTLY` lo recorre sin bloquear lecturas ni escrituras, con
`lock_timeout` de 5 s para no esperar indefinidamente a una transacción larga (si vence, falla y se
repite). Un `CONCURRENTLY` que falla deja el índice `INVALID`: al repetirla, primero se borra ese
índice inválido (`DROP INDEX CONCURRENTLY`) y después se crea; uno válido ya construido se reutiliza
(`IF NOT EXISTS`). Hacia atrás, `DROP INDEX CONCURRENTLY IF EXISTS`."""

from typing import Any

from django.db import migrations, models

INDEX = "inventory_movements_group_idx"
INVALID = (
    "SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid JOIN pg_namespace n"
    " ON n.oid = c.relnamespace WHERE n.nspname = 'public' AND c.relname = %s AND NOT i.indisvalid"
)
CREATE = (
    f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {INDEX} ON public.inventory_movements"
    " (organization_id, transfer_group_id) WHERE transfer_group_id IS NOT NULL"
)
DROP = f"DROP INDEX CONCURRENTLY IF EXISTS public.{INDEX}"


def _run(schema_editor: Any, *steps: str) -> None:
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("SET lock_timeout = '5s'")
        try:
            for step in steps:
                cursor.execute(step)
        finally:
            cursor.execute("RESET lock_timeout")


def create(apps: Any, schema_editor: Any) -> None:
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(INVALID, [INDEX])
        invalid = cursor.fetchone() is not None
    _run(schema_editor, *((DROP,) if invalid else ()), CREATE)


def drop(apps: Any, schema_editor: Any) -> None:
    _run(schema_editor, DROP)


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ("inventory", "0005_validate_transfer_checks"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AddIndex(
                    model_name="inventorymovement",
                    index=models.Index(
                        condition=models.Q(("transfer_group_id__isnull", False)),
                        fields=["organization_id", "transfer_group_id"],
                        name=INDEX,
                    ),
                ),
            ],
            database_operations=[migrations.RunPython(create, drop)],
        ),
    ]
