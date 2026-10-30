"""F3-52: la función SECURITY DEFINER del outbox no admite un `pg_temp` implícito."""

from django.db import migrations

# Solo cambia el `search_path` de la función: `pg_temp` explícito y al final, para que una tabla o
# un tipo temporal de `crm_app` no se resuelva primero. El cuerpo, el propietario y los permisos
# no se tocan.
FORWARD = """
ALTER FUNCTION public.outbox_pending_organizations(integer)
    SET search_path = pg_catalog, public, pg_temp;
"""
REVERSE = """
ALTER FUNCTION public.outbox_pending_organizations(integer) SET search_path = pg_catalog, public;
"""


class Migration(migrations.Migration):
    dependencies = [("core", "0003_outbox_events")]
    operations = [migrations.RunSQL(FORWARD, REVERSE)]
