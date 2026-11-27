"""F11-03 (ADR-049 §4): la tarea automática enlaza a su conversación o a su cotización. Dos columnas
nulas de `tasks` (`conversation_id`, `quote_id`: sin valor por omisión, solo catálogo, sin
reescribir la tabla) y sus FKs compuestas `(organization_id, …)`, que nacen `NOT VALID`: rigen ya
para toda fila nueva o modificada y la `0003` las valida. Todo en una sola sentencia con
`lock_timeout` de 5 s, en los dos sentidos: `ADD COLUMN` toma `ACCESS EXCLUSIVE` sobre `tasks` y
cada FK, `SHARE ROW EXCLUSIVE` sobre `conversations` y `quotes` (bloquea sus escrituras mientras
dura, que es solo el cambio de catálogo). Si no consigue los bloqueos en 5 s, la migración falla
entera, sin estado a medias, y se repite. `crm_app` ya tiene `SELECT`, `INSERT` y `UPDATE` de la
tabla (no por columna) y la tabla ya tiene RLS con FORCE.

Desplegar y volver atrás (ADR-033 §7): la imagen anterior no conoce las columnas y sigue
escribiendo igual (quedan nulas; ningún `CHECK` ni disparador nuevo). Clase B a mano; C con el
clasificador de #1206 (`RunSQL` y FKs `NOT VALID` sobre tablas que ya existen)."""

from django.db import migrations, models

LOCK = "SET LOCAL lock_timeout = '5s'"
FKS = {"conversation_id": ("tasks_conversation_fk", "conversations"),
       "quote_id": ("tasks_quote_fk", "quotes")}  # fmt: skip
ADD = (
    LOCK,
    "ALTER TABLE public.tasks ADD COLUMN conversation_id uuid NULL, ADD COLUMN quote_id uuid NULL, "
    + ", ".join(
        f"ADD CONSTRAINT {name} FOREIGN KEY (organization_id, {column})"
        f" REFERENCES public.{table} (organization_id, id) NOT VALID"
        for column, (name, table) in FKS.items()
    ),
)
DROP = (
    LOCK,
    "ALTER TABLE public.tasks DROP CONSTRAINT tasks_quote_fk,"
    " DROP CONSTRAINT tasks_conversation_fk, DROP COLUMN quote_id, DROP COLUMN conversation_id",
)


class Migration(migrations.Migration):
    dependencies = [
        ("tasks", "0001_tasks"),
        ("conversations", "0068_backfill_awaiting_reply_since"),
        ("quotes", "0007_revision"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AddField(
                    model_name="task",
                    name=column,
                    field=models.UUIDField(blank=True, null=True),
                )
                for column in FKS
            ],
            database_operations=[migrations.RunSQL(ADD, DROP)],
        ),
    ]
