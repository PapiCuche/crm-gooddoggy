"""Lecturas de contactos, por organización (RLS y `TenantManager`). El permiso y el alcance los
aplica quien las sirve (`HasPermission` y `ScopeFilter`, o `scoped()`)."""

from django.db.models import QuerySet

from apps.contacts.models import Contact


def contacts() -> QuerySet[Contact]:
    """Los contactos no borrados. El orden lo pone la paginación (ADR-016)."""
    return Contact.objects.filter(deleted_at__isnull=True)
