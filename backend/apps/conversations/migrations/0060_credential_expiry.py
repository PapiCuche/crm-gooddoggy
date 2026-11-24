"""F7b-12 (#1077; ADR-035 §3, ADR-022): la caducidad del token de una página de Messenger o
Instagram y sus avisos.

- `expiry_checked_at`: cuándo se leyó la caducidad en Meta (`debug_token`); `NULL`, no se sabe.
  `expires_at` (ya existía, siempre vacía) solo con la caducidad leída (`credentials_expiry_ck`).
- `expiry_warned_days`: el aviso más urgente ya enviado (10 o 3 días), solo con fecha
  (`credentials_expiry_warned_ck`). Rotar crea otra credencial: los avisos empiezan de cero.
- `crm_app` puede cambiar esas tres columnas (`GRANT UPDATE` por columna, como las demás) y el
  disparador `credentials_expiry_guard` (INVOKER, para todo rol) impide reescribir una caducidad
  ya leída o volver atrás un aviso (`NULL` → 10 → 3, o `NULL` → 3).
- `credentials_expiry_due(p_unknown, p_limit)`: SECURITY DEFINER, solo ids de organización con una
  credencial viva de una cuenta social viva que se comprueba (ADR-029 §12) y que tiene un aviso
  pendiente o (con `p_unknown`) la caducidad sin leer. Dueño `crm_migrator`, `search_path` fijo,
  sin EXECUTE para PUBLIC, tope de 500. Enumera organizaciones para un `crm_app` comprometido:
  aceptado (solo ids), como `credentials_stale_organizations`.

Las filas que ya existen no tienen `expires_at`: los CHECK valen sin reescribir nada. Hacia atrás
se borran la función, el disparador, las columnas nuevas y el permiso."""

from typing import Any

from django.conf import settings
from django.db import migrations, models

from core.db.operations import SecurityDefinerFunction

LOCK = "SET LOCAL lock_timeout = '5s'"
COLUMNS = "expires_at, expiry_checked_at, expiry_warned_days"
GUARD = """
CREATE FUNCTION public.credentials_expiry_guard() RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
  IF OLD.expiry_checked_at IS NOT NULL AND (NEW.expiry_checked_at, NEW.expires_at)
     IS DISTINCT FROM (OLD.expiry_checked_at, OLD.expires_at) THEN
    RAISE EXCEPTION 'credentials: a read expiry is never rewritten'
      USING ERRCODE = 'check_violation', CONSTRAINT = 'credentials_expiry_guard';
  END IF;
  IF coalesce(NEW.expiry_warned_days, 32767) > coalesce(OLD.expiry_warned_days, 32767) THEN
    RAISE EXCEPTION 'credentials: a warning never goes back'
      USING ERRCODE = 'check_violation', CONSTRAINT = 'credentials_expiry_guard';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER credentials_expiry_guard
BEFORE UPDATE OF expires_at, expiry_checked_at, expiry_warned_days ON public.credentials
FOR EACH ROW EXECUTE FUNCTION public.credentials_expiry_guard();
"""
UNGUARD = """
DROP TRIGGER IF EXISTS credentials_expiry_guard ON public.credentials;
DROP FUNCTION IF EXISTS public.credentials_expiry_guard();
"""
DUE = (
    "SELECT c.organization_id FROM public.credentials c "
    "JOIN public.channel_accounts a ON a.organization_id = c.organization_id "
    "AND a.id = c.owner_id AND a.credential_id = c.id "
    "WHERE c.revoked_at IS NULL AND a.deleted_at IS NULL "
    "AND a.platform IN ('MESSENGER', 'INSTAGRAM') "
    "AND a.status IN ('PENDING', 'CONNECTED', 'DEGRADED') "
    "AND ((p_unknown AND c.expiry_checked_at IS NULL) "
    "OR (c.expires_at <= now() + interval '10 days' "
    "AND coalesce(c.expiry_warned_days, 32767) > 3)) "
    "GROUP BY c.organization_id ORDER BY min(c.created_at), c.organization_id "
    "LIMIT greatest(0, least(coalesce(p_limit, 0), 500))"
)


def _grant(apps: Any, schema_editor: Any) -> None:
    role = schema_editor.quote_name(settings.DB_APP_ROLE)
    schema_editor.execute(f"GRANT UPDATE ({COLUMNS}) ON public.credentials TO {role}")


def _revoke(apps: Any, schema_editor: Any) -> None:
    role = schema_editor.quote_name(settings.DB_APP_ROLE)
    schema_editor.execute(f"REVOKE UPDATE ({COLUMNS}) ON public.credentials FROM {role}")


class Migration(migrations.Migration):
    dependencies = [
        ("conversations", "0059_failure_settled_once"),
    ]

    operations = [
        migrations.RunSQL(LOCK, migrations.RunSQL.noop),
        migrations.AddField(
            model_name="credential",
            name="expiry_checked_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="credential",
            name="expiry_warned_days",
            field=models.PositiveSmallIntegerField(blank=True, null=True),
        ),
        migrations.AddConstraint(
            model_name="credential",
            constraint=models.CheckConstraint(
                condition=models.Q(expires_at__isnull=True)
                | models.Q(expiry_checked_at__isnull=False),
                name="credentials_expiry_ck",
            ),
        ),
        migrations.AddConstraint(
            model_name="credential",
            constraint=models.CheckConstraint(
                condition=models.Q(expiry_warned_days__isnull=True)
                | models.Q(expiry_warned_days__in=(10, 3), expires_at__isnull=False),
                name="credentials_expiry_warned_ck",
            ),
        ),
        migrations.RunSQL(GUARD, UNGUARD),
        migrations.RunPython(_grant, _revoke),
        SecurityDefinerFunction(
            "credentials_expiry_due",
            arguments="p_unknown boolean, p_limit integer",
            argtypes="boolean, integer",
            returns="SETOF uuid",
            body=DUE,
            search_path="pg_catalog, public, pg_temp",
        ),
    ]
