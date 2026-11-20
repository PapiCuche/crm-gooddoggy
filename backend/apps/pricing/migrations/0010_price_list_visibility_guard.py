"""F8-07g (#1017): quién ve una lista de precios (`required_permission`, y `kind`, del que sale)
lo cambia en la base solo quien gestiona precios, como pide la API (`PATCH`, `prices.manage`).

Sin esto, la guarda de `ai_exposable` (`0009`) se rodeaba en dos sentencias: quien tiene
`ai.manage` y solo `prices.view` abría MAYORISTA a `prices.view`, la marcaba y la volvía a cerrar;
o marcaba REGULAR y la cerraba después. Cada sentencia, sola, pasaba `0009`.

- `price_lists_visibility_guard` (disparador BEFORE UPDATE, INVOKER, `search_path` fijo, para
  `crm_app`; el migrador, propietario, no pasa por él): cambiar `required_permission` o `kind`
  exige que quien actúa sea una persona (`app.actor_type = 'USER'`, `app.user_id`) miembro
  `ACTIVE`, usuario activo, con `prices.manage`. Si ninguno cambia, no mira nada más.
- Un disparador aparte, no el de `0009`: cada regla falla con su nombre (`ERRCODE 23514`), `0009`
  queda como se revisó, y deshacer esta quita solo lo suyo. Los BEFORE corren por orden de nombre:
  `…_ai_exposable_guard` y después `…_visibility_guard`.
- Crear una lista no pasa por aquí (la API lo pide a `prices.manage`, y nace sin marcar por
  `0009`): la regla es sobre cambiar quién ve una que ya existe.

**Qué no hace:** igual que `0009` y `quotes` 0003, `app.user_id` y `app.actor_type` los fija
`tenant_scope` en la transacción; un SQL hostil de `crm_app` podría fijarlos con el id de un
miembro con `prices.manage`. Protege de un fallo de la aplicación o de un camino nuevo que olvide la
regla, no de un `crm_app` comprometido. Sin `SECURITY DEFINER` (ADR-002 §3.3). Hacia atrás quita el
disparador y su función."""

from typing import Any

from django.conf import settings
from django.db import migrations

GUARD = """
CREATE FUNCTION price_lists_visibility_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public, pg_temp AS $fn$
DECLARE who uuid;
BEGIN
    IF current_user <> '__APP__' THEN RETURN NEW; END IF;
    IF NEW.required_permission IS NOT DISTINCT FROM OLD.required_permission
       AND NEW.kind IS NOT DISTINCT FROM OLD.kind THEN
        RETURN NEW;  -- quién la ve no cambia
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
           AND m.status = 'ACTIVE' AND u.is_active AND rp.permission_code = 'prices.manage'
    ) THEN
        RAISE EXCEPTION 'price_lists: only prices.manage changes who sees a price list'
            USING ERRCODE = '23514', CONSTRAINT = 'price_lists_visibility_guard';
    END IF;
    RETURN NEW;
END
$fn$;
CREATE TRIGGER price_lists_visibility_guard BEFORE UPDATE ON price_lists
    FOR EACH ROW EXECUTE FUNCTION price_lists_visibility_guard();
"""
UNGUARD = """
DROP TRIGGER IF EXISTS price_lists_visibility_guard ON price_lists;
DROP FUNCTION IF EXISTS price_lists_visibility_guard();
"""


def guard(apps: Any, schema_editor: Any) -> None:
    schema_editor.execute(GUARD.replace("__APP__", settings.DB_APP_ROLE))


def unguard(apps: Any, schema_editor: Any) -> None:
    schema_editor.execute(UNGUARD)


class Migration(migrations.Migration):
    dependencies = [("pricing", "0009_ai_exposable_guard")]

    operations = [migrations.RunPython(guard, unguard)]
