"""Acceso por sesión (F2-03A; ADR-003 §2, ADR-013 §5, ADR-014 §3).

`login` y `logout` son operaciones de plataforma: ocurren antes de elegir organización, no
abren `tenant_scope` y se auditan en `platform_audit_logs`.
"""

import ipaddress
import logging
import time
from typing import Any
from uuid import UUID

from django.contrib.auth import SESSION_KEY, authenticate
from django.contrib.auth import login as django_login
from django.contrib.auth import logout as django_logout
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import F
from django.http import HttpRequest

from apps.accounts import throttle
from apps.accounts.emails import canonical_email, presented_email
from apps.accounts.models import User
from apps.audit import platform
from apps.audit.services import Entity, Result
from core.api.errors import ApiError
from core.observability import reporting

logger = logging.getLogger(__name__)
INVALID_CREDENTIALS = "INVALID_CREDENTIALS"
RATE_LIMITED = "RATE_LIMITED"
AUTH_AT = "auth_at"  # cuándo se inició la sesión: límite absoluto (ADR-003 §2)
SEEN_AT = "seen_at"  # última renovación de la caducidad por inactividad
EPOCH = "epoch"  # época de sesión del usuario al iniciarla: revocar la deja atrás (D-F2-11)


def _client(request: HttpRequest) -> dict[str, Any]:
    """IP y agente del cliente. La IP es la que resuelve el servidor ASGI desde los proxies de
    confianza (`FORWARDED_ALLOW_IPS`); nunca una cabecera leída aquí."""
    remote = request.META.get("REMOTE_ADDR") or ""
    try:
        ipaddress.ip_address(remote)  # lo mismo que valida la auditoría: una IP rara no la rompe
    except ValueError:
        remote = ""
    return {"ip": remote or None, "user_agent": request.META.get("HTTP_USER_AGENT")}


def _unaudited(error: Exception, action: str) -> None:
    """ADR-013 §5: un evento de acceso que no se pudo auditar va al log y al reporte de errores,
    solo con el nombre de la acción (nunca el identificador). Nada de esto cambia la respuesta."""
    logger.exception("%s sin auditar", action)
    try:
        reporting.reporter().capture_exception(error, {"action": action})
    except Exception:
        logger.exception("%s: no se pudo reportar", action)


def _audit_failure(email: str, client: dict[str, Any]) -> None:
    """El motivo real solo queda en la auditoría; al cliente le llega siempre lo mismo."""
    try:
        try:
            identifier = canonical_email(email)
            account = User.objects.filter(email=identifier).only("id", "is_active").first()
        except ValidationError:
            identifier, account = email, None
        if account is None:
            reason = "unknown_identifier"
        else:
            reason = "wrong_password" if account.is_active else "inactive_user"
        platform.record(
            "auth.login.failed",
            actor_type=platform.Actor.ANONYMOUS,
            identifier=identifier,
            entity=Entity("user", account.pk) if account else None,
            metadata={"reason": reason},
            result=Result.FAILED,
            **client,
        )
    except Exception as error:  # nada de la auditoría cambia la respuesta de un acceso rechazado
        _unaudited(error, "auth.login.failed")


def _rate_limited(seconds: int) -> ApiError:
    """429 `RATE_LIMITED` con `Retry-After`: el mismo para una cuenta real y una inexistente."""
    error = ApiError(RATE_LIMITED, 429)
    error.wait = seconds  # el manejador de errores lo pasa a la cabecera
    return error


def _audit_block(email: str, client: dict[str, Any], blocked: throttle.Blocked) -> None:
    """Una fila cuando empieza un bloqueo, no una por cada intento rechazado después."""
    try:
        platform.record(
            "auth.login.throttled",
            actor_type=platform.Actor.ANONYMOUS,
            identifier=None if blocked.scope == "ip" else presented_email(email),
            metadata={
                "scope": blocked.scope,
                "failures": blocked.failures,
                "seconds": blocked.seconds,
            },
            result=Result.DENIED,
            **client,
        )
    except Exception as error:
        _unaudited(error, "auth.login.throttled")


def login(request: HttpRequest, *, email: str, password: str) -> User:
    """Autentica y abre la sesión. La misma respuesta para un email desconocido, una contraseña
    incorrecta y un usuario desactivado. Si el acceso no se puede auditar, no hay sesión."""
    client = _client(request)
    if client["ip"] is None:  # todos compartirían un contador: lo delata el proxy de confianza
        logger.warning("login sin dirección de cliente: revisar FORWARDED_ALLOW_IPS")
    attempt = throttle.keys(email, client["ip"])
    try:
        started = throttle.admit(attempt)  # antes de mirar las credenciales, exista o no
    except throttle.Refused as refused:
        raise _rate_limited(refused.seconds) from None
    user = authenticate(request, username=email, password=password)
    if user is None:
        throttle.failed(attempt)
        _audit_failure(email, client)
        for blocked in started:
            _audit_block(email, client, blocked)
        raise ApiError(INVALID_CREDENTIALS, 401)
    assert isinstance(user, User)  # noqa: S101 — el único backend es ModelBackend
    with transaction.atomic():
        if SESSION_KEY in request.session:  # ya autenticada: `django_login` conservaría su ID
            request.session.clear()  # vacía: la clave nueva se crea antes de borrar la anterior
        django_login(request, user)  # rota el ID de sesión y el token CSRF
        request.session[AUTH_AT] = request.session[SEEN_AT] = int(time.time())
        request.session[EPOCH] = user.session_epoch
        platform.record(
            "auth.login.succeeded", actor_type=platform.Actor.USER, actor_id=user.pk, **client
        )
        throttle.forgive(attempt, started)
    return user


def revoke_sessions(user_id: UUID) -> None:
    """Deja sin validez todas las sesiones abiertas del usuario (ADR-003 §2, D-F2-11).

    No recorre las sesiones: incrementa la época del usuario, y `SessionLifetimeMiddleware`
    destruye cada sesión de una época anterior cuando vuelve a presentarse. Corre en la
    transacción de quien llama: si esa se deshace, no hay revocación. No comprueba permisos.
    """
    User.objects.filter(pk=user_id).update(session_epoch=F("session_epoch") + 1)


def logout(request: HttpRequest) -> None:
    """Cierra la sesión y después audita: un fallo de auditoría nunca la mantiene viva."""
    user_id, client = request.user.pk, _client(request)
    django_logout(request)  # borra la fila de la sesión
    try:
        platform.record("auth.logout", actor_type=platform.Actor.USER, actor_id=user_id, **client)
    except Exception as error:
        _unaudited(error, "auth.logout")
