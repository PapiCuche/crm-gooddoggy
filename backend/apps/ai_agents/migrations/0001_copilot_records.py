"""F8-08b (#1019): el registro del copiloto (ADR-034 §5, §7; ADR-005 Parte E; ADR-011).

- `ai_runs` (una invocación) y `ai_actions` (cada tool call y su evidencia): de solo inserción para
  `crm_app` (sin UPDATE, DELETE ni TRUNCATE) y particionadas por mes desde su creación, como
  `audit_logs` y `ai_llm_calls`: la clave primaria lleva la clave de partición, cada partición
  tiene RLS con FORCE y su política, y `crm_app` no tiene privilegios sobre ellas.
  `ai_actions.run_id` no tiene FK (`ai_runs` es particionada; ADR-034 §2): lo escribe la misma
  transacción (`records.save`). Ninguna guarda texto: los CHECK solo admiten, en los hallazgos,
  un tipo y un número; en los argumentos, un UUID o un número (la longitud de un texto libre).
- `ai_suggestions` (el borrador o el resumen): `crm_app` inserta y lee; solo puede cambiar `text`
  y solo a NULL (concesión por columna y el disparador `ai_suggestions_guard`, para todo rol),
  la purga de 30 días de D-F8-05 (F8-08j). Sin DELETE ni TRUNCATE.
- Quien pide y la conversación, con FK compuestas a la organización (ADR-001 §3).
- `ai_runs_ensure_partitions` y `ai_actions_ensure_partitions`: como
  `ai_llm_calls_ensure_partitions` (dueño `crm_migrator`, `search_path` fijo, sin EXECUTE para
  `crm_app`: el runtime no tiene DDL). El `post_migrate` de `ai_agents` mantiene el mes actual
  + 12.

Ninguna función SECURITY DEFINER: `crm_app` inserta bajo RLS, como en `audit_logs` (el límite
conocido es el mismo: con SQL directo podría insertar una fila falsa, no cambiarla ni borrarla).
Tablas nuevas, sin datos que migrar; hacia atrás se borran con sus funciones.
"""

from typing import Any

import django.contrib.postgres.fields
from django.conf import settings
from django.db import migrations, models

import core.ids

