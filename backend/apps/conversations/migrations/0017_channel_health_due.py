"""F7-06b: las cuentas de WhatsApp a las que les toca la comprobación de salud (ADR-029 §12). Una
función `SECURITY DEFINER` mínima (propietario `crm_migrator`, `search_path` con `pg_temp` al final,
`REVOKE` a `PUBLIC`, solo `crm_app`) que devuelve solo ids (organización y cuenta) de las cuentas
vivas `PENDING`, `CONNECTED` o `DEGRADED` sin comprobación desde `p_before`, las más atrasadas
primero y con un tope dentro. La tarea trabaja luego en el scope de cada organización."""

from django.db import migrations

from core.db.operations import SecurityDefinerFunction


class Migration(migrations.Migration):
    dependencies = [
        ("conversations", "0016_late_status_after_unknown"),
    ]

    operations = [
        SecurityDefinerFunction(
            "channel_accounts_health_due",
            arguments="p_before timestamptz, p_limit integer",
            argtypes="timestamptz, integer",
            returns="TABLE (org uuid, account uuid)",
            body="SELECT a.organization_id, a.id FROM public.channel_accounts a "
            "WHERE a.platform = 'WHATSAPP' AND a.deleted_at IS NULL "
            "AND a.status IN ('PENDING', 'CONNECTED', 'DEGRADED') "
            "AND (a.last_healthcheck_at IS NULL OR a.last_healthcheck_at <= p_before) "
            "ORDER BY a.last_healthcheck_at NULLS FIRST, a.id "
            "LIMIT greatest(0, least(coalesce(p_limit, 0), 500))",
            search_path="pg_catalog, public, pg_temp",
        ),
    ]
