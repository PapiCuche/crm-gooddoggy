"""F3-52: las funciones de particiones de auditoría fijan `pg_temp` al final del `search_path`."""

from django.db import migrations

# Solo cambia el `search_path` de la función: `pg_temp` explícito y al final, para que una tabla o
# un tipo temporal de `crm_app` no se resuelva primero. El cuerpo, el propietario y los permisos
# no se tocan.
FORWARD = """
ALTER FUNCTION public.audit_ensure_partitions(integer)
    SET search_path = pg_catalog, public, pg_temp;
ALTER FUNCTION public.platform_audit_ensure_partitions(integer)
    SET search_path = pg_catalog, public, pg_temp;
"""
REVERSE = """
ALTER FUNCTION public.audit_ensure_partitions(integer) SET search_path = pg_catalog, public;
ALTER FUNCTION public.platform_audit_ensure_partitions(integer)
    SET search_path = pg_catalog, public;
"""


class Migration(migrations.Migration):
    dependencies = [("audit", "0004_audit_filter_indexes")]
    operations = [migrations.RunSQL(FORWARD, REVERSE)]