POLICY = (
    "CREATE POLICY tenant_isolation ON {table} USING (organization_id = app_current_tenant()) "
    "WITH CHECK (organization_id = app_current_tenant())"
)
CODE = "'^[A-Z][A-Z_]{1,31}$'"  # resultados, códigos y motivos: un código, nunca texto
UUID = "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
MEMBER = "FOREIGN KEY (organization_id, requested_by_user_id) REFERENCES public.organization_memberships (organization_id, user_id)"  # noqa: E501
CONVERSATION = "FOREIGN KEY (organization_id, conversation_id) REFERENCES public.conversations (organization_id, id)"  # noqa: E501
RUNS = f"""
CREATE TABLE public.ai_runs (
  id uuid NOT NULL DEFAULT uuidv7(),
  organization_id uuid NOT NULL REFERENCES public.organizations (id),
  created_at timestamptz NOT NULL DEFAULT now(),
  purpose varchar(16) NOT NULL CHECK (purpose IN ('SUGGEST', 'SUMMARY')),
  requested_by_user_id uuid NOT NULL,
  conversation_id uuid NOT NULL,
  outcome varchar(32) NOT NULL CHECK (outcome ~ {CODE}),
  tool_calls integer NOT NULL CHECK (tool_calls >= 0),
  findings jsonb NOT NULL DEFAULT '{{}}',
  reservation_ids uuid[] NOT NULL DEFAULT '{{}}' CHECK (array_position(reservation_ids, NULL) IS NULL),
  retried boolean NOT NULL DEFAULT false,
  handoff_reason varchar(32) NULL CHECK (handoff_reason ~ {CODE}),
  duration_ms integer NOT NULL CHECK (duration_ms >= 0),
  CONSTRAINT ai_runs_findings_ck CHECK (jsonb_typeof(findings) = 'object' AND NOT jsonb_path_exists(
    findings, '$.keyvalue() ? (!(@.key like_regex "^[A-Z][A-Z_]{{0,31}}$" && @.value.type() == "number"))')),
  PRIMARY KEY (organization_id, created_at, id),
  CONSTRAINT ai_runs_requested_by_fk {MEMBER},
  CONSTRAINT ai_runs_conversation_fk {CONVERSATION}
) PARTITION BY RANGE (created_at)
"""  # noqa: E501
ACTIONS = f"""
CREATE TABLE public.ai_actions (
  id uuid NOT NULL DEFAULT uuidv7(),
  organization_id uuid NOT NULL REFERENCES public.organizations (id),
  created_at timestamptz NOT NULL DEFAULT now(),
  run_id uuid NOT NULL,
  tool_use_id varchar(64) NOT NULL CHECK (tool_use_id ~ '^[A-Za-z0-9_-]{{1,64}}$'),
  tool varchar(41) NOT NULL CHECK (tool ~ '^[a-z][a-z0-9_]{{2,40}}$'),
  code varchar(32) NOT NULL CHECK (code ~ {CODE}),
  arguments jsonb NOT NULL DEFAULT '{{}}',
  evidence_id uuid NULL,
  kind varchar(16) NULL CHECK (kind ~ '^[A-Z][A-Z_]{{1,15}}$'),
  as_of timestamptz NULL,
  subjects uuid[] NOT NULL DEFAULT '{{}}' CHECK (array_position(subjects, NULL) IS NULL),
  facts jsonb NOT NULL DEFAULT '{{}}',
  CONSTRAINT ai_actions_arguments_ck CHECK (jsonb_typeof(arguments) = 'object' AND NOT jsonb_path_exists(
    arguments, '$.keyvalue() ? (!(@.key like_regex "^[a-z][a-z0-9_]{{0,40}}$" && (@.value.type() == "number"
    || @.value.type() == "string" && @.value like_regex "{UUID}")))')),
  CONSTRAINT ai_actions_facts_ck CHECK (jsonb_typeof(facts) = 'object' AND NOT jsonb_path_exists(
    facts, '$.keyvalue() ? (!(@.key like_regex "^[a-z][a-z0-9_]{{0,40}}$" && (@.value.type() == "number"
    || @.value.type() == "null")))')),
  CONSTRAINT ai_actions_evidence_ck CHECK ((code = 'OK') = (evidence_id IS NOT NULL)
    AND (evidence_id IS NULL) = (kind IS NULL) AND (evidence_id IS NULL) = (as_of IS NULL)
    AND (evidence_id IS NOT NULL OR (subjects = '{{}}' AND facts = '{{}}'))),
  PRIMARY KEY (organization_id, created_at, id)
) PARTITION BY RANGE (created_at)
"""  # noqa: E501
ACTIONS_INDEX = "CREATE INDEX ai_actions_run_idx ON public.ai_actions (organization_id, run_id)"
FINDING_KEYS = '@.key == "type" || @.key == "start" || @.key == "end"'
SUGGESTIONS = f"""
CREATE TABLE public.ai_suggestions (
  id uuid PRIMARY KEY DEFAULT uuidv7(),
  organization_id uuid NOT NULL REFERENCES public.organizations (id),
  created_at timestamptz NOT NULL DEFAULT now(),
  run_id uuid NOT NULL,
  conversation_id uuid NOT NULL,
  requested_by_user_id uuid NOT NULL,
  kind varchar(16) NOT NULL CHECK (kind IN ('SUGGEST', 'SUMMARY')),
  text text NULL CHECK (char_length(text) BETWEEN 1 AND 10000),
  findings jsonb NOT NULL DEFAULT '[]',
  handoff_reason varchar(32) NULL CHECK (handoff_reason ~ {CODE}),
  evidence_expires_at timestamptz NULL,
  CONSTRAINT ai_suggestions_findings_ck CHECK (jsonb_typeof(findings) = 'array'
    AND NOT jsonb_path_exists(findings, '$[*] ? (!(@.type() == "object"
      && @."type".type() == "string" && @."type" like_regex "^[A-Z][A-Z_]{{0,31}}$"
      && @."start".type() == "number" && @."end".type() == "number"
      && @."start" >= 0 && @."end" >= @."start"))')
    AND NOT jsonb_path_exists(findings, '$[*].keyvalue() ? (!({FINDING_KEYS}))')),
  CONSTRAINT ai_suggestions_run_uq UNIQUE (organization_id, run_id),
  CONSTRAINT ai_suggestions_requested_by_fk {MEMBER},
  CONSTRAINT ai_suggestions_conversation_fk {CONVERSATION}
);
CREATE INDEX ai_suggestions_conversation_idx ON public.ai_suggestions
  (organization_id, conversation_id, created_at);
CREATE FUNCTION public.ai_suggestions_guard() RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
  IF NEW.text IS NOT NULL OR to_jsonb(NEW) - 'text' IS DISTINCT FROM to_jsonb(OLD) - 'text' THEN
    RAISE EXCEPTION 'ai_suggestions: only the text changes, and only to NULL'
      USING ERRCODE = 'check_violation', CONSTRAINT = 'ai_suggestions_text_purge_only';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER ai_suggestions_guard BEFORE UPDATE ON public.ai_suggestions
FOR EACH ROW EXECUTE FUNCTION public.ai_suggestions_guard();
"""  # noqa: E501
# El mes actual y los `p_months` siguientes (UTC), como `ai_llm_calls_ensure_partitions`.
ENSURE = """
CREATE FUNCTION public.{table}_ensure_partitions(p_months integer) RETURNS integer
LANGUAGE plpgsql VOLATILE SET search_path = pg_catalog, public, pg_temp AS $fn$
DECLARE
  m date := date_trunc('month', now() AT TIME ZONE 'UTC')::date;
  part text;
  created integer := 0;
BEGIN
  IF p_months IS NULL OR p_months < 0 OR p_months > 36 THEN
    RAISE EXCEPTION 'p_months fuera de rango: %', p_months;
  END IF;
  FOR i IN 0..p_months LOOP
    part := '{table}_y' || to_char(m, 'YYYY') || 'm' || to_char(m, 'MM');
    IF to_regclass('public.' || part) IS NULL THEN
      EXECUTE format('CREATE TABLE public.%I PARTITION OF public.{table} '
                     'FOR VALUES FROM (%L) TO (%L)',
                     part, m::timestamp AT TIME ZONE 'UTC',
                     (m + interval '1 month')::timestamp AT TIME ZONE 'UTC');
      EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', part);
      EXECUTE format('ALTER TABLE public.%I FORCE ROW LEVEL SECURITY', part);
      EXECUTE format('{policy}', part);
      EXECUTE format('REVOKE ALL ON public.%I FROM %I', part, {app_role_literal});
      created := created + 1;
    END IF;
    m := (m + interval '1 month')::date;
  END LOOP;
  RETURN created;
END
$fn$
"""
PARTITIONED = (("ai_runs", RUNS), ("ai_actions", ACTIONS))


