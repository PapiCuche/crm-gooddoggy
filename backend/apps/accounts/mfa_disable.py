"""Quitar el segundo factor propio (F2-109, ADR-023 §8).

Con sesión, la contraseña actual **y** un código válido, de la aplicación de autenticación o de
respaldo: quien solo tiene una sesión robada, o la sesión y la contraseña, no lo quita. Borra
el dispositivo y todos sus códigos de respaldo, audita y revoca las demás sesiones. Desde
entonces la cuenta vuelve a entrar solo con su contraseña.

Quitar el de otra persona no está aquí: ADR-023 §11 sigue abierta (D-F2-15).
"""

import logging
from typing import Any

from django.db import connection
from django.http import HttpRequest

from apps.accounts import mfa, second_factor, throttle
from apps.accounts.models import MfaBackupCode, MfaDevice, User
from apps.accounts.recovery import NOTICE_QUEUED_FOR, enqueue
from apps.accounts.services import (
    EPOCH,
    audit_block,
    client_of,
    rate_limited,
    revoke_sessions,
    unaudited,
)
from apps.audit import platform
from apps.audit.services import Entity, Result
from core import mail
from core.api.errors import NOT_AUTHENTICATED, ApiError
from core.observability import reporting

logger = logging.getLogger(__name__)

DISABLED = "auth.mfa.disabled"  # del catálogo (ADR-023 §10)
NOT_ENABLED = "MFA_NOT_ENABLED"
DISABLED_SUBJECT = "Se quitó el segundo factor de tu cuenta de Good Doggy CRM"
DISABLED_BODY = """Hola:

Se quitó el segundo factor de tu cuenta de Good Doggy CRM: el dispositivo con la aplicación de
autenticación y sus códigos de respaldo ya no valen. Desde ahora la cuenta entra solo con su
contraseña. Las demás sesiones que tenías abiertas se cerraron.

Si fuiste tú y quieres seguir protegida, da de alta un dispositivo otra vez.

Si no fuiste tú, lo hizo alguien que conoce tu contraseña, tenía abierta una sesión tuya y
tenía un código tuyo. Cambia la contraseña con un enlace de recuperación: eso cierra todas tus
sesiones. Después da de alta un segundo factor, y avisa a quien administra tu organización.
"""


class _Stale(Exception):
    """La sesión que lo pide dejó de valer con la petición en curso."""


def _denied(user: User, reason: str, client: dict[str, Any]) -> None:
    """La fila de un rechazo, fuera de toda transacción; si no se puede escribir, la respuesta
    no cambia (ADR-013 §5)."""
    try:
        platform.record(
            DISABLED,
            actor_type=platform.Actor.USER,
            actor_id=user.pk,
            entity=Entity("user", user.pk),
            metadata={"reason": reason},
            result=Result.DENIED,
            **client,
        )
    except Exception as error:
        unaudited(error, DISABLED)


def disable(request: HttpRequest, *, password: str, code: str) -> None:
    """Quita el segundo factor de la cuenta con sesión (ADR-023 §8).

    Primero la contraseña, que cuenta en el límite por dirección del acceso como al empezar un
    alta: si no es la de la cuenta lanza `mfa.WrongPassword`, sin mirar el código. Después el
    código, con `second_factor.verify`: si no vale lanza `mfa.WrongCode` y cuenta en el tope
    por cuenta. Cada rechazo deja su fila denegada. Sin dispositivo confirmado, `ApiError` 409
    `NOT_ENABLED`; alcanzado un límite, 429.

    Con las dos cosas, en una transacción y con la fila de la cuenta bloqueada: borra el
    dispositivo y sus códigos de respaldo, revoca las demás sesiones (la época avanza), audita
    (sin esa fila no se quita nada) y esta sesión adopta la época nueva y cambia de ID. Si su
    época ya no era la de la cuenta, o su fila ya no existe, no quita nada: cierra la sesión y
    lanza `ApiError` 401. Después avisa a la cuenta por correo, de mejor esfuerzo.
    """
    from apps.accounts.tasks import send_mfa_disabled  # noqa: PLC0415 — ciclo de imports

    if connection.in_atomic_block:  # la fila de un rechazo se desharía con esa transacción
        raise RuntimeError("disable no se llama dentro de una transacción")
    user, client = request.user, client_of(request)
    assert isinstance(user, User)  # noqa: S101 — la ruta exige sesión
    if not MfaDevice.objects.filter(user=user, confirmed_at__isnull=False).exists():
        raise ApiError(NOT_ENABLED, 409)
    attempt = throttle.keys(user.email, client["ip"])
    try:
        started = throttle.admit(attempt)  # antes de mirar la contraseña
    except throttle.Refused as refused:
        raise rate_limited(refused.seconds) from None

    def failed(reason: str) -> None:
        throttle.failed(attempt)
        _denied(user, reason, client)
        for blocked in started:
            audit_block(user.email, client, blocked)

    if not user.check_password(password):
        failed("password")
        raise mfa.WrongPassword

    def ready(account: User) -> None:
        gone = not request.session.exists(request.session.session_key or "")  # la cerró otra
        if gone or request.session.get(EPOCH, 0) != account.session_epoch:
            raise _Stale

    def accepted(account: User, method: str) -> None:
        MfaBackupCode.objects.filter(user=account).delete()
        MfaDevice.objects.filter(user=account).delete()
        revoke_sessions(account.pk)  # las demás no vieron este cambio
        platform.record(  # en la transacción: sin esta fila, el factor sigue
            DISABLED,
            actor_type=platform.Actor.USER,
            actor_id=account.pk,
            entity=Entity("user", account.pk),
            metadata={"method": method},
            **client,
        )
        throttle.forgive(attempt, started)
        # Esta sesión sigue: guardaba la época anterior al avance, leída con la fila bloqueada.
        request.session.cycle_key()
        request.session[EPOCH] = account.session_epoch + 1
        request.session.pop(mfa.MFA_AT, None)  # ya no hay segundo factor que haber verificado

    try:
        second_factor.verify(
            user.pk, code, actor=user.pk, client=client, ready=ready, accepted=accepted
        )
    except mfa.WrongCode:
        failed("code")
        raise
    except second_factor.Capped as capped:
        raise rate_limited(capped.seconds) from None
    except second_factor.NoDevice:
        raise ApiError(NOT_ENABLED, 409) from None  # otra petición lo quitó mientras tanto
    except _Stale, User.DoesNotExist:
        request.session.flush()
        raise ApiError(NOT_AUTHENTICATED, 401) from None
    try:  # el aviso es de mejor esfuerzo: el segundo factor ya está quitado
        enqueue(send_mfa_disabled, str(user.pk), NOTICE_QUEUED_FOR)
    except Exception as error:
        logger.exception("aviso de segundo factor quitado sin encolar")
        try:
            reporting.reporter().capture_exception(error, {"action": DISABLED})
        except Exception:
            logger.exception("aviso de segundo factor quitado: no se pudo reportar")


def notify_disabled(user_id: str) -> bool:
    """Avisa por correo a la cuenta `user_id` de que se le quitó el segundo factor (ADR-023 §8).
    Devuelve si envió el aviso; no lo envía en los casos de `mfa.notify_enabled`. Sin códigos."""
    address = mfa.notice_address(user_id)
    if address is None:
        return False
    notice = mail.Message(
        to=address, subject=DISABLED_SUBJECT, body=DISABLED_BODY, purpose="mfa_disabled"
    )
    mail.send(notice)
    return True
