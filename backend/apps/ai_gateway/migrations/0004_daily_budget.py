"""F8-05b2 (#949; ADR-034 §5, ADR-043, ADR-044): el tope diario, con el mismo corte duro que el
mensual. `ai_budgets.daily_usd` y una fila de uso por día de Lima (`ai_budget_daily_usage`), con los
mismos CHECK, RLS y guardas que la del mes; `crm_app` solo la lee. `ai_budget_reserve` comprueba y
reserva en la fila del mes y después en la del día; `ai_budget_settle` ajusta en el mismo orden, en
el día guardado en la reserva (nunca el de hoy). Las firmas no cambian. Sin tope diario no hay
llamadas (`ai_budget_not_set`, falla cerrado): la columna admite NULL para no inventar un tope a una
fila que ya exista. Cada sentencia nueva filtra por `app_current_tenant()` (ADR-044 §2)."""

import importlib
from typing import Any

from django.conf import settings
from django.db import migrations, models

import core.ids
from core.db.operations import EnableRLS

PREVIOUS = importlib.import_module("apps.ai_gateway.migrations.0003_ledger_functions")
PURPOSES, _raise, SIGNATURES = PREVIOUS.PURPOSES, PREVIOUS._raise, PREVIOUS.SIGNATURES

# El día del presupuesto de un instante: el de Lima para toda organización (ADR-043).
DAY = """
CREATE FUNCTION public.ai_budget_day(p_at timestamptz) RETURNS date
LANGUAGE sql STABLE SET search_path = pg_catalog, public, pg_temp
AS $fn$ SELECT (p_at AT TIME ZONE 'America/Lima')::date $fn$
"""
# Como en 0003, más el tope diario: sin él no hay llamadas; la fila del mes y después la del día.
# Si el día no cabe, la excepción deshace también lo reservado en el mes.
RESERVE = f"""
CREATE OR REPLACE FUNCTION public.ai_budget_reserve(p_amount numeric, p_purpose text,
  p_provider text, p_model text, p_conversation_id uuid) RETURNS uuid
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $fn$
DECLARE
  org uuid := public.app_current_tenant();
  who uuid := public.app_current_user();
  m date := public.ai_budget_month(now());
  d date := public.ai_budget_day(now());
  held uuid;
BEGIN
  IF org IS NULL THEN
    {_raise("ai_budget_tenant_required", "ai_budget_reserve: sin organización")};
  END IF;
  IF who IS NULL OR NOT EXISTS (
    SELECT 1 FROM public.organization_memberships ms JOIN public.users u ON u.id = ms.user_id
    WHERE ms.organization_id = org AND ms.user_id = who AND ms.status = 'ACTIVE' AND u.is_active
  ) THEN
    {_raise("ai_budget_requester_member", "ai_budget_reserve: quien pide no es miembro activo")};
  END IF;
  IF p_purpose IS NULL OR p_purpose NOT IN {PURPOSES} THEN
    {_raise("ai_budget_reservations_purpose_ck", "ai_budget_reserve: propósito no válido")};
  END IF;
  IF p_amount IS NULL OR p_amount = 'NaN' OR p_amount <= 0 THEN
    {_raise("ai_budget_res_ck", "ai_budget_reserve: importe no válido")};
  END IF;
  IF to_regclass('public.ai_llm_calls_y' || to_char(now() AT TIME ZONE 'UTC', 'YYYY"m"MM')) IS NULL
     OR to_regclass('public.ai_llm_calls_y' || to_char((now() + interval '1 month')
                                                       AT TIME ZONE 'UTC', 'YYYY"m"MM')) IS NULL
  THEN
    {_raise("ai_llm_calls_partition_missing", "ai_budget_reserve: falta la partición")};
  END IF;
  IF NOT EXISTS (SELECT 1 FROM public.ai_budgets b
                 WHERE b.organization_id = org AND b.daily_usd IS NOT NULL) THEN
    {_raise("ai_budget_not_set", "ai_budget_reserve: sin tope")};
  END IF;
  INSERT INTO public.ai_budget_usage (id, organization_id, month, spent_usd, reserved_usd)
  VALUES (uuidv7(), org, m, 0, 0) ON CONFLICT (organization_id, month) DO NOTHING;
  UPDATE public.ai_budget_usage u SET reserved_usd = u.reserved_usd + p_amount
  FROM public.ai_budgets b
  WHERE b.organization_id = org AND u.organization_id = org AND u.month = m
    AND u.spent_usd + u.reserved_usd + p_amount <= b.monthly_usd;
  IF NOT FOUND THEN
    {_raise("ai_budget_exhausted", "ai_budget_reserve: sin margen")};
  END IF;
  INSERT INTO public.ai_budget_daily_usage (id, organization_id, day, spent_usd, reserved_usd)
  VALUES (uuidv7(), org, d, 0, 0) ON CONFLICT (organization_id, day) DO NOTHING;
  UPDATE public.ai_budget_daily_usage u SET reserved_usd = u.reserved_usd + p_amount
  FROM public.ai_budgets b
  WHERE b.organization_id = org AND u.organization_id = org AND u.day = d
    AND u.spent_usd + u.reserved_usd + p_amount <= b.daily_usd;
  IF NOT FOUND THEN
    {_raise("ai_budget_exhausted", "ai_budget_reserve: sin margen")};
  END IF;
  INSERT INTO public.ai_budget_reservations (id, organization_id, month, day, amount_usd,
    created_at, settled_at, purpose, requested_by_user_id, provider, model, conversation_id)
  VALUES (uuidv7(), org, m, d, p_amount, now(), NULL, p_purpose, who, p_provider, p_model,
    p_conversation_id)
  RETURNING id INTO held;
  RETURN held;
END
$fn$
"""  # noqa: S608 — solo constantes del módulo
# Como en 0003; ajusta el mes y después el día de la reserva, los suyos aunque hoy sea otro.
SETTLE = f"""
CREATE OR REPLACE FUNCTION public.ai_budget_settle(p_reservation uuid, p_outcome text,
  p_cost numeric, p_input integer, p_output integer, p_cache_write integer, p_cache_read integer,
  p_latency_ms integer, p_request_id text) RETURNS text
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $fn$
DECLARE
  org uuid := public.app_current_tenant();
  r public.ai_budget_reservations%ROWTYPE;
  spent numeric;
BEGIN
  IF org IS NULL THEN
    {_raise("ai_budget_tenant_required", "ai_budget_settle: sin organización")};
  END IF;
  IF NOT coalesce(CASE
       WHEN p_outcome = 'OK' THEN p_cost > 0 AND p_cost <> 'NaN'
         AND p_input IS NOT NULL AND p_output IS NOT NULL
       WHEN p_outcome = 'PROVIDER_UNCERTAIN' THEN p_cost IS NULL
         AND num_nulls(p_input, p_output, p_cache_write, p_cache_read) = 4
       WHEN p_outcome IN ('CREDENTIALS_REJECTED', 'PROVIDER_BILLING', 'PROVIDER_UNAVAILABLE',
                          'PROVIDER_ERROR') THEN p_cost = 0
         AND num_nulls(p_input, p_output, p_cache_write, p_cache_read) = 4
     END, false) THEN
    {_raise("ai_budget_settle_cost", "ai_budget_settle: costo no válido para el resultado")};
  END IF;
  UPDATE public.ai_budget_reservations SET settled_at = now()
  WHERE id = p_reservation AND organization_id = org AND settled_at IS NULL
  RETURNING * INTO r;
  IF NOT FOUND THEN
    RETURN NULL;
  END IF;
  spent := coalesce(p_cost, r.amount_usd);
  UPDATE public.ai_budget_usage
  SET reserved_usd = reserved_usd - r.amount_usd, spent_usd = spent_usd + spent
  WHERE organization_id = org AND month = r.month;
  UPDATE public.ai_budget_daily_usage
  SET reserved_usd = reserved_usd - r.amount_usd, spent_usd = spent_usd + spent
  WHERE organization_id = org AND day = r.day;
  INSERT INTO public.ai_llm_calls (organization_id, reservation_id, provider, model, purpose,
    conversation_id, requested_by_user_id, reserved_usd, cost_usd, input_tokens, output_tokens,
    cache_write_tokens, cache_read_tokens, latency_ms, outcome, request_id)
  VALUES (org, r.id, r.provider, r.model, r.purpose, r.conversation_id, r.requested_by_user_id,
    r.amount_usd, spent, p_input, p_output, p_cache_write, p_cache_read, p_latency_ms, p_outcome,
    left(coalesce(p_request_id, ''), 64));
  RETURN CASE WHEN spent > r.amount_usd THEN 'OVER' ELSE 'SETTLED' END;
END
$fn$
"""  # noqa: S608 — solo constantes del módulo
CHECKS = """
ALTER TABLE public.ai_budgets
  ADD CONSTRAINT ai_budgets_daily_not_nan CHECK (daily_usd <> 'NaN');
ALTER TABLE public.ai_budget_daily_usage
  ADD CONSTRAINT ai_budget_daily_usage_organization_fk
    FOREIGN KEY (organization_id) REFERENCES public.organizations (id),
  ADD CONSTRAINT ai_budget_daily_usage_not_nan
    CHECK (spent_usd <> 'NaN' AND reserved_usd <> 'NaN');
CREATE TRIGGER ai_budget_daily_usage_guard BEFORE UPDATE ON public.ai_budget_daily_usage
FOR EACH ROW EXECUTE FUNCTION public.ai_budget_usage_guard();
ALTER TABLE public.ai_budget_reservations ADD CONSTRAINT ai_budget_reservations_day_fk
  FOREIGN KEY (organization_id, day)
  REFERENCES public.ai_budget_daily_usage (organization_id, day);
"""
DROP_CHECKS = """
ALTER TABLE public.ai_budget_reservations DROP CONSTRAINT IF EXISTS ai_budget_reservations_day_fk;
DROP TRIGGER IF EXISTS ai_budget_daily_usage_guard ON public.ai_budget_daily_usage;
ALTER TABLE public.ai_budget_daily_usage
  DROP CONSTRAINT IF EXISTS ai_budget_daily_usage_not_nan,
  DROP CONSTRAINT IF EXISTS ai_budget_daily_usage_organization_fk;
ALTER TABLE public.ai_budgets DROP CONSTRAINT IF EXISTS ai_budgets_daily_not_nan;
"""


