"""F8-05b4 (#957; ADR-034 §5, ADR-043, ADR-044, ADR-045): los avisos del presupuesto de IA, una
sola vez por organización y mes de Lima. Las marcas viven en la fila del mes (`ai_budget_usage`,
que `crm_app` solo lee), así que el mes siguiente empieza sin ellas. Solo `ai_budget_reserve` las
pone, en la transacción de la reserva, y con ellas inserta el evento del outbox (ADR-045 amplía
ADR-044 solo para eso): el id del tope como agregado y el mes como único dato.

- `ai.budget.threshold_reached`: la reserva lleva lo gastado más lo reservado del mes al 80 % del
  tope mensual o más.
- `ai.budget.exceeded`: el tope mensual corta la reserva. Para que la marca y el evento se
  confirmen, ese corte ya no lanza la excepción: la función devuelve NULL sin reservar nada y el
  gateway responde `AI_BUDGET_EXHAUSTED` sin llamar al proveedor. El corte del día sigue lanzando
  y deshace todo, también la marca del 80 % de esa misma reserva.

Una vez: cada marca se pone con `WHERE … IS NULL` y el evento solo si esa sentencia cambió la fila.
Dos reservas a la vez esperan al bloqueo de la fila del mes y la segunda ya ve la marca. Una marca
puesta no cambia ni vuelve a NULL (disparador `ai_budget_usage_alerted_once`, también para el
dueño). Cada sentencia filtra por `app_current_tenant()` (ADR-044 §2)."""

import importlib
from typing import Any

from django.db import migrations, models

PREVIOUS = importlib.import_module("apps.ai_gateway.migrations.0004_daily_budget")
PURPOSES, _raise = PREVIOUS.PURPOSES, PREVIOUS._raise


def _event(name: str) -> str:
    """El evento del aviso, en la organización de la sesión y con el id de su tope."""
    return f"""INSERT INTO public.outbox_events (id, organization_id, event_type, aggregate_type,
      aggregate_id, payload, published_at, attempts, last_error, correlation_id)
    SELECT uuidv7(), org, '{name}', 'ai_budget', b.id, jsonb_build_object('month', m), NULL, 0,
      '', NULL
    FROM public.ai_budgets b WHERE b.organization_id = org"""  # noqa: S608 — solo constantes


# Como en 0004, más los dos avisos del mes.
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
  IF NOT FOUND THEN  -- el tope del mes la corta: sin reservar nada, el aviso se confirma
    UPDATE public.ai_budget_usage SET exceeded_alerted_at = now()
    WHERE organization_id = org AND month = m AND exceeded_alerted_at IS NULL;
    IF FOUND THEN
      {_event("ai.budget.exceeded")};
    END IF;
    RETURN NULL;
  END IF;
  UPDATE public.ai_budget_usage u SET threshold_alerted_at = now()
  FROM public.ai_budgets b
  WHERE b.organization_id = org AND u.organization_id = org AND u.month = m
    AND u.threshold_alerted_at IS NULL AND u.spent_usd + u.reserved_usd >= b.monthly_usd * 0.8;
  IF FOUND THEN
    {_event("ai.budget.threshold_reached")};
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
# Una marca puesta no cambia ni vuelve a NULL, tampoco para el dueño de las funciones.
GUARD = """
CREATE FUNCTION public.ai_budget_usage_alerted_once() RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
  IF (OLD.threshold_alerted_at IS NOT NULL
      AND NEW.threshold_alerted_at IS DISTINCT FROM OLD.threshold_alerted_at)
     OR (OLD.exceeded_alerted_at IS NOT NULL
         AND NEW.exceeded_alerted_at IS DISTINCT FROM OLD.exceeded_alerted_at) THEN
    RAISE EXCEPTION 'ai_budget_usage: el aviso se marca una sola vez'
      USING ERRCODE = 'check_violation', CONSTRAINT = 'ai_budget_usage_alerted_once';
  END IF;
  RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION public.ai_budget_usage_alerted_once() FROM PUBLIC;
CREATE TRIGGER ai_budget_usage_alerted_once BEFORE UPDATE ON public.ai_budget_usage
FOR EACH ROW EXECUTE FUNCTION public.ai_budget_usage_alerted_once();
"""
DROP_GUARD = (
    "DROP TRIGGER IF EXISTS ai_budget_usage_alerted_once ON public.ai_budget_usage; "
    "DROP FUNCTION IF EXISTS public.ai_budget_usage_alerted_once();"
)


def _function(apps: Any, schema_editor: Any) -> None:
    schema_editor.execute(RESERVE, None)  # OR REPLACE conserva dueño y permisos de 0003


def _restore(apps: Any, schema_editor: Any) -> None:
    schema_editor.execute(PREVIOUS.RESERVE, None)


class Migration(migrations.Migration):
    dependencies = [("ai_gateway", "0005_budget_writes"), ("core", "0003_outbox_events")]

    operations = [
        migrations.AddField(
            model_name="aibudgetusage",
            name="threshold_alerted_at",
            field=models.DateTimeField(null=True),
        ),
        migrations.AddField(
            model_name="aibudgetusage",
            name="exceeded_alerted_at",
            field=models.DateTimeField(null=True),
        ),
        migrations.RunSQL(GUARD, DROP_GUARD),
        migrations.RunPython(_function, _restore),
    ]
