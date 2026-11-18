"""F8-05b3 (#952; ADR-034 §5, ADR-044): los topes los fija `crm_app` desde la ruta con `ai.manage`,
bajo RLS con FORCE y sin función nueva: solo puede insertar la fila de su organización (el `id`, por
omisión) y cambiar los dos topes; nunca la borra, ni su `id` ni su organización. El tope diario no
pasa del mensual (NULL, la fila de F8-05b2 solo con el mensual, sigue valiendo). Y, de la revisión
de #950, el día de una reserva cae dentro de su mes."""

from typing import Any

from django.conf import settings
from django.db import migrations, models

DAY_IN_MONTH = (
    "ALTER TABLE public.ai_budget_reservations ADD CONSTRAINT ai_budget_reservations_day_in_month"
    " CHECK (date_trunc('month', day)::date = month)",
    "ALTER TABLE public.ai_budget_reservations"
    " DROP CONSTRAINT IF EXISTS ai_budget_reservations_day_in_month",
)


def _grants(apps: Any, schema_editor: Any) -> None:
    role = schema_editor.quote_name(settings.DB_APP_ROLE)
    schema_editor.execute(
        f"GRANT INSERT (organization_id, monthly_usd, daily_usd) ON public.ai_budgets TO {role}"
    )
    schema_editor.execute(f"GRANT UPDATE (monthly_usd, daily_usd) ON public.ai_budgets TO {role}")


def _revoke(apps: Any, schema_editor: Any) -> None:
    role = schema_editor.quote_name(settings.DB_APP_ROLE)
    schema_editor.execute(f"REVOKE INSERT, UPDATE ON public.ai_budgets FROM {role}")


class Migration(migrations.Migration):
    dependencies = [("ai_gateway", "0004_daily_budget")]

    operations = [
        migrations.AddConstraint(
            model_name="aibudget",
            constraint=models.CheckConstraint(
                condition=models.Q(("daily_usd__lte", models.F("monthly_usd"))),
                name="ai_budgets_daily_le_monthly",
            ),
        ),
        migrations.RunSQL(*DAY_IN_MONTH),
        migrations.RunPython(_grants, _revoke),
    ]
