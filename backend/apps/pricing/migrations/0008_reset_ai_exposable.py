"""F8-07b (#996): ninguna lista de precios queda marcada para la IA (`ai_exposable`).

Hasta aquí `prices.manage` podía marcarla sin segundo factor al crear o cambiar la lista. La regla
nueva (D-F8-06, ADR-034 §13) la deja al propietario (`ai.manage`, segundo factor reciente,
auditoría), y el propietario decidió el 2026-11-19 (#770) apagarlas todas al cerrar el camino
anterior: las vuelve a marcar con la regla nueva. Cada lista que estaba marcada, de cualquier
organización y también si está borrada, deja una fila de auditoría `price_list.ai_exposable_changed`
(`SYSTEM`, sin persona) con el antes y el después: así se sabe cuáles eran. Nada más cambia (ni
`updated_at` ni `updated_by_user_id`). Las que ya estaban apagadas no dejan rastro.

La cuenta la hace `crm_migrator`, que ha de tener `BYPASSRLS` (con FORCE RLS, un rol sin él no
vería ninguna lista y la migración no apagaría nada en silencio). `lock_timeout` de 5 s: falla en
lugar de esperar sin fin detrás de una transacción larga, y se repite.

**Hacia atrás no hace nada:** volver a marcar listas las expondría a la IA sin la decisión del
propietario. Las filas de auditoría dicen cuáles estaban marcadas si hiciera falta.
"""

from typing import Any

from django.db import migrations

REASON = "F8-07b: D-F8-06, the owner marks the lists again"
RESET = """
WITH reset AS (
  UPDATE public.price_lists SET ai_exposable = false WHERE ai_exposable
  RETURNING organization_id, id, code
)
INSERT INTO public.audit_logs
  (organization_id, actor_type, action, entity_type, entity_id, entity_label, changes, metadata)
SELECT organization_id, 'SYSTEM', 'price_list.ai_exposable_changed', 'price_list', id, code,
       '{"ai_exposable": [true, false]}'::jsonb, jsonb_build_object('reason', %s::text)
FROM reset
"""


def reset(apps: Any, schema_editor: Any) -> None:
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user")
        if not cursor.fetchone()[0]:
            raise RuntimeError("pricing 0008: run as crm_migrator with BYPASSRLS (RLS hides rows)")
        cursor.execute("SET LOCAL lock_timeout = '5s'")
        cursor.execute("SELECT public.audit_ensure_partitions(0)")  # el mes en curso
        cursor.execute(RESET, [REASON])


class Migration(migrations.Migration):
    dependencies = [
        ("pricing", "0007_price_change_batches"),
        ("audit", "0005_partition_functions_search_path"),
    ]
    operations = [migrations.RunPython(reset, migrations.RunPython.noop)]
