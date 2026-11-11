"""Lecturas de cotizaciones, por organización (RLS y `TenantManager`). El permiso y el alcance los
aplica quien las sirve (`HasPermission` y `ScopeFilter`)."""

from django.db.models import QuerySet

from apps.quotes.models import Quote


def quotes() -> QuerySet[Quote]:
    """Las cotizaciones (no se borran: se cancelan). El orden lo pone la paginación (ADR-016)."""
    return Quote.objects.all()
