"""F3-52: el trigger de precios fija `pg_temp` al final del `search_path`."""

from django.db import migrations

# Solo cambia el `search_path` de la función: `pg_temp` explícito y al final, para que una tabla o
# un tipo temporal de `crm_app` no se resuelva primero. El cuerpo, el propietario y los permisos
# no se tocan.
FORWARD = """
ALTER FUNCTION public.product_prices_immutable() SET search_path = pg_catalog, public, pg_temp;
"""
REVERSE = """
ALTER FUNCTION public.product_prices_immutable() SET search_path = pg_catalog, public;
"""


class Migration(migrations.Migration):
    dependencies = [("pricing", "0004_promotions_frozen")]
    operations = [migrations.RunSQL(FORWARD, REVERSE)]
