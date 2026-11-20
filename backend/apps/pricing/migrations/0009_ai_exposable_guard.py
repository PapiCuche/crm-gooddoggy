"""F8-07f (#1010): la decisión del propietario sobre qué listas cita la IA (D-F8-06, ADR-034 §13)
se sostiene también en la base, no solo en la API y en los comandos.

- `price_lists_internal_not_ai_ck` (CHECK, para todo rol): la lista interna, por su tipo
  (`INTERNAL`) o por quién la ve (`prices.view_internal`), nunca tiene `ai_exposable`. Volver
  interna una lista marcada **falla** si la misma escritura no la desmarca: `update_price_list` la
  desmarca y lo audita; un SQL que no lo hace no la apaga en silencio (sin auditoría), se niega.
- `price_lists_ai_exposable_guard` (disparador BEFORE INSERT OR UPDATE, INVOKER, `search_path`
  fijo, para `crm_app`; el migrador, propietario, no pasa por él): una lista nace sin marcar, y
  `ai_exposable` solo cambia si quien actúa es una persona (`app.actor_type = 'USER'`,
  `app.user_id`) miembro `ACTIVE`, usuario activo, con `ai.manage` y que ve la lista (su
  `required_permission` o `prices.manage`), como pide `set_ai_exposable`. Desmarcar una lista que
  en la misma escritura queda interna lo hace quien la edita (`prices.manage`, el camino de
  `update_price_list`).

**Qué no hace:** el segundo factor reciente y la auditoría siguen en la aplicación (la sesión no
está en la base), y `app.user_id` y `app.actor_type` son variables de la transacción que fija
`tenant_scope`: un SQL hostil de `crm_app` podría fijarlas con el id de un miembro con
`ai.manage` (como en `quotes` 0003 y 0006). Protege de un fallo de la aplicación o de un camino
nuevo que olvide la regla, no de un `crm_app` comprometido. Sin `SECURITY DEFINER` (ADR-002 §3.3).

**Datos que ya existen.** `0008` las apagó todas; una interna marcada después (solo con SQL
directo) se apaga aquí antes del CHECK con su fila de auditoría (`SYSTEM`, `reason =
INTERNAL_LIST`), como hace `update_price_list`. Las demás marcadas siguen igual. Lo hace
`crm_migrator`, que ha de tener `BYPASSRLS` (si no, se niega). Hacia atrás quita el disparador y el
CHECK y no vuelve a marcar nada."""

from importlib import import_module
from typing import Any

from django.conf import settings
from django.db import migrations, models

PREVIOUS = import_module("apps.pricing.migrations.0008_reset_ai_exposable")
INTERNAL = "(kind = 'INTERNAL' OR required_permission = 'prices.view_internal')"
if PREVIOUS.RESET.count("WHERE ai_exposable\n") != 1:
    raise RuntimeError("pricing 0009: the 0008 reset changed; the anchor is not there")
OFF = PREVIOUS.RESET.replace("WHERE ai_exposable\n", f"WHERE ai_exposable AND {INTERNAL}\n")
FAIL = (
    "RAISE EXCEPTION 'price_lists: only ai.manage decides which lists the AI cites'"
    " USING ERRCODE = '23514', CONSTRAINT = 'price_lists_ai_exposable_guard';"
)
GUARD = f"""
CREATE FUNCTION price_lists_ai_exposable_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public, pg_temp AS $fn$
DECLARE who uuid;
BEGIN
    IF current_user <> '__APP__' THEN RETURN NEW; END IF;
    IF TG_OP = 'INSERT' THEN  -- nace sin marcar: la marca el propietario después
        IF NEW.ai_exposable THEN {FAIL} END IF;
        RETURN NEW;
    END IF;
    IF NEW.ai_exposable = OLD.ai_exposable THEN RETURN NEW; END IF;
    IF NOT NEW.ai_exposable AND (NEW.kind = 'INTERNAL'
                                 OR NEW.required_permission = 'prices.view_internal') THEN
        RETURN NEW;  -- queda interna en esta escritura: la desmarca quien la edita
    END IF;
    who := (CASE WHEN current_setting('app.actor_type', true) = 'USER'
                 THEN nullif(current_setting('app.user_id', true), '') END)::uuid;
    IF who IS NULL OR NOT EXISTS (
        SELECT 1 FROM organization_memberships m
          JOIN users u ON u.id = m.user_id
          JOIN membership_roles mr
            ON mr.organization_id = m.organization_id AND mr.membership_id = m.id
          JOIN role_permissions rp
            ON rp.organization_id = m.organization_id AND rp.role_id = mr.role_id
         WHERE m.organization_id = NEW.organization_id AND m.user_id = who
           AND m.status = 'ACTIVE' AND u.is_active
         GROUP BY m.id  -- `ai.manage`, y ve la lista: su permiso de lectura o `prices.manage`
        HAVING bool_or(rp.permission_code = 'ai.manage')
           AND bool_or(rp.permission_code IN (NEW.required_permission, 'prices.manage'))
    ) THEN
        {FAIL}
    END IF;
    RETURN NEW;
END
$fn$;
CREATE TRIGGER price_lists_ai_exposable_guard BEFORE INSERT OR UPDATE ON price_lists
    FOR EACH ROW EXECUTE FUNCTION price_lists_ai_exposable_guard();
"""  # noqa: S608
UNGUARD = """
DROP TRIGGER IF EXISTS price_lists_ai_exposable_guard ON price_lists;
DROP FUNCTION IF EXISTS price_lists_ai_exposable_guard();
"""


def internal_off(apps: Any, schema_editor: Any) -> None:
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user")
        if not cursor.fetchone()[0]:
            raise RuntimeError("pricing 0009: run as crm_migrator with BYPASSRLS (RLS hides rows)")
        cursor.execute("SET LOCAL lock_timeout = '5s'")
        cursor.execute("SELECT public.audit_ensure_partitions(0)")  # el mes en curso
        cursor.execute(OFF, ["INTERNAL_LIST"])


def guard(apps: Any, schema_editor: Any) -> None:
    schema_editor.execute(GUARD.replace("__APP__", settings.DB_APP_ROLE))


def unguard(apps: Any, schema_editor: Any) -> None:
    schema_editor.execute(UNGUARD)


class Migration(migrations.Migration):
    dependencies = [("pricing", "0008_reset_ai_exposable")]

    operations = [
        migrations.RunPython(internal_off, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="pricelist",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("ai_exposable", False),
                    models.Q(
                        models.Q(("kind", "INTERNAL"), _negated=True),
                        models.Q(("required_permission", "prices.view_internal"), _negated=True),
                    ),
                    _connector="OR",
                ),
                name="price_lists_internal_not_ai_ck",
            ),
        ),
        migrations.RunPython(guard, unguard),
    ]