def _create(apps: Any, schema_editor: Any) -> None:
    q = schema_editor.quote_name
    role, owner = q(settings.DB_APP_ROLE), q(settings.DB_MIGRATOR_ROLE)
    literal = "'" + settings.DB_APP_ROLE.replace("'", "''") + "'"
    policy = POLICY.format(table="public.%I").replace("'", "''")
    for table, create in (*PARTITIONED, ("ai_suggestions", SUGGESTIONS)):
        schema_editor.execute(create, None)  # params=None: los cuerpos llevan `%`
        schema_editor.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
        schema_editor.execute(f"ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY")
        schema_editor.execute(POLICY.format(table=f"public.{table}"))
        schema_editor.execute(f"REVOKE UPDATE, DELETE, TRUNCATE ON public.{table} FROM {role}")
    schema_editor.execute(ACTIONS_INDEX)
    schema_editor.execute(f"GRANT UPDATE (text) ON public.ai_suggestions TO {role}")
    for table, _ in PARTITIONED:
        function = f"public.{table}_ensure_partitions(integer)"
        sql = ENSURE.format(table=table, policy=policy, app_role_literal=literal)
        schema_editor.execute(sql, None)
        schema_editor.execute(f"ALTER FUNCTION {function} OWNER TO {owner}")
        schema_editor.execute(f"REVOKE ALL ON FUNCTION {function} FROM PUBLIC")
        schema_editor.execute(f"SELECT {function.removesuffix('(integer)')}(12)")


