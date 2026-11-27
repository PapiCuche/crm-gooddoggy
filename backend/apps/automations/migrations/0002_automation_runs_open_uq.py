"""F11-05a (#1180; ADR-049 §6): como mucho una ejecución abierta (no `CLOSED`) por sujeto, regla,
paso y modo, sea cual sea su `episode_at`. Así una hora de `awaiting_reply_since` que cambia dentro
de un episodio (casos a y b de F11-04) nunca abre una segunda ejecución ni una segunda tarea, ni
siquiera con dos barridos a la vez: el `INSERT … ON CONFLICT DO NOTHING` del barrido, sin columnas
de conflicto, toma como árbitros las dos claves únicas.

Mismo patrón que `conversations` 0067: **no atómica**, `CREATE UNIQUE INDEX CONCURRENTLY` con
`lock_timeout` de 5 s; un índice `INVALID` de un intento anterior se borra antes de crearlo, uno
válido se reutiliza y uno con ese nombre y otra definición hace fallar la migración. La tabla es
nueva y nada en `main` escribe en ella (el barrido llega con esta misma migración), así que no
puede haber duplicados abiertos que la hagan fallar; la imagen anterior no la conoce. Hacia atrás,
`DROP INDEX CONCURRENTLY`."""

from typing import Any

from django.db import migrations, models

INDEX = "automation_runs_open_uq"
STATE = (
    "SELECT i.indisvalid, pg_get_indexdef(i.indexrelid) FROM pg_index i JOIN pg_class c"
    " ON c.oid = i.indexrelid JOIN pg_namespace n ON n.oid = c.relnamespace"
    " WHERE n.nspname = 'public' AND c.relname = %s"
)
CREATE = (
    f"CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS {INDEX} ON public.automation_runs"
    " (organization_id, rule_id, subject_id, step, mode) WHERE status <> 'CLOSED'"
)
DEFINITION = (
    f"CREATE UNIQUE INDEX {INDEX} ON public.automation_runs USING btree"
    " (organization_id, rule_id, subject_id, step, mode) WHERE ((status)::text <> 'CLOSED'::text)"
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
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(STATE, [INDEX])
        row = cursor.fetchone()
    return None if row is None else (row[0], row[1])


def create(apps: Any, schema_editor: Any) -> None:
    found = _state(schema_editor)
    _run(schema_editor, *((DROP,) if found and not found[0] else ()), CREATE)
    if _state(schema_editor) != (True, DEFINITION):  # `IF NOT EXISTS` solo mira el nombre
        raise RuntimeError(f"0002: {INDEX} ya existe con otra definición; bórralo y repítela")


def drop(apps: Any, schema_editor: Any) -> None:
    _run(schema_editor, DROP)


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ("automations", "0001_automations"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AddConstraint(
                    model_name="automationrun",
                    constraint=models.UniqueConstraint(
                        condition=models.Q(("status", "CLOSED"), _negated=True),
                        fields=("organization_id", "rule", "subject_id", "step", "mode"),
                        name=INDEX,
                    ),
                ),
            ],
            database_operations=[migrations.RunPython(create, drop)],
        ),
    ]
