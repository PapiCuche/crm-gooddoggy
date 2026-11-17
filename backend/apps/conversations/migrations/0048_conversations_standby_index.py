"""F7b-06b: índice parcial de las conversaciones no cerradas en `standby`, para el `Exists` de
`has_standby_conversations` en la lista de cuentas (pocas filas: solo los hilos en `standby`). Mismo
patrón que la `0037`: **no atómica**, `CREATE INDEX CONCURRENTLY` con `lock_timeout` de 5 s; un
índice `INVALID` de un intento anterior se borra antes de crearlo, uno válido se reutiliza y uno con
ese nombre y otra definición hace fallar la migración. Hacia atrás, `DROP INDEX CONCURRENTLY`."""

from typing import Any

from django.db import migrations, models

INDEX = "conversations_standby_idx"
STATE = (
    "SELECT i.indisvalid, pg_get_indexdef(i.indexrelid) FROM pg_index i JOIN pg_class c"
    " ON c.oid = i.indexrelid JOIN pg_namespace n ON n.oid = c.relnamespace"
    " WHERE n.nspname = 'public' AND c.relname = %s"
)
CREATE = (
    f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {INDEX} ON public.conversations"
    " (organization_id, channel_account_id) WHERE standby_since IS NOT NULL AND status <> 'CLOSED'"
)
DEFINITION = (
    f"CREATE INDEX {INDEX} ON public.conversations USING btree"
    " (organization_id, channel_account_id) WHERE ((standby_since IS NOT NULL) AND"
    " ((status)::text <> 'CLOSED'::text))"
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
        raise RuntimeError(f"0048: {INDEX} ya existe con otra definición; bórralo y repítela")


def drop(apps: Any, schema_editor: Any) -> None:
    _run(schema_editor, DROP)


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ("conversations", "0047_conversation_standby_since"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AddIndex(
                    model_name="conversation",
                    index=models.Index(
                        fields=["organization_id", "channel_account"],
                        condition=models.Q(standby_since__isnull=False)
                        & ~models.Q(status="CLOSED"),
                        name=INDEX,
                    ),
                ),
            ],
            database_operations=[migrations.RunPython(create, drop)],
        ),
    ]
