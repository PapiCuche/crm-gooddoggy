"""F7b-04b (ADR-035 §4–§5): el procesador ve la plataforma de cada ítem y una identidad solo nace
en una cuenta viva.

- `webhook_ingress_claim` (de `0009`) devuelve también `platform` y `routing_id`: sin ellos un ítem
  de Instagram o Messenger se cerraba `IGNORED` (`NO_NUMBER`). Mismo propietario y permisos.
- `contact_identities_live_account` (disparador, INVOKER): insertar una identidad sobre una cuenta
  borrada o que no se ve falla (`check_violation`). Falla **cerrado** bajo la RLS: si la cuenta no
  es visible, tampoco se inserta. El código ya solo enruta a cuentas vivas; esta es la segunda
  barrera.

`lock_timeout` de 5 s en los dos sentidos. Volver atrás deja la función como en `0009` y quita el
disparador."""

from importlib import import_module
from typing import Any

from django.db import migrations

from core.db.operations import SecurityDefinerFunction

LOCK = "SET LOCAL lock_timeout = '5s'"
OLD: SecurityDefinerFunction = next(
    op
    for op in import_module(
        "apps.conversations.migrations.0009_webhook_processing"
    ).Migration.operations
    if getattr(op, "name", None) == "webhook_ingress_claim"
)
CHANGES = (
    ("w.attempts AS c_attempts)", "w.attempts AS c_attempts,\n"
     "              w.platform AS c_platform, w.routing_id AS c_routing)"),
    ("c.c_attempts::integer FROM claimed c;",
     "c.c_attempts::integer, c.c_platform::text, c.c_routing::text FROM claimed c;"),
)  # fmt: skip
LIVE = """
CREATE FUNCTION public.contact_identities_live_account() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public, pg_temp AS $fn$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM public.channel_accounts a
        WHERE a.organization_id = NEW.organization_id AND a.id = NEW.channel_account_id
          AND a.deleted_at IS NULL
    ) THEN
        RAISE EXCEPTION 'contact_identities_live_account: solo en una cuenta viva'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$fn$;
CREATE TRIGGER contact_identities_live_account BEFORE INSERT ON public.contact_identities
    FOR EACH ROW EXECUTE FUNCTION public.contact_identities_live_account();
"""
DROP_LIVE = """
DROP TRIGGER IF EXISTS contact_identities_live_account ON public.contact_identities;
DROP FUNCTION IF EXISTS public.contact_identities_live_account();
"""


def platform(body: str) -> str:
    for old, new in CHANGES:
        if body.count(old) != 1:
            raise ValueError(old)
        body = body.replace(old, new)
    return body


class Replace(SecurityDefinerFunction):
    """Cambia una función de `0009` y lo que devuelve, con el mismo propietario y permisos."""

    def database_forwards(self, app_label: str, schema_editor: Any, *a: Any) -> None:
        OLD.database_backwards(app_label, schema_editor, *a)
        super().database_forwards(app_label, schema_editor, *a)

    def database_backwards(self, app_label: str, schema_editor: Any, *a: Any) -> None:
        super().database_backwards(app_label, schema_editor, *a)
        OLD.database_forwards(app_label, schema_editor, *a)


class Migration(migrations.Migration):
    dependencies = [
        ("conversations", "0041_validate_social_constraints"),
    ]

    operations = [
        migrations.RunSQL(LOCK, migrations.RunSQL.noop),
        Replace(
            OLD.name,
            OLD.arguments,
            OLD.argtypes,
            OLD.returns.removesuffix(")") + ", platform text, routing_id text)",
            platform(OLD.body),
            language=OLD.language,
            search_path=OLD.search_path,
        ),
        migrations.RunSQL(LIVE, DROP_LIVE),
        migrations.RunSQL(migrations.RunSQL.noop, LOCK),  # hacia atrás corre primero
    ]
