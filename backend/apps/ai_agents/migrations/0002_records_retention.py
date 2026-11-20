"""F8-08k (#1033): la retención del detalle de la IA (D-F8-05, ADR-034 §13; ADR-011: 12 meses).

`ai_records_drop_partitions(p_keep_months)` quita las particiones mensuales (UTC) de
`ai_llm_calls`, `ai_runs` y `ai_actions` que acabaron hace más de `p_keep_months` meses completos
y devuelve solo su nombre y cuántas filas tenían. Con el mes actual en M, la última que quita es
la de M - 13: la de M - 12 todavía guarda filas de hace menos de un año. Nunca el mes actual ni los
siguientes: `p_keep_months` va de 12 a 120.

Decide por los límites reales, no solo por el nombre: si una candidata no cubre exactamente el mes
UTC de su nombre, o guarda una fila de después del corte, la función falla y no quita nada. Dos
pasadas: primero bloquea (las tablas padre, todas a la vez o ninguna, y después las particiones) y
cuenta; después quita. Espera 5 s como mucho (y `lock_timeout` de 5 s, como el DDL sobre tablas
vivas en `core.db.member_fk`) y nunca espera a una tabla con otra bloqueada: los INSERT del
runtime no se quedan esperando sin límite. Si no consigue los bloqueos, falla con 55P03.

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
LANGUAGE plpgsql VOLATILE
SET search_path = pg_catalog, public, pg_temp
SET lock_timeout = '5s'
AS $fn$
DECLARE
  tables CONSTANT text[] := ARRAY['ai_llm_calls', 'ai_runs', 'ai_actions'];
  kept date;
  cutoff timestamptz;
  t text;
  part text;
  parts text[] := '{}';
  parents text[] := '{}';
  counts bigint[] := '{}';
  m date;
  young bigint;
  locked boolean := false;
BEGIN
  IF p_keep_months IS NULL OR p_keep_months < 12 OR p_keep_months > 120 THEN
    RAISE EXCEPTION 'p_keep_months fuera de rango: %', p_keep_months;
  END IF;
  kept := (date_trunc('month', now() AT TIME ZONE 'UTC')
           - make_interval(months => p_keep_months))::date;  -- el primer mes que se guarda
  cutoff := kept::timestamp AT TIME ZONE 'UTC';
  FOREACH t IN ARRAY tables LOOP
    FOR part IN
      SELECT c.relname::text FROM pg_catalog.pg_inherits i
        JOIN pg_catalog.pg_class c ON c.oid = i.inhrelid
       WHERE i.inhparent = ('public.' || t)::regclass
         AND c.relnamespace = 'public'::regnamespace
         AND c.relname ~ ('^' || t || '_y[1-9][0-9]{3}m(0[1-9]|1[0-2])$')
         AND to_date(right(c.relname, 7), 'YYYY"m"MM') < kept  -- fechas: sin la zona de la sesión
       ORDER BY 1
    LOOP
      parts := parts || part;
      parents := parents || t;
    END LOOP;
  END LOOP;
  IF cardinality(parts) = 0 THEN
    RETURN;
  END IF;
  -- Pasada 1: los padres y después las particiones, todo o nada (NOWAIT en un subbloque: un
  -- intento fallido suelta lo que tomó, así que nunca se espera a una tabla con otra bloqueada),
  -- durante 5 s como mucho; después comprueba y cuenta.
  FOR attempt IN 1..50 LOOP
    BEGIN
      FOREACH t IN ARRAY tables LOOP
        IF t = ANY (parents) THEN
          EXECUTE format('LOCK TABLE ONLY public.%I IN ACCESS EXCLUSIVE MODE NOWAIT', t);
        END IF;
      END LOOP;
      FOREACH part IN ARRAY parts LOOP
        EXECUTE format('LOCK TABLE public.%I IN ACCESS EXCLUSIVE MODE NOWAIT', part);
      END LOOP;
      locked := true;
      EXIT;
    EXCEPTION WHEN lock_not_available THEN
      PERFORM pg_sleep(0.1);
    END;
  END LOOP;
  IF NOT locked THEN
    RAISE EXCEPTION 'ai_records_drop_partitions: tablas ocupadas'
      USING ERRCODE = 'lock_not_available';
  END IF;
  FOR i IN 1..cardinality(parts) LOOP
    part := parts[i];
    m := to_date(right(part, 7), 'YYYY"m"MM');
    IF (SELECT pg_get_expr(c.relpartbound, c.oid) FROM pg_catalog.pg_class c
         WHERE c.oid = ('public.' || part)::regclass)
       IS DISTINCT FROM format('FOR VALUES FROM (%L) TO (%L)',
                               (m::timestamp AT TIME ZONE 'UTC')::text,
                               ((m + interval '1 month')::timestamp AT TIME ZONE 'UTC')::text) THEN
      RAISE EXCEPTION 'ai_records_drop_partitions: % no cubre el mes de su nombre', part;
    END IF;
    EXECUTE format('SELECT count(*), count(*) FILTER (WHERE created_at >= %L) FROM public.%I',
                   cutoff, part) INTO row_count, young;
    IF young > 0 THEN  -- imposible con el límite comprobado; por si acaso
      RAISE EXCEPTION 'ai_records_drop_partitions: % tiene filas de después del corte', part;
    END IF;
    counts := counts || row_count;
  END LOOP;
  -- Pasada 2: todo bloqueado y contado; ahora se quita.
  FOR i IN 1..cardinality(parts) LOOP
    EXECUTE format('DROP TABLE public.%I', parts[i]);
    partition_name := parts[i];
    row_count := counts[i];
    RETURN NEXT;
  END LOOP;
END
$fn$
"""


def _create(apps: Any, schema_editor: Any) -> None:
    schema_editor.execute(DROP, None)  # params=None: el cuerpo lleva `%`
    schema_editor.execute(
        f"ALTER FUNCTION {FUNCTION} OWNER TO {schema_editor.quote_name(settings.DB_MIGRATOR_ROLE)}"
    )
    # Los bootstrap ya quitan EXECUTE a PUBLIC por omisión, pero solo para las funciones que crea
    # `crm_migrator`: si un superusuario corre la migración, sin esta línea PUBLIC la ejecutaría.
    schema_editor.execute(f"REVOKE ALL ON FUNCTION {FUNCTION} FROM PUBLIC")


def _drop(apps: Any, schema_editor: Any) -> None:
    schema_editor.execute(f"DROP FUNCTION IF EXISTS {FUNCTION}")


class Migration(migrations.Migration):
    dependencies = [
        ("ai_agents", "0001_copilot_records"),
        ("ai_gateway", "0002_budget_and_calls"),  # `ai_llm_calls`
    ]

    operations = [migrations.RunPython(_create, _drop)]
