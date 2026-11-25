"""F7b-12c (#1103; ADR-035 §3): la pasada de caducidad sin inanición y con espera por credencial.

- `expiry_attempt_at`: la última vez que la pasada diaria preguntó a Meta por una caducidad que
  no se sabe (la reclama antes de la llamada, con la hora de la base). Hasta 23 horas después no
  se vuelve a preguntar: una vez por pasada diaria (un día menos el margen del horario). Un
  intento a más de 5 minutos en el futuro (escrito a mano) cuenta como ninguno; los 5 minutos son
  el desfase tolerado (`HEALTH_CLOCK_SKEW`, como la `0052`), para que dos pasadas cuyas horas de
  transacción difieren un instante no reclamen las dos la misma pregunta. `crm_app` solo puede
  escribir esa columna nueva (`GRANT UPDATE` por columna, como `health_attempt_at`).
- `credentials_expiry_due` (misma firma: la imagen anterior la sigue llamando igual):
  - solo organizaciones accesibles (`ACTIVE`, `TRIAL`: `ACCESSIBLE_STATUSES`);
  - el predicado exacto de un aviso pendiente, `(sin aviso y caduca en 10 días) o (avisada a 10 y
    caduca en 3)`, para que una credencial ya avisada a 10 días no devuelva su organización cada
    hora;
  - las caducidades sin leer, solo pasada la espera;
  - ordena por el turno más viejo (sin intento primero) y después por organización.
  Mantiene el propietario `crm_migrator`, el `search_path`, el `REVOKE` a `PUBLIC`, el `EXECUTE` a
  `crm_app`, el tope (500) y que solo devuelve ids.

Expandir (ADR-033 §7): la columna admite `NULL`, sin valor por defecto ni CHECK; el disparador
`credentials_expiry_guard` y los de la `0064` no cambian. Toda escritura de la imagen anterior
sigue valiendo. Hacia atrás vuelve el cuerpo exacto de la `0060` y se borran el permiso y la
columna (un intento que se pierde solo adelanta la siguiente pregunta a Meta)."""

from importlib import import_module
from typing import Any

from django.conf import settings
from django.db import migrations, models

from core.db.operations import SecurityDefinerFunction

LOCK = "SET LOCAL lock_timeout = '5s'"
OLD: SecurityDefinerFunction = import_module(
    "apps.conversations.migrations.0060_credential_expiry"
).Migration.operations[-1]
BODY = (
    "SELECT c.organization_id FROM public.credentials c "
    "JOIN public.channel_accounts a ON a.organization_id = c.organization_id "
    "AND a.id = c.owner_id AND a.credential_id = c.id "
    "JOIN public.organizations o ON o.id = c.organization_id "
    "CROSS JOIN LATERAL (SELECT CASE WHEN c.expiry_attempt_at <= now() + interval '5 minutes' "
    "THEN c.expiry_attempt_at END AS tried) t "
    "WHERE c.revoked_at IS NULL AND a.deleted_at IS NULL "
    "AND a.platform IN ('MESSENGER', 'INSTAGRAM') "
    "AND a.status IN ('PENDING', 'CONNECTED', 'DEGRADED') "
    "AND o.status IN ('ACTIVE', 'TRIAL') "
    "AND ((p_unknown AND c.expiry_checked_at IS NULL "
    "AND (t.tried IS NULL OR t.tried <= now() - interval '23 hours')) "
    "OR (c.expiry_warned_days IS NULL AND c.expires_at <= now() + interval '10 days') "
    "OR (c.expiry_warned_days = 10 AND c.expires_at <= now() + interval '3 days')) "
    "GROUP BY c.organization_id "
    "ORDER BY bool_or(t.tried IS NULL) DESC, min(t.tried) NULLS FIRST, c.organization_id "
    "LIMIT greatest(0, least(coalesce(p_limit, 0), 500))"
)


class Replace(SecurityDefinerFunction):
    """Cambia el cuerpo de la función de la `0060`, con el mismo propietario y permisos."""

    def database_forwards(self, app_label: str, schema_editor: Any, *a: Any) -> None:
        super().database_backwards(app_label, schema_editor, *a)  # el mismo `DROP` (misma firma)
        super().database_forwards(app_label, schema_editor, *a)

    def database_backwards(self, app_label: str, schema_editor: Any, *a: Any) -> None:
        super().database_backwards(app_label, schema_editor, *a)
        SecurityDefinerFunction.database_forwards(OLD, app_label, schema_editor, *a)  # la de 0060


def _grant(apps: Any, schema_editor: Any) -> None:
    role = schema_editor.quote_name(settings.DB_APP_ROLE)
    schema_editor.execute(f"GRANT UPDATE (expiry_attempt_at) ON public.credentials TO {role}")


def _revoke(apps: Any, schema_editor: Any) -> None:
    role = schema_editor.quote_name(settings.DB_APP_ROLE)
    schema_editor.execute(f"REVOKE UPDATE (expiry_attempt_at) ON public.credentials FROM {role}")


class Migration(migrations.Migration):
    dependencies = [
        ("conversations", "0064_db_clock_stamps"),
    ]

    operations = [
        migrations.RunSQL(LOCK, migrations.RunSQL.noop),
        migrations.AddField(
            model_name="credential",
            name="expiry_attempt_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RunPython(_grant, _revoke),
        Replace(
            OLD.name,
            OLD.arguments,
            OLD.argtypes,
            OLD.returns,
            BODY,
            language=OLD.language,
            search_path=OLD.search_path,
        ),
    ]
