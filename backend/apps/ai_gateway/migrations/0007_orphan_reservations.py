"""F8-05b5 (#962; ADR-034 §5, ADR-044): las reservas huérfanas se cierran como gastadas.

- `ai_budget_settle` admite un resultado propio, `ORPHANED`, con la regla de `PROVIDER_UNCERTAIN`:
  sin costo ni tokens, cuenta la reserva entera y nunca la libera. Solo cierra una reserva de más
  de `ORPHAN_AGE`; una más joven devuelve NULL y no cambia nada. Misma firma, dueño, `search_path`
  y permisos; ninguna sentencia nueva: cada una sigue filtrando por `app_current_tenant()`. ADR-044
  («Consequences») deja ampliar esta función para el barrido de huérfanas con las mismas
  condiciones; no hay ninguna función nueva que escriba.
- `ai_budget_orphan_organizations(p_limit)`: SECURITY DEFINER de solo lectura (ADR-002 §3.3) que
  devuelve solo los ids de las organizaciones con reservas huérfanas, la más atrasada primero, con
  tope de 500. El plazo va dentro: no sirve para saber quién tiene una llamada en curso.
- El CHECK de `ai_llm_calls.outcome` admite `ORPHANED`. Volver atrás con filas `ORPHANED` falla con
  un error claro: nunca las borra."""

import importlib
from typing import Any

from django.db import migrations, models

from core.db.operations import SecurityDefinerFunction

PREVIOUS = importlib.import_module("apps.ai_gateway.migrations.0004_daily_budget")
FIRST = importlib.import_module("apps.ai_gateway.migrations.0003_ledger_functions")
ORPHAN_AGE = "interval '15 minutes'"  # el mismo plazo que `apps.ai_gateway.orphans.ORPHAN_AFTER`
OUTCOMES = FIRST.OUTCOMES.replace("'PROVIDER_UNCERTAIN')", "'PROVIDER_UNCERTAIN', 'ORPHANED')")
EDITS = (  # (lo de 0004, lo nuevo): cada uno aparece una sola vez en el cuerpo de 0004
    (
        "WHEN p_outcome = 'PROVIDER_UNCERTAIN' THEN",
        "WHEN p_outcome IN ('PROVIDER_UNCERTAIN', 'ORPHANED') THEN",
    ),
    (
        "AND settled_at IS NULL\n  RETURNING * INTO r;",
        "AND settled_at IS NULL\n"
        f"    AND (p_outcome <> 'ORPHANED' OR created_at <= now() - {ORPHAN_AGE})\n"
        "  RETURNING * INTO r;",
    ),
)


def _settle() -> str:
    body: str = PREVIOUS.SETTLE
    for old, new in EDITS:
        if body.count(old) != 1:
            raise ValueError(old)
        body = body.replace(old, new)
    return body


SETTLE = _settle()
CHECK = (
    "ALTER TABLE public.ai_llm_calls DROP CONSTRAINT ai_llm_calls_outcome_check, "
    "ADD CONSTRAINT ai_llm_calls_outcome_check CHECK (outcome IN {})"
)
DISCOVER = (  # solo constantes del módulo
    "SELECT r.organization_id FROM public.ai_budget_reservations r "  # noqa: S608
    f"WHERE r.settled_at IS NULL AND r.created_at <= now() - {ORPHAN_AGE} "
    "GROUP BY r.organization_id ORDER BY min(r.created_at), r.organization_id "
    "LIMIT greatest(0, least(coalesce(p_limit, 0), 500))"
)


def _forwards(apps: Any, schema_editor: Any) -> None:
    schema_editor.execute(CHECK.format(OUTCOMES))
    schema_editor.execute(SETTLE, None)  # OR REPLACE conserva dueño y permisos de 0003


def _backwards(apps: Any, schema_editor: Any) -> None:
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            "SELECT EXISTS (SELECT 1 FROM public.ai_llm_calls WHERE outcome = 'ORPHANED')"
        )
        if cursor.fetchone()[0]:
            raise RuntimeError(
                "ai_llm_calls tiene filas ORPHANED: no se vuelve atrás (nunca se borran)"
            )
    schema_editor.execute(PREVIOUS.SETTLE, None)
    schema_editor.execute(CHECK.format(FIRST.OUTCOMES))


class Migration(migrations.Migration):
    dependencies = [("ai_gateway", "0006_budget_alerts")]

    operations = [
        migrations.AddIndex(  # las reservas abiertas, para el descubrimiento
            model_name="aibudgetreservation",
            index=models.Index(
                condition=models.Q(("settled_at__isnull", True)),
                fields=["created_at"],
                name="ai_budget_res_open_idx",
            ),
        ),
        migrations.RunPython(_forwards, _backwards),
        SecurityDefinerFunction(
            "ai_budget_orphan_organizations",
            arguments="p_limit integer",
            argtypes="integer",
            returns="SETOF uuid",
            body=DISCOVER,
            search_path="pg_catalog, public, pg_temp",
        ),
    ]
