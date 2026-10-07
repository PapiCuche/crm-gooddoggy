"""Tareas de `accounts` (autodiscover de Celery)."""

from django.contrib.sessions.models import Session
from django.utils import timezone

from apps.accounts import recovery, throttle
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


# Sin reintentos y sin `acks_late`: cada ejecución puede emitir un enlace, y un enlace emitido
# cuenta para el tope de su cuenta (ADR-021 §3). Sin resultado: no hay nada que guardar.
@platform_task(name="accounts.send_password_reset", ignore_result=True, max_retries=0)
def send_password_reset(email: str) -> None:
    """Emite y envía el enlace de recuperación de la cuenta de `email`, si procede (ADR-021).
    `email` llega en su forma canónica. Si la entrega falla, la tarea falla y no reintenta."""
    recovery.issue_link(email)
