"""F8-08k (#1033): la retención del detalle de la IA (D-F8-05, ADR-034 §13; ADR-011: 12 meses).

`ai_records_drop_partitions(p_keep_months)` quita las particiones mensuales (UTC) de
`ai_llm_calls`, `ai_runs` y `ai_actions` que acabaron hace más de `p_keep_months` meses completos
y devuelve solo su nombre y cuántas filas tenían. Con el mes actual en M, la última que quita es
la de M - 13: la de M - 12 todavía guarda filas de hace menos de un año. Nunca el mes actual ni los
siguientes: `p_keep_months` va de 12 a 120.

Como `*_ensure_partitions`: dueño `crm_migrator`, `search_path` fijo, sin EXECUTE para `crm_app`
ni SECURITY DEFINER (el runtime no tiene DDL). La llama el `post_migrate` de `ai_agents` en el
job de migraciones (cada despliegue y `crm-migrate.timer`, el día 15 de cada mes). Una sola
sentencia: si una partición no se puede quitar, no se quita ninguna. Hacia atrás se borra la
función; las particiones quitadas no vuelven.
"""

from typing import Any

from django.conf import settings
from django.db import migrations

FUNCTION = "public.ai_records_drop_partitions(integer)"
DROP = """
CREATE FUNCTION public.ai_records_drop_partitions(p_keep_months integer)
RETURNS TABLE (partition_name text, row_count bigint)
LANGUAGE plpgsql VOLATILE SET search_path = pg_catalog, public, pg_temp AS $fn$
DECLARE
  cutoff date;
  t text;
BEGIN
  IF p_keep_months IS NULL OR p_keep_months < 12 OR p_keep_months > 120 THEN
    RAISE EXCEPTION 'p_keep_months fuera de rango: %', p_keep_months;
  END IF;
  cutoff := (date_trunc('month', now() AT TIME ZONE 'UTC')
             - make_interval(months => p_keep_months))::date;
  FOREACH t IN ARRAY ARRAY['ai_llm_calls', 'ai_runs', 'ai_actions'] LOOP
    FOR partition_name IN
      SELECT c.relname::text FROM pg_catalog.pg_inherits i
        JOIN pg_catalog.pg_class c ON c.oid = i.inhrelid
       WHERE i.inhparent = ('public.' || t)::regclass
         AND c.relnamespace = 'public'::regnamespace
         AND c.relname ~ ('^' || t || '_y[0-9]{4}m(0[1-9]|1[0-2])$')
         AND to_date(right(c.relname, 7), 'YYYY"m"MM') < cutoff
       ORDER BY 1
    LOOP
      EXECUTE format('SELECT count(*) FROM public.%I', partition_name) INTO row_count;
      EXECUTE format('DROP TABLE public.%I', partition_name);
      RETURN NEXT;
    END LOOP;
  END LOOP;
END
$fn$
"""


def _create(apps: Any, schema_editor: Any) -> None:
    schema_editor.execute(DROP, None)  # params=None: el cuerpo lleva `%`
    schema_editor.execute(
        f"ALTER FUNCTION {FUNCTION} OWNER TO {schema_editor.quote_name(settings.DB_MIGRATOR_ROLE)}"
    )
    schema_editor.execute(f"REVOKE ALL ON FUNCTION {FUNCTION} FROM PUBLIC")


def _drop(apps: Any, schema_editor: Any) -> None:
    schema_editor.execute(f"DROP FUNCTION IF EXISTS {FUNCTION}")


class Migration(migrations.Migration):
    dependencies = [
        ("ai_agents", "0001_copilot_records"),
        ("ai_gateway", "0002_budget_and_calls"),  # `ai_llm_calls`
    ]

    operations = [migrations.RunPython(_create, _drop)]
