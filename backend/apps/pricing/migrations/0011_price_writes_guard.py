"""F8-07h (#1028): las guardas de `price_lists` (`0009`, `0010`) no se rodean borrando y creando
otra vez una lista, y los precios que cita la IA solo los escribe quien la aplicación deja.

- **Sin borrado físico de listas:** `REVOKE DELETE, TRUNCATE ON price_lists FROM crm_app`, como
  `product_prices`, `promotions` y `price_change_batches`. La aplicación solo las borra lógicamente
  (`deleted_at`); sin esto, quien tiene `ai.manage` y solo `prices.view` borraba MAYORISTA (sin
  precios), la creaba otra vez con el mismo `id` y `code` visible para `prices.view` y la marcaba.
- `prices_writer_guard` (disparador BEFORE, INVOKER, `search_path` fijo, para `crm_app`; el
  migrador, propietario, no pasa por él): escribir `product_prices` (insertar o cerrar `ends_at`),
  `promotions` (crear, publicar, cancelar) y sus objetivos y listas exige que quien actúa sea una
  persona (`app.actor_type = 'USER'`, `app.user_id`) miembro `ACTIVE`, usuario activo, con
  `prices.manage`, el permiso de la API (`POST …/prices/`, las rutas de promociones). En
  `product_prices` vale también `prices.bulk_update` con el permiso que pide la lista: es quien
  aplicará un lote (#849, ADR-028 §6), solo sobre una lista que ve. Sin esto, `crm_app` copiaba
  los precios de una lista oculta a una marcada, o los rebajaba con una promoción.
- Ningún camino de sistema escribe precios: un precio programado es una fila con `starts_at`
  futuro y una promoción rige entre sus fechas, sin ningún job; la IA no cambia precios
  (ADR-034); un precio de la tienda entra por un lote que confirma una persona (ADR-028 §6). Si un
  día lo hiciera uno, necesitará su propia decisión, no una excepción aquí.
- Su nombre ordena los BEFORE: corre después de `…_immutable`, `promotions_born_draft`,
  `promotions_guard` y `…_guard` de los hijos, que siguen fallando con su nombre.

**Qué no hace:** igual que `0009`, `0010` y `quotes` 0003, `app.user_id` y `app.actor_type` los
fija `tenant_scope` en la transacción; un SQL hostil de `crm_app` podría fijarlos con el id de un
miembro con `prices.manage`. Protege de un fallo de la aplicación o de un camino nuevo que olvide la
regla, no de un `crm_app` comprometido. Sin `SECURITY DEFINER` (ADR-002 §3.3). Hacia atrás quita
los disparadores y la función y devuelve `DELETE` sobre `price_lists`."""

from typing import Any

from django.conf import settings
from django.db import migrations

TABLES = (  # tabla y lo que escribe en ella `crm_app`
    ("product_prices", "INSERT OR UPDATE"),
    ("promotions", "INSERT OR UPDATE"),
    ("promotion_items", "INSERT"),
    ("promotion_price_lists", "INSERT"),
)
GUARD = """
CREATE FUNCTION prices_writer_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public, pg_temp AS $fn$
DECLARE who uuid; seen text;
BEGIN
    IF current_user <> '__APP__' THEN RETURN NEW; END IF;
    IF TG_TABLE_NAME = 'product_prices' THEN  -- `prices.bulk_update` sirve si ve la lista
        SELECT required_permission INTO seen FROM price_lists
         WHERE organization_id = NEW.organization_id AND id = NEW.price_list_id;
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
         GROUP BY m.id
        HAVING bool_or(rp.permission_code = 'prices.manage')
            OR bool_or(rp.permission_code = 'prices.bulk_update')
               AND bool_or(rp.permission_code = seen)
    ) THEN
        RAISE EXCEPTION USING MESSAGE = TG_TABLE_NAME || ': only prices.manage writes prices',
            ERRCODE = '23514', CONSTRAINT = 'prices_writer_guard';
    END IF;
    RETURN NEW;
END
$fn$;
"""


def guard(apps: Any, schema_editor: Any) -> None:
    role = schema_editor.quote_name(settings.DB_APP_ROLE)
    schema_editor.execute(f"REVOKE DELETE, TRUNCATE ON public.price_lists FROM {role}")
    schema_editor.execute(GUARD.replace("__APP__", settings.DB_APP_ROLE))
    for table, events in TABLES:
        schema_editor.execute(
            f"CREATE TRIGGER {table}_writer_guard BEFORE {events} ON {table}"
            " FOR EACH ROW EXECUTE FUNCTION prices_writer_guard()"
        )


def unguard(apps: Any, schema_editor: Any) -> None:
    for table, _ in TABLES:
        schema_editor.execute(f"DROP TRIGGER IF EXISTS {table}_writer_guard ON {table}")
    schema_editor.execute("DROP FUNCTION IF EXISTS prices_writer_guard()")
    role = schema_editor.quote_name(settings.DB_APP_ROLE)
    schema_editor.execute(f"GRANT DELETE ON public.price_lists TO {role}")


class Migration(migrations.Migration):
    dependencies = [("pricing", "0010_price_list_visibility_guard")]

    operations = [migrations.RunPython(guard, unguard)]
