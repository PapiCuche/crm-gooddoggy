"""Lecturas de tareas, por organización (RLS y `TenantManager`). El permiso y el alcance los aplica
quien las sirve (`HasPermission` y `ScopeFilter`)."""

from django.db.models import QuerySet

from apps.tasks.models import Task


def tasks() -> QuerySet[Task]:
    """Las tareas no borradas. El orden lo pone la paginación (ADR-016)."""
    return Task.objects.filter(deleted_at__isnull=True)
