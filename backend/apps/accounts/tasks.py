"""Tareas de `accounts` (autodiscover de Celery)."""

from django.contrib.sessions.models import Session
from django.utils import timezone

from apps.accounts import throttle
from core.tenancy.celery import platform_task


@platform_task(name="accounts.purge_expired_sessions")
def purge_expired_sessions() -> int:
    """Borra las sesiones caducadas (D-F2-2): Django no las elimina por sí solo."""
    deleted, _ = Session.objects.filter(expire_date__lt=timezone.now()).delete()
    return deleted


@platform_task(name="accounts.purge_login_throttles")
def purge_login_throttles() -> int:
    """Borra los contadores de acceso que ya no cuentan (F2-03B)."""
    return throttle.purge()
