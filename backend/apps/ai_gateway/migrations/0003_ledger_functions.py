"""F8-05b1 (#942; ADR-034 §5, ADR-043, ADR-002 §3.3): el registro de gasto de IA solo cambia por
dos funciones SECURITY DEFINER, `ai_budget_reserve` y `ai_budget_settle`. `crm_app` pierde el
INSERT directo en `ai_budget_usage`, `ai_budget_reservations` y `ai_llm_calls` y sus permisos de
columna de UPDATE: ya no baja `reserved_usd` ni inventa llamadas. La reserva guarda el propósito,
quien pide (de la sesión), el proveedor, el modelo y la conversación, así que el ajuste no falla
por ellos con el proveedor ya pagado. El mes es el de Lima (ADR-043); las particiones, por mes UTC.

Las dos funciones son de `crm_migrator`, que tiene BYPASSRLS: cada sentencia filtra por
`app_current_tenant()` de forma explícita y la organización y el usuario nunca son parámetros."""

from typing import Any

from django.conf import settings
from django.db import migrations, models

PURPOSES = "('SUGGEST', 'SUMMARY')"
OUTCOMES = (
    "('OK', 'CREDENTIALS_REJECTED', 'PROVIDER_BILLING', 'PROVIDER_UNAVAILABLE', "
    "'PROVIDER_ERROR', 'PROVIDER_UNCERTAIN')"
)
COLUMNS = f"""
ALTER TABLE public.ai_budget_reservations
  ADD CONSTRAINT ai_budget_reservations_purpose_ck CHECK (purpose IN {PURPOSES}),
  ADD CONSTRAINT ai_budget_reservations_provider_ck CHECK (provider IN ('ANTHROPIC')),
  ADD CONSTRAINT ai_budget_reservations_model_ck CHECK (model <> ''),
  ADD CONSTRAINT ai_budget_reservations_requested_by_fk
    FOREIGN KEY (organization_id, requested_by_user_id)
    REFERENCES public.organization_memberships (organization_id, user_id);
ALTER TABLE public.ai_llm_calls DROP CONSTRAINT ai_llm_calls_outcome_check,
  ADD CONSTRAINT ai_llm_calls_outcome_check CHECK (outcome IN {OUTCOMES});
"""
DROP_COLUMNS = """
ALTER TABLE public.ai_llm_calls DROP CONSTRAINT ai_llm_calls_outcome_check,
  ADD CONSTRAINT ai_llm_calls_outcome_check CHECK (outcome IN ('OK', 'CREDENTIALS_REJECTED',
    'PROVIDER_BILLING', 'PROVIDER_UNAVAILABLE', 'PROVIDER_ERROR'));
"""


def _raise(constraint: str, text: str) -> str:
    return (
        f"RAISE EXCEPTION '{text}' USING ERRCODE = 'check_violation', CONSTRAINT = '{constraint}'"
    )


# El mes del presupuesto de un instante: el de Lima para toda organización (ADR-043).
MONTH = """
CREATE FUNCTION public.ai_budget_month(p_at timestamptz) RETURNS date
LANGUAGE sql STABLE SET search_path = pg_catalog, public, pg_temp
AS $fn$ SELECT date_trunc('month', p_at AT TIME ZONE 'America/Lima')::date $fn$
"""
# Reserva la cota superior de una llamada antes de llamar al proveedor, en la transacción de quien
# la pide (el gateway la confirma antes de llamar). Fila del mes y, con el tope diario (#932), la
# del día después, siempre en ese orden. Falla con el nombre de una restricción y no deja nada.
RESERVE = f"""
CREATE FUNCTION public.ai_budget_reserve(p_amount numeric, p_purpose text, p_provider text,
  p_model text, p_conversation_id uuid) RETURNS uuid
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $fn$
DECLARE
  org uuid := public.app_current_tenant();
  who uuid := public.app_current_user();
  m date := public.ai_budget_month(now());
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
  -- sin la partición de ahora o la de dentro de un día, el ajuste fallaría con la llamada pagada
  IF to_regclass('public.ai_llm_calls_y' || to_char(now() AT TIME ZONE 'UTC', 'YYYY"m"MM')) IS NULL
     OR to_regclass('public.ai_llm_calls_y'
                    || to_char((now() + interval '1 day') AT TIME ZONE 'UTC', 'YYYY"m"MM')) IS NULL
  THEN
    {_raise("ai_llm_calls_partition_missing", "ai_budget_reserve: falta la partición")};
  END IF;
  IF NOT EXISTS (SELECT 1 FROM public.ai_budgets b WHERE b.organization_id = org) THEN
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
  INSERT INTO public.ai_budget_reservations (id, organization_id, month, amount_usd, created_at,
    settled_at, purpose, requested_by_user_id, provider, model, conversation_id)
  VALUES (uuidv7(), org, m, p_amount, now(), NULL, p_purpose, who, p_provider, p_model,
    p_conversation_id)
  RETURNING id INTO held;
  RETURN held;
END
$fn$
"""  # noqa: S608 — solo constantes del módulo
# Ajusta una reserva propia sin ajustar, una sola vez: `PROVIDER_UNCERTAIN` (salió y no hubo
# respuesta: pudo cobrarla) cuenta la reserva entera y no lleva costo; cualquier otro resultado
# lleva su costo (0 libera: no salió, o un error sin uso). Un costo mayor que lo reservado se
# registra igual (el proveedor ya cobró) y devuelve `OVER`. NULL si no hay nada que ajustar.
SETTLE = f"""
CREATE FUNCTION public.ai_budget_settle(p_reservation uuid, p_outcome text, p_cost numeric,
  p_input integer, p_output integer, p_cache_write integer, p_cache_read integer,
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
  IF (p_outcome IS NOT DISTINCT FROM 'PROVIDER_UNCERTAIN') <> (p_cost IS NULL)
     OR p_cost = 'NaN' OR p_cost < 0 THEN
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
  INSERT INTO public.ai_llm_calls (organization_id, reservation_id, provider, model, purpose,
    conversation_id, requested_by_user_id, reserved_usd, cost_usd, input_tokens, output_tokens,
    cache_write_tokens, cache_read_tokens, latency_ms, outcome, request_id)
  VALUES (org, r.id, r.provider, r.model, r.purpose, r.conversation_id, r.requested_by_user_id,
    r.amount_usd, spent, p_input, p_output, p_cache_write, p_cache_read, p_latency_ms, p_outcome,
    coalesce(p_request_id, ''));
  RETURN CASE WHEN spent > r.amount_usd THEN 'OVER' ELSE 'SETTLED' END;
END
$fn$
"""  # noqa: S608 — solo constantes del módulo
SIGNATURES = (
    "public.ai_budget_reserve(numeric, text, text, text, uuid)",
    "public.ai_budget_settle(uuid, text, numeric, integer, integer, integer, integer, integer,"
    " text)",
)