def _functions(apps: Any, schema_editor: Any) -> None:
    q = schema_editor.quote_name
    role, owner = q(settings.DB_APP_ROLE), q(settings.DB_MIGRATOR_ROLE)
    schema_editor.execute(DAY, None)
    schema_editor.execute(f"ALTER FUNCTION public.ai_budget_day(timestamptz) OWNER TO {owner}")
    schema_editor.execute("REVOKE ALL ON FUNCTION public.ai_budget_day(timestamptz) FROM PUBLIC")
    schema_editor.execute(RESERVE, None)  # OR REPLACE conserva dueño y permisos de 0003
    schema_editor.execute(SETTLE, None)
    # El día solo cambia por las funciones: `crm_app` lo lee y nada más.
    schema_editor.execute(
        f"REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON public.ai_budget_daily_usage FROM {role}"
    )


def _restore_functions(apps: Any, schema_editor: Any) -> None:
    for body in (PREVIOUS.RESERVE, PREVIOUS.SETTLE):
        body = body.replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1)
        schema_editor.execute(body, None)
    schema_editor.execute("DROP FUNCTION IF EXISTS public.ai_budget_day(timestamptz)")


class Migration(migrations.Migration):
    dependencies = [("ai_gateway", "0003_ledger_functions")]

    # Sin llamadores del gateway todavía (F8-08): la tabla de reservas está vacía y `day` nace
    # NOT NULL sin valor por omisión, como las columnas de 0003.
    operations = [
        migrations.AddField(
            model_name="aibudget",
            name="daily_usd",
            field=models.DecimalField(decimal_places=6, max_digits=12, null=True),
        ),
        migrations.AddConstraint(
            model_name="aibudget",
            constraint=models.CheckConstraint(
                condition=models.Q(("daily_usd__gt", 0)), name="ai_budgets_daily_ck"
            ),
        ),
        migrations.CreateModel(
            name="AIBudgetDailyUsage",
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
                ("day", models.DateField()),
                ("spent_usd", models.DecimalField(decimal_places=6, max_digits=18)),
                ("reserved_usd", models.DecimalField(decimal_places=6, max_digits=12)),
            ],
            options={
                "db_table": "ai_budget_daily_usage",
                "constraints": [
                    models.UniqueConstraint(
                        fields=("organization_id", "day"), name="ai_budget_daily_usage_day_uq"
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("reserved_usd__gte", 0), ("spent_usd__gte", 0)),
                        name="ai_budget_daily_usage_ck",
                    ),
                ],
            },
        ),
        EnableRLS("AIBudgetDailyUsage"),
        migrations.AddField(
            model_name="aibudgetreservation",
            name="day",
            field=models.DateField(),
        ),
        migrations.RunSQL(CHECKS, DROP_CHECKS),
        migrations.RunPython(_functions, _restore_functions),
    ]
