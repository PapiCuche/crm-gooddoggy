"""Tareas de `accounts` (autodiscover de Celery)."""

from django.contrib.sessions.models import Session
from django.utils import timezone

from apps.accounts import code_limit, mfa, recovery, second_factor, throttle
from core.tenancy.celery import platform_task


@platform_task(name="accounts.purge_expired_sessions")
def purge_expired_sessions() -> int:
    """Borra las sesiones caducadas (D-F2-2): Django no las elimina por sí solo."""
    deleted, _ = Session.objects.filter(expire_date__lt=timezone.now()).delete()
    return deleted


@platform_task(name="accounts.purge_login_throttles")
def purge_login_throttles() -> int:
    """Borra los contadores de acceso que ya no cuentan (F2-03B)."""
    # Y los de pedir un enlace (F2-88) y los intentos de código del segundo factor (F2-102).
    return throttle.purge() + recovery.purge_limits() + code_limit.purge()


# Sin reintentos y sin `acks_late`: cada ejecución puede emitir un enlace, y un enlace emitido
# cuenta para el tope de su cuenta (ADR-021 §3). Sin resultado: no hay nada que guardar.
@platform_task(name="accounts.send_password_reset", ignore_result=True, max_retries=0)
def send_password_reset(email: str) -> None:
    """Emite y envía el enlace de recuperación de la cuenta de `email`, si procede (ADR-021).
    `email` llega en su forma canónica. Si la entrega falla, la tarea falla y no reintenta."""
    recovery.issue_link(email)


# Sin reintentos, como la que envía el enlace: si la entrega falla, la tarea falla. Su argumento
# es el identificador de la cuenta, no su correo.
@platform_task(name="accounts.send_password_changed", ignore_result=True, max_retries=0)
def send_password_changed(user_id: str) -> None:
    """Avisa a la cuenta `user_id` de que su contraseña se cambió con un enlace (F2-93)."""
    recovery.notify_change(user_id)


@platform_task(name="accounts.reseal_mfa_secrets")
def reseal_mfa_secrets() -> dict[str, int]:
    """Vuelve a cifrar con la clave vigente los secretos TOTP que quedaron con otra (F2-98,
    ADR-022 §5). Devuelve la versión de la clave vigente en este proceso y cuántos volvió a
    cifrar, cuántos no pudo abrir y cuántos dejó."""
    return mfa.reseal()


# Sin reintentos, como el aviso del cambio de contraseña. Su argumento es el identificador de la
# cuenta, no su correo.
@platform_task(name="accounts.send_mfa_enabled", ignore_result=True, max_retries=0)
def send_mfa_enabled(user_id: str) -> None:
    """Avisa a la cuenta `user_id` de que se le dio de alta un segundo factor (F2-100)."""
    mfa.notify_enabled(user_id)


# Sin reintentos, como los otros avisos. Su argumento es el identificador de la cuenta.
@platform_task(name="accounts.send_mfa_code_limit", ignore_result=True, max_retries=0)
def send_mfa_code_limit(user_id: str) -> None:
    """Avisa a la cuenta `user_id` de que alcanzó el tope de códigos fallidos (F2-104)."""
    second_factor.notify_code_limit(user_id)
