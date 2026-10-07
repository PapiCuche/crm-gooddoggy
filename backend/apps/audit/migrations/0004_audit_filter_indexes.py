"""F2-74: índices de los dos filtros selectivos del listado de auditoría. Como el de F2-73, se
crean en la tabla padre y PostgreSQL los repite en cada partición, también en las nuevas.
`CREATE INDEX` sobre la tabla padre bloquea las inserciones hasta que la migración confirma
(OBS-F2-73-1): hoy la tabla es pequeña.
"""

from django.db import migrations

INDEXES = {
    # La historia de una entidad, de la más reciente a la más antigua.
    "audit_logs_org_entity_idx": "(organization_id, entity_type, entity_id, id)",
    # Lo que hizo un actor.
    "audit_logs_org_actor_idx": "(organization_id, actor_id, id)",
}


class Migration(migrations.Migration):
    dependencies = [("audit", "0003_audit_log_read_model")]
    operations = [
        migrations.RunSQL(
            f"CREATE INDEX {name} ON public.audit_logs {columns}",
            f"DROP INDEX IF EXISTS public.{name}",
        )
        for name, columns in INDEXES.items()
    ]