def _drop(apps: Any, schema_editor: Any) -> None:
    for table in ("ai_suggestions", "ai_actions", "ai_runs"):
        schema_editor.execute(f"DROP TABLE IF EXISTS public.{table} CASCADE")
    for function in ("ai_suggestions_guard()", "ai_actions_ensure_partitions(integer)",
                     "ai_runs_ensure_partitions(integer)"):  # fmt: skip
        schema_editor.execute(f"DROP FUNCTION IF EXISTS public.{function}")


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ("conversations", "0052_channel_health_future_check"),
        ("organizations", "0013_invitation_sends_guard"),
    ]

    operations = [
        migrations.RunPython(_create, _drop),
        # Solo el estado de Django: los modelos son `managed = False`.
        migrations.CreateModel(
            name="AIAction",
            fields=[
                ("organization_id", models.UUIDField(db_index=True, editable=False)),
                (
                    "id",
                    models.UUIDField(
                        db_default=models.Func(function="uuidv7", output_field=models.UUIDField()),
                        default=core.ids.new_id,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("created_at", models.DateTimeField()),
                ("run_id", models.UUIDField()),
                ("tool_use_id", models.CharField(max_length=64)),
                ("tool", models.CharField(max_length=41)),
                ("code", models.CharField(max_length=32)),
                ("arguments", models.JSONField()),
                ("evidence_id", models.UUIDField(null=True)),
                ("kind", models.CharField(max_length=16, null=True)),
                ("as_of", models.DateTimeField(null=True)),
                (
                    "subjects",
                    django.contrib.postgres.fields.ArrayField(
                        base_field=models.UUIDField(), size=None
                    ),
                ),
                ("facts", models.JSONField()),
            ],
            options={
                "db_table": "ai_actions",
                "managed": False,
            },
        ),
        migrations.CreateModel(
            name="AIRun",
            fields=[
                ("organization_id", models.UUIDField(db_index=True, editable=False)),
                (
                    "id",
                    models.UUIDField(
                        db_default=models.Func(function="uuidv7", output_field=models.UUIDField()),
                        default=core.ids.new_id,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("created_at", models.DateTimeField()),
                ("purpose", models.CharField(max_length=16)),
                ("requested_by_user_id", models.UUIDField()),
                ("conversation_id", models.UUIDField()),
                ("outcome", models.CharField(max_length=32)),
                ("tool_calls", models.IntegerField()),
                ("findings", models.JSONField()),
                (
                    "reservation_ids",
                    django.contrib.postgres.fields.ArrayField(
                        base_field=models.UUIDField(), size=None
                    ),
                ),
                ("retried", models.BooleanField()),
                ("handoff_reason", models.CharField(max_length=32, null=True)),
                ("duration_ms", models.IntegerField()),
            ],
            options={
                "db_table": "ai_runs",
                "managed": False,
            },
        ),
        migrations.CreateModel(
            name="AISuggestion",
            fields=[
                ("organization_id", models.UUIDField(db_index=True, editable=False)),
                (
                    "id",
                    models.UUIDField(
                        db_default=models.Func(function="uuidv7", output_field=models.UUIDField()),
                        default=core.ids.new_id,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("created_at", models.DateTimeField()),
                ("run_id", models.UUIDField()),
                ("conversation_id", models.UUIDField()),
                ("requested_by_user_id", models.UUIDField()),
                ("kind", models.CharField(max_length=16)),
                ("text", models.TextField(null=True)),
                ("findings", models.JSONField()),
                ("handoff_reason", models.CharField(max_length=32, null=True)),
                ("evidence_expires_at", models.DateTimeField(null=True)),
            ],
            options={
                "db_table": "ai_suggestions",
                "managed": False,
            },
        ),
    ]
