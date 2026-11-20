"""F8-08f (#1023): `ai_runs.findings` admite 32 tipos de hallazgo por run, no 16.

El Output Guard I usa 10 tipos y los detectores de F8-08f añaden 7 (ADR-034 §9: uno por familia,
más `MARKUP`): 17 no cabían en el CHECK de 0001. Solo cambia el tope; el resto del CHECK (un tipo
con forma de código y un entero >= 0, nunca texto) es el mismo. En la tabla particionada, el CHECK
pasa a cada partición y se valida (bloquea la tabla un momento: `lock_timeout` de 5 s, como el DDL
de `core.db.member_fk`). Hacia atrás vuelve a 16 (falla si ya hay un run con más tipos)."""

from django.db import migrations

CHECK = """
SET LOCAL lock_timeout = '5s';
ALTER TABLE public.ai_runs DROP CONSTRAINT ai_runs_findings_ck, ADD CONSTRAINT ai_runs_findings_ck
  CHECK (jsonb_typeof(findings) = 'object'
    AND jsonb_array_length(jsonb_path_query_array(findings, '$.keyvalue()')) <= {most}
    AND NOT jsonb_path_exists(findings, '$.keyvalue() ? (!(@.key like_regex "^[A-Z][A-Z_]{{0,31}}$"
      && @.value.type() == "number" && @.value.floor() == @.value && @.value >= 0))'));
"""


class Migration(migrations.Migration):
    dependencies = [("ai_agents", "0002_records_retention")]

    operations = [
        migrations.RunSQL(CHECK.format(most=32), reverse_sql=CHECK.format(most=16)),
    ]
