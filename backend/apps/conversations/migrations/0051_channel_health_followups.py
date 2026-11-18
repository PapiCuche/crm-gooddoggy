"""F7b-06h (ADR-035 §3 y §10): lo que la revisión de F7b-06g dejó pendiente.

- `page_subscribed`: lo último que Meta dijo de la suscripción de la app del CRM a la página
  (`NULL`, no se sabe). Solo Messenger e Instagram (`channel_accounts_page_subscribed_ck`).
- `health_attempt_at`: el último intento de comprobación que no terminó (`SKIPPED`, `UNAVAILABLE`
  o un fallo). `channel_accounts_health_due` ya no devuelve una cuenta intentada desde `p_before`
  y ordena por el turno más viejo (la última comprobación o el último intento): cada cuenta tiene
  como mucho un intento por intervalo y ninguna deja sin turno a las demás. Un intento con fecha
  futura (escrito a mano) cuenta como ninguno: nunca saca a una cuenta de la cola.
- `crm_app` escribe las dos columnas (`GRANT UPDATE` por columna, como sus hermanas). La función
  mantiene la firma, el propietario `crm_migrator`, el `search_path`, el `REVOKE` a `PUBLIC`, el
  `EXECUTE` a `crm_app`, el tope (500) y solo devuelve ids.

Hacia atrás vuelve la función de la `0050` y se borran las columnas (son observaciones que la
siguiente comprobación vuelve a escribir: no se pierde nada que no se recupere)."""

from importlib import import_module
from typing import Any

from django.conf import settings
from django.db import migrations, models

from core.db.operations import SecurityDefinerFunction

LOCK = "SET LOCAL lock_timeout = '5s'"
COLUMNS = "page_subscribed, health_attempt_at"
OLD: SecurityDefinerFunction = import_module(
    "apps.conversations.migrations.0050_page_not_subscribed"
).Migration.operations[-1]
BODY = (
    "SELECT a.organization_id, a.id FROM public.channel_accounts a "
    "CROSS JOIN LATERAL (SELECT CASE WHEN a.health_attempt_at <= now() "
    "THEN a.health_attempt_at END AS tried) t "
    "WHERE a.platform IN ('WHATSAPP', 'MESSENGER', 'INSTAGRAM') AND a.deleted_at IS NULL "
    "AND a.status IN ('PENDING', 'CONNECTED', 'DEGRADED') "
    "AND (a.last_healthcheck_at IS NULL OR a.last_healthcheck_at <= p_before) "
    "AND (t.tried IS NULL OR t.tried <= p_before) "
    "ORDER BY greatest(a.last_healthcheck_at, t.tried) NULLS FIRST, a.id "
    "LIMIT greatest(0, least(coalesce(p_limit, 0), 500))"
)


class Replace(SecurityDefinerFunction):
    """Cambia el cuerpo de la función de la `0050`, con el mismo propietario y permisos."""

    def database_forwards(self, app_label: str, schema_editor: Any, *a: Any) -> None:
        super().database_backwards(app_label, schema_editor, *a)  # el mismo `DROP` (misma firma)
        super().database_forwards(app_label, schema_editor, *a)

    def database_backwards(self, app_label: str, schema_editor: Any, *a: Any) -> None:
        super().database_backwards(app_label, schema_editor, *a)
        SecurityDefinerFunction.database_forwards(OLD, app_label, schema_editor, *a)  # la de 0050


def _grant(apps: Any, schema_editor: Any) -> None:
    role = schema_editor.quote_name(settings.DB_APP_ROLE)
    schema_editor.execute(LOCK)
    schema_editor.execute(f"GRANT UPDATE ({COLUMNS}) ON public.channel_accounts TO {role}")


def _revoke(apps: Any, schema_editor: Any) -> None:
    role = schema_editor.quote_name(settings.DB_APP_ROLE)
    schema_editor.execute(LOCK)
    schema_editor.execute(f"REVOKE UPDATE ({COLUMNS}) ON public.channel_accounts FROM {role}")


class Migration(migrations.Migration):
    dependencies = [
        ("conversations", "0050_page_not_subscribed"),
    ]

    operations = [
        migrations.RunSQL(LOCK, migrations.RunSQL.noop),
        migrations.AddField(
            model_name="channelaccount",
            name="page_subscribed",
            field=models.BooleanField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="channelaccount",
            name="health_attempt_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddConstraint(
            model_name="channelaccount",
            constraint=models.CheckConstraint(
                condition=models.Q(page_subscribed__isnull=True)
                | models.Q(platform__in=["MESSENGER", "INSTAGRAM"]),
                name="channel_accounts_page_subscribed_ck",
            ),
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
