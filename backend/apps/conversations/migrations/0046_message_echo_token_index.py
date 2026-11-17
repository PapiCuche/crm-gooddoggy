"""F7b-05c: valida el `messages_echo_token_ck` que la `0045` añadió `NOT VALID` (`VALIDATE
CONSTRAINT` toma `SHARE UPDATE EXCLUSIVE`, que no impide leer ni escribir) y crea, `CONCURRENTLY`,
el índice único de la ficha del eco por cuenta (solo las filas con ficha). Mismo patrón que la
`0037`: **no atómica**, `lock_timeout` de 5 s; un índice `INVALID` de un intento anterior (una
construcción cortada o con duplicados) se borra antes de crearlo, uno válido se reutiliza, y si al
acabar no es válido o tiene otra definición la migración falla (repetirla vuelve a intentarlo).
Hacia atrás, `DROP INDEX CONCURRENTLY` (el `CHECK` queda, validado)."""

from typing import Any

from django.db import migrations, models

INDEX = "messages_echo_token_uq"
VALIDATE = "ALTER TABLE public.messages VALIDATE CONSTRAINT messages_echo_token_ck"
STATE = (
    "SELECT i.indisvalid, pg_get_indexdef(i.indexrelid) FROM pg_index i JOIN pg_class c"
    " ON c.oid = i.indexrelid JOIN pg_namespace n ON n.oid = c.relnamespace"
    " WHERE n.nspname = 'public' AND c.relname = %s"
)
COLUMNS = "(organization_id, channel_account_id, echo_token) WHERE"
CREATE = (
    f"CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS {INDEX} ON public.messages {COLUMNS}"
    " echo_token IS NOT NULL"
)
DEFINITION = (
    f"CREATE UNIQUE INDEX {INDEX} ON public.messages USING btree {COLUMNS} (echo_token IS NOT NULL)"
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
    _run(schema_editor, VALIDATE, *((DROP,) if found and not found[0] else ()), CREATE)
    if _state(schema_editor) != (True, DEFINITION):  # `IF NOT EXISTS` solo mira el nombre
        raise RuntimeError(f"0046: {INDEX} no es válido o tiene otra definición; repítela")


def drop(apps: Any, schema_editor: Any) -> None:
    _run(schema_editor, DROP)


class Migration(migrations.Migration):
    atomic = False
    dependencies = [
        ("conversations", "0045_message_echo_token"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AddConstraint(
                    model_name="message",
                    constraint=models.UniqueConstraint(
                        condition=models.Q(("echo_token__isnull", False)),
                        fields=("organization_id", "channel_account", "echo_token"),
                        name=INDEX,
                    ),
                ),
            ],
            database_operations=[migrations.RunPython(create, drop)],
        ),
    ]
