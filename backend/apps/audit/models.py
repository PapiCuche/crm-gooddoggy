"""`audit_logs` es SQL (migrations/0001): particionada y, para `crm_app`, solo `SELECT` e
`INSERT`. Este modelo no la gestiona (`managed = False`): existe para leerla (F2-73) y para que
se emita `post_migrate`. Se escribe solo con `services.record()`, que redacta.
"""

from typing import Any, NoReturn

from django.db import models

from core.db.models import TenantManager, TenantModel, uuid7_primary_key

# Los que admite el `CHECK` de la tabla: los de `core.tenancy.ActorType` y `PLATFORM_STAFF`, que
# hoy nada escribe.
ACTOR_TYPES = ("USER", "AI_AGENT", "SYSTEM", "INTEGRATION", "PLATFORM_STAFF")


def _refuse(*args: Any, **kwargs: Any) -> NoReturn:
    raise TypeError("audit_logs solo se escribe con apps.audit.services.record()")


class _ReadOnly(models.QuerySet["AuditLog"]):
    """Sin las escrituras en bloque del ORM, que no pasan por `save()` ni por el redactor."""

    bulk_create = bulk_update = update = delete = _refuse


class AuditLog(TenantModel):
    """Una fila de la auditoría de la organización, para leer. En la tabla la clave primaria es
    `(organization_id, occurred_at, id)`, la que pide el particionado; `id` es un UUIDv7 y
    ordena las filas por el momento en que se escribieron."""

    id = uuid7_primary_key()
    occurred_at = models.DateTimeField()
    actor_type = models.CharField(max_length=16)
    actor_id = models.UUIDField(null=True)
    actor_label = models.CharField(max_length=200, null=True)  # noqa: DJ001 (la tabla es SQL)
    action = models.CharField(max_length=100)
    entity_type = models.CharField(max_length=50)
    entity_id = models.UUIDField(null=True)
    entity_label = models.CharField(max_length=200, null=True)  # noqa: DJ001
    changes = models.JSONField()
    metadata = models.JSONField()
    correlation_id = models.CharField(max_length=64, null=True)  # noqa: DJ001
    result = models.CharField(max_length=8)

    objects = TenantManager.from_queryset(_ReadOnly)()  # type: ignore[misc]

    class Meta:
        managed = False
        db_table = "audit_logs"

    save = delete = _refuse