def _functions(apps: Any, schema_editor: Any) -> None:
    q = schema_editor.quote_name
    role, owner = q(settings.DB_APP_ROLE), q(settings.DB_MIGRATOR_ROLE)
    schema_editor.execute(MONTH, None)
    schema_editor.execute(f"ALTER FUNCTION public.ai_budget_month(timestamptz) OWNER TO {owner}")
    schema_editor.execute("REVOKE ALL ON FUNCTION public.ai_budget_month(timestamptz) FROM PUBLIC")
    schema_editor.execute(RESERVE, None)
    schema_editor.execute(SETTLE, None)
    for function in SIGNATURES:
        schema_editor.execute(f"ALTER FUNCTION {function} OWNER TO {owner}")
        schema_editor.execute(f"REVOKE ALL ON FUNCTION {function} FROM PUBLIC")
        schema_editor.execute(f"GRANT EXECUTE ON FUNCTION {function} TO {role}")
    # El registro solo cambia por las funciones: ni INSERT ni UPDATE directos.
    schema_editor.execute(
        "REVOKE INSERT ON public.ai_budget_usage, public.ai_budget_reservations,"
        f" public.ai_llm_calls FROM {role}"
    )
    schema_editor.execute(
        f"REVOKE UPDATE (spent_usd, reserved_usd) ON public.ai_budget_usage FROM {role}"
    )
    schema_editor.execute(
        f"REVOKE UPDATE (settled_at) ON public.ai_budget_reservations FROM {role}"
    )


def _drop_functions(apps: Any, schema_editor: Any) -> None:
    role = schema_editor.quote_name(settings.DB_APP_ROLE)
    for function in (*SIGNATURES, "public.ai_budget_month(timestamptz)"):
        schema_editor.execute(f"DROP FUNCTION IF EXISTS {function}")
    schema_editor.execute(
        "GRANT INSERT ON public.ai_budget_usage, public.ai_budget_reservations,"
        f" public.ai_llm_calls TO {role}"
    )
    schema_editor.execute(
        f"GRANT UPDATE (spent_usd, reserved_usd) ON public.ai_budget_usage TO {role}"
    )
    schema_editor.execute(f"GRANT UPDATE (settled_at) ON public.ai_budget_reservations TO {role}")


class Migration(migrations.Migration):
    dependencies = [("ai_gateway", "0002_budget_and_calls")]

    # Sin llamadores del gateway todavía (F8-08): la tabla de reservas está vacía y las columnas
    # nuevas nacen NOT NULL sin valor por omisión.
    operations = [
        migrations.AddField(
            model_name="aibudgetreservation",
            name="purpose",
            field=models.CharField(max_length=16),
        ),
        migrations.AddField(
            model_name="aibudgetreservation",
            name="requested_by_user_id",
            field=models.UUIDField(),
        ),
        migrations.AddField(
            model_name="aibudgetreservation",
            name="provider",
            field=models.CharField(max_length=16),
        ),
        migrations.AddField(
            model_name="aibudgetreservation",
            name="model",
            field=models.CharField(max_length=64),
        ),
        migrations.AddField(
            model_name="aibudgetreservation",
            name="conversation_id",
            field=models.UUIDField(null=True),
        ),
        migrations.RunSQL(COLUMNS, DROP_COLUMNS),
        migrations.RunPython(_functions, _drop_functions),
    ]
