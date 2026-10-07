"""Lecturas de la auditoría de tenant (F2-73)."""

from django.db.models import QuerySet

from apps.audit.models import AuditLog


def entries() -> QuerySet[AuditLog]:
    """Las filas de auditoría de la organización del `tenant_scope` activo. Filtra por
    organización, no por permiso: el permiso lo exige quien las sirve (`HasPermission`)."""
    return AuditLog.objects.all()
