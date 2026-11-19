"""F7b-06i (ADR-035 §10): una última comprobación con fecha futura ya no saca a una cuenta de la
cola de salud.

`channel_accounts_health_due` tomaba `last_healthcheck_at` tal cual: un valor futuro (escrito a
mano, o con un reloj adelantado) la dejaba fuera para siempre, porque ninguna pasada volvía a
comprobarla y por tanto nadie lo corregía. Ahora un valor de más de 5 minutos en el futuro cuenta
como ninguno, como ya hacía la `0051` con `health_attempt_at`: la cuenta entra la primera, como una
nunca comprobada; la pasada escribe la hora de la comprobación o la del intento, así que tiene un
turno por intervalo, como las demás, y el tope (500) y el lote acotan la pasada.

Los 5 minutos son el desfase de reloj tolerado: `health_attempt_at` lo escribe la base (`now()`),
pero `last_healthcheck_at` lo escribe el reloj del worker. Sin margen, un worker unos milisegundos
adelantado haría «futura» cada comprobación recién hecha y la cuenta volvería a la cola en la
siguiente pasada (`models.HEALTH_CLOCK_SKEW` es el mismo margen para la pantalla).

Solo cambia el cuerpo: la firma, el propietario `crm_migrator`, el `search_path`, el `REVOKE` a
`PUBLIC`, el `EXECUTE` a `crm_app`, el tope y que solo devuelve ids siguen igual. Hacia atrás
vuelve el cuerpo exacto de la `0051`."""

from importlib import import_module
from typing import Any

from django.db import migrations

from core.db.operations import SecurityDefinerFunction

OLD: SecurityDefinerFunction = import_module(
    "apps.conversations.migrations.0051_channel_health_followups"
).Migration.operations[-1]
BODY = (
    "SELECT a.organization_id, a.id FROM public.channel_accounts a "
    "CROSS JOIN LATERAL (SELECT CASE WHEN a.health_attempt_at <= now() "
    "THEN a.health_attempt_at END AS tried, CASE WHEN a.last_healthcheck_at <= now() + "
    "interval '5 minutes' "
    "THEN a.last_healthcheck_at END AS checked) t "
    "WHERE a.platform IN ('WHATSAPP', 'MESSENGER', 'INSTAGRAM') AND a.deleted_at IS NULL "
    "AND a.status IN ('PENDING', 'CONNECTED', 'DEGRADED') "
    "AND (t.checked IS NULL OR t.checked <= p_before) "
    "AND (t.tried IS NULL OR t.tried <= p_before) "
    "ORDER BY greatest(t.checked, t.tried) NULLS FIRST, a.id "
    "LIMIT greatest(0, least(coalesce(p_limit, 0), 500))"
)


class Replace(SecurityDefinerFunction):
    """Cambia el cuerpo de la función de la `0051`, con el mismo propietario y permisos."""

    def database_forwards(self, app_label: str, schema_editor: Any, *a: Any) -> None:
        super().database_backwards(app_label, schema_editor, *a)  # el mismo `DROP` (misma firma)
        super().database_forwards(app_label, schema_editor, *a)

    def database_backwards(self, app_label: str, schema_editor: Any, *a: Any) -> None:
        super().database_backwards(app_label, schema_editor, *a)
        SecurityDefinerFunction.database_forwards(OLD, app_label, schema_editor, *a)  # la de 0051


class Migration(migrations.Migration):
    dependencies = [
        ("conversations", "0051_channel_health_followups"),
    ]

    operations = [
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
