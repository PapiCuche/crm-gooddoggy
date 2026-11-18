"""F7b-06g (ADR-035 §3 y §10): «página sin suscribir».

- `channel_accounts_detail_ck` admite `PAGE_NOT_SUBSCRIBED`: la app del CRM no está en las apps
  suscritas de la página (`GET /<PAGE_ID>/subscribed_apps`). `channel_accounts` tiene pocas filas:
  el `CHECK` se cambia y se valida aquí, con `lock_timeout` de 5 s.
- `channel_accounts_health_due` (de la `0017`) devuelve también las cuentas vivas de Messenger e
  Instagram, con la misma cadencia, el mismo orden y el mismo tope; sigue devolviendo solo ids
  (organización y cuenta), con el mismo propietario, `search_path` y permisos.

Hacia atrás vuelven la función y el `CHECK` anteriores; el `CHECK` falla si ya hay una cuenta con
el detalle nuevo (no se pierde nada)."""

from importlib import import_module
from typing import Any

from django.db import migrations, models

from core.db.operations import SecurityDefinerFunction

ACCOUNTS, DETAIL_CK = "public.channel_accounts", "channel_accounts_detail_ck"
NEW = "PAGE_NOT_SUBSCRIBED"
DETAILS = (
    "TOKEN_INVALID", "WEBHOOK_NOT_SEEN", "QUALITY_LOW", "LIMIT_REACHED", "DISCONNECTED_BY_ADMIN",
    "PROVIDER_ERROR", "MESSAGES_ACCESS_OFF",
)  # fmt: skip
LOCK = "SET LOCAL lock_timeout = '5s'"
OLD: SecurityDefinerFunction = next(
    op
    for op in import_module(
        "apps.conversations.migrations.0017_channel_health_due"
    ).Migration.operations
    if getattr(op, "name", None) == "channel_accounts_health_due"
)
WHATSAPP = "a.platform = 'WHATSAPP'"


def wider(body: str) -> str:
    if body.count(WHATSAPP) != 1:
        raise ValueError(WHATSAPP)
    return body.replace(WHATSAPP, "a.platform IN ('WHATSAPP', 'MESSENGER', 'INSTAGRAM')")


class Replace(SecurityDefinerFunction):
    """Cambia el cuerpo de la función de `0017`, con el mismo propietario y los mismos permisos."""

    def database_forwards(self, app_label: str, schema_editor: Any, *a: Any) -> None:
        OLD.database_backwards(app_label, schema_editor, *a)
        super().database_forwards(app_label, schema_editor, *a)

    def database_backwards(self, app_label: str, schema_editor: Any, *a: Any) -> None:
        super().database_backwards(app_label, schema_editor, *a)
        OLD.database_forwards(app_label, schema_editor, *a)


def detail_ck(details: tuple[str, ...]) -> models.CheckConstraint:
    return models.CheckConstraint(
        condition=models.Q(status_detail__isnull=True) | models.Q(status_detail__in=details),
        name=DETAIL_CK,
    )


def detail_sql(details: tuple[str, ...]) -> str:
    listed = ", ".join(f"'{value}'" for value in details)
    return (
        f"ALTER TABLE {ACCOUNTS} ADD CONSTRAINT {DETAIL_CK} CHECK (status_detail IS NULL OR "
        f"status_detail IN ({listed}))"
    )


class Migration(migrations.Migration):
    dependencies = [
        ("conversations", "0049_channel_accounts_page_owner"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.RemoveConstraint(model_name="channelaccount", name=DETAIL_CK),
                migrations.AddConstraint(
                    model_name="channelaccount", constraint=detail_ck((*DETAILS, NEW))
                ),
            ],
            database_operations=[
                migrations.RunSQL(
                    [
                        LOCK,
                        f"ALTER TABLE {ACCOUNTS} DROP CONSTRAINT {DETAIL_CK}",
                        detail_sql((*DETAILS, NEW)),
                    ],
                    [
                        LOCK,
                        f"ALTER TABLE {ACCOUNTS} DROP CONSTRAINT {DETAIL_CK}",
                        detail_sql(DETAILS),
                    ],
                ),
            ],
        ),
        Replace(
            OLD.name,
            OLD.arguments,
            OLD.argtypes,
            OLD.returns,
            wider(OLD.body),
            language=OLD.language,
            search_path=OLD.search_path,
        ),
    ]
