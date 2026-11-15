"""F3-31: índice parcial `(organization_id, transfer_group_id)` de las patas de transferencia, para
`GET …/inventory/movements/?transfer_group_id=`. Migración **no atómica**: el ledger crece sin
límite, y `CREATE INDEX CONCURRENTLY` lo recorre sin bloquear lecturas ni escrituras, con
`lock_timeout` de 5 s para no esperar indefinidamente a una transacción larga (si vence, falla y se
repite). Un `CONCURRENTLY` que falla deja el índice `INVALID`: al repetirla, primero se borra ese
índice inválido (`DROP INDEX CONCURRENTLY`) y después se crea; uno válido ya construido se reutiliza
(`IF NOT EXISTS`). Uno con ese nombre y otra definición no se reutiliza: la migración falla y lo
nombra (F3-32b). Hacia atrás, `DROP INDEX CONCURRENTLY IF EXISTS`.

Al aplicarla: `CONCURRENTLY` espera también a toda transacción con una instantánea más antigua en
toda la base de datos, no solo en esta tabla. Una tarea larga (Celery) hace vencer cada intento de
5 s, y el índice `INVALID` que deja se sigue manteniendo en cada escritura hasta que un intento
acaba bien."""

from typing import Any

from django.db import migrations, models

INDEX = "inventory_movements_group_idx"
STATE = (
    "SELECT i.indisvalid, pg_get_indexdef(i.indexrelid) FROM pg_index i JOIN pg_class c"
    " ON c.oid = i.indexrelid JOIN pg_namespace n ON n.oid = c.relnamespace"
    " WHERE n.nspname = 'public' AND c.relname = %s"
)
DEFINITION = (
    f"CREATE INDEX {INDEX} ON public.inventory_movements USING btree"
    " (organization_id, transfer_group_id) WHERE (transfer_group_id IS NOT NULL)"
)
LOCK_TIMEOUT = "5s"
CREATE = (
    f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {INDEX} ON public.inventory_movements"
    " (organization_id, transfer_group_id) WHERE transfer_group_id IS NOT NULL"
)
DROP = f"DROP INDEX CONCURRENTLY IF EXISTS public.{INDEX}"


def _run(schema_editor: Any, *steps: str) -> None:
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(f"SET lock_timeout = '{LOCK_TIMEOUT}'")
        try:
            for step in steps:
                cursor.execute(step)
        finally:
            cursor.execute("RESET lock_timeout")


def _state(schema_editor: Any) -> tuple[bool, str] | None:
    """`(válido, definición)` del índice con ese nombre, o `None` si no hay ninguno."""
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(STATE, [INDEX])
        row = cursor.fetchone()
    return None if row is None else (row[0], row[1])


def create(apps: Any, schema_editor: Any) -> None:
    found = _state(schema_editor)
    _run(schema_editor, *((DROP,) if found and not found[0] else ()), CREATE)
    if _state(schema_editor) != (True, DEFINITION):  # `IF NOT EXISTS` solo mira el nombre
        raise RuntimeError(f"0006: {INDEX} ya existe con otra definición; bórralo y repítela")


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
