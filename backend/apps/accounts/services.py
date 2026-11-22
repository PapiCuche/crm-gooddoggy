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
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.db import DEFAULT_DB_ALIAS, IntegrityError, transaction
from django.db.models import F
from django.http import HttpRequest

from apps.accounts import throttle
from apps.accounts.emails import canonical_email, presented_email
from apps.accounts.models import MfaDevice, User
from apps.audit import platform
from apps.audit.services import Entity, Result
from core import text
from core.api.errors import ApiError
from core.observability import reporting

logger = logging.getLogger(__name__)
INVALID_CREDENTIALS = "INVALID_CREDENTIALS"
RATE_LIMITED = "RATE_LIMITED"
PASSWORD_CHANGE_REQUIRED = "PASSWORD_CHANGE_REQUIRED"  # noqa: S105 — un código de error
AUTH_AT = "auth_at"  # cuándo se inició la sesión: límite absoluto (ADR-003 §2)
SEEN_AT = "seen_at"  # última renovación de la caducidad por inactividad
EPOCH = "epoch"  # época de sesión del usuario al iniciarla: revocar la deja atrás (D-F2-11)
PENDING = "mfa_pending"  # la marca de acceso a medias (`second_step`): no es una sesión de usuario


def client_of(request: HttpRequest) -> dict[str, Any]:
    """IP y agente del cliente. La IP es la que resuelve el servidor ASGI desde los proxies de
    confianza (`FORWARDED_ALLOW_IPS`); nunca una cabecera leída aquí."""
    remote = request.META.get("REMOTE_ADDR") or ""
    try:
        ipaddress.ip_address(remote)  # lo mismo que valida la auditoría: una IP rara no la rompe
    except ValueError:
        remote = ""
    return {"ip": remote or None, "user_agent": request.META.get("HTTP_USER_AGENT")}


def unaudited(error: Exception, action: str) -> None:
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
        unaudited(error, "auth.login.failed")


def rate_limited(seconds: int) -> ApiError:
    """429 `RATE_LIMITED` con `Retry-After`: el mismo para una cuenta real y una inexistente."""
    error = ApiError(RATE_LIMITED, 429)
    error.wait = seconds  # el manejador de errores lo pasa a la cabecera
    return error


def audit_block(email: str, client: dict[str, Any], blocked: throttle.Blocked) -> None:
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
        unaudited(error, "auth.login.throttled")


def _refuse_operator_password(
    user: User, email: str, client: dict[str, Any], started: list[throttle.Blocked]
) -> None:
    """La contraseña era la correcta, pero la escribió un operador (`add_member`, F2-127): no abre
    sesión ni empieza el segundo paso. La cuenta elige la suya con el enlace de recuperación
    (ADR-021). Lo contado en el límite no se devuelve, y el bloqueo que empiece con este intento
    se audita como el de un fallo. Un fallo de auditoría no cambia el 403."""
    try:
        platform.record(
            "auth.login.password_change_required",
            actor_type=platform.Actor.ANONYMOUS,  # sin sesión, como `auth.login.mfa_required`
            identifier=user.email,
            entity=Entity("user", user.pk),
            result=Result.DENIED,
            **client,
        )
    except Exception as error:
        unaudited(error, "auth.login.password_change_required")
    for blocked in started:
        audit_block(email, client, blocked)
    raise ApiError(PASSWORD_CHANGE_REQUIRED, 403)


def login(request: HttpRequest, *, email: str, password: str) -> User | None:
    """Autentica y abre la sesión. La misma respuesta para un email desconocido, una contraseña
    incorrecta y un usuario desactivado. Si el acceso no se puede auditar, no hay sesión.

    Si la cuenta tiene un dispositivo confirmado, la contraseña correcta **no abre sesión**
    (ADR-023 §5): deja la marca de acceso a medias (`second_step.begin`) y devuelve `None`. La
    sesión la abre el segundo paso, y lo contado aquí en el límite de intentos lo devuelve él.
    Antes, una contraseña que escribió un operador se rechaza con 403 (F2-127).
    """
    client = client_of(request)
    if client["ip"] is None:  # todos compartirían un contador: lo delata el proxy de confianza
        logger.warning("login sin dirección de cliente: revisar FORWARDED_ALLOW_IPS")
    attempt = throttle.keys(email, client["ip"])
    try:
        started = throttle.admit(attempt)  # antes de mirar las credenciales, exista o no
    except throttle.Refused as refused:
        raise rate_limited(refused.seconds) from None
    user = authenticate(request, username=email, password=password)
    if user is None:
        throttle.failed(attempt)
        _audit_failure(email, client)
        for blocked in started:
            audit_block(email, client, blocked)
        raise ApiError(INVALID_CREDENTIALS, 401)
    assert isinstance(user, User)  # noqa: S101 — el único backend es ModelBackend
    if user.password_change_required:
        _refuse_operator_password(user, email, client, started)
    if MfaDevice.objects.filter(user=user, confirmed_at__isnull=False).exists():
        from apps.accounts import second_step  # noqa: PLC0415 — ciclo de imports

        second_step.begin(request, user, attempt, started, client)
        return None
    with transaction.atomic():
        if SESSION_KEY in request.session:  # ya autenticada: `django_login` conservaría su ID
            request.session.clear()  # vacía: la clave nueva se crea antes de borrar la anterior
        django_login(request, user)  # rota el ID de sesión y el token CSRF
        request.session.pop(PENDING, None)  # una marca nunca vive en una sesión autenticada
        request.session[AUTH_AT] = request.session[SEEN_AT] = int(time.time())
        request.session[EPOCH] = user.session_epoch
        platform.record(
            "auth.login.succeeded", actor_type=platform.Actor.USER, actor_id=user.pk, **client
        )
        throttle.forgive(attempt, started)
    return user


def revoke_sessions(user_id: UUID, *, using: str = DEFAULT_DB_ALIAS) -> None:
    """Deja sin validez todas las sesiones abiertas del usuario (ADR-003 §2, D-F2-11).

    No recorre las sesiones: incrementa la época del usuario, y `SessionLifetimeMiddleware`
    destruye cada sesión de una época anterior cuando vuelve a presentarse. Corre en la
    transacción de quien llama, en la conexión `using`: si esa se deshace, no hay revocación.
    No comprueba permisos.
    """
    User.objects.using(using).filter(pk=user_id).update(session_epoch=F("session_epoch") + 1)


class AccountExists(Exception):
    """El correo ya tiene cuenta y no es la de la sesión, o se creó a la vez (ADR-020 §4)."""


class StaffAccount(Exception):
    """El correo es de una cuenta de personal de plataforma: nunca es miembro (ADR-001)."""


NAME_MAX = User._meta.get_field("first_name").max_length or 0


def _person_name(value: str, *, required: bool) -> str:
    """Un nombre o apellido como se guarda (`core.text.line`: NFC, una línea, sin controles ni
    caracteres de dirección ni ocultos) o `ValidationError` con un mensaje fijo, que nunca repite
    el valor. Con alguna letra o cifra que se vea (`core.text.name`): el nombre siempre y el
    apellido si lo lleva (`core.text.optional_name`, F2-131)."""
    try:
        tidy = text.line(value, NAME_MAX)
    except ValueError:
        message = "Escríbelo en una sola línea, sin caracteres de control ni de dirección."
        raise ValidationError(message, code="invalid") from None
    try:
        return text.name(tidy, NAME_MAX) if required else text.optional_name(tidy, NAME_MAX)
    except ValueError:
        if not required:
            message = "Escribe tu apellido con alguna letra o cifra, o déjalo vacío."
            raise ValidationError(message, code="invalid") from None
        raise ValidationError("Escribe tu nombre.", code="required") from None


def invited_account(
    email: str, *, session_user_id: UUID | None, password: str, first_name: str, last_name: str
) -> tuple[UUID, bool]:
    """La cuenta que acepta una invitación a `email` (canónico), como `(id, creada)` (ADR-020 §4).

    Si existe, solo vale la de la sesión, activa, y no se toca (ni se miran los nombres): si no,
    `AccountExists` (también desactivada) o `StaffAccount`. Si no, se crea con el nombre
    (`_person_name`) y una contraseña que pasa los validadores con esos nombres
    (`ValidationError` por campo, con el código de cada validador). Si otra aceptación la creó a
    la vez, `AccountExists`: quien llama deshace su transacción entera. No inicia sesión.
    """
    existing = User.objects.filter(email=email).first()
    if existing is not None:
        if existing.is_platform_staff:
            raise StaffAccount
        if existing.pk != session_user_id or not existing.is_active:
            raise AccountExists
        return existing.pk, False
    names = {"first_name": first_name, "last_name": last_name}
    problems: dict[str, list[ValidationError]] = {}
    for field, value in names.items():
        try:
            names[field] = _person_name(value, required=field == "first_name")
        except ValidationError as invalid:
            problems[field], names[field] = [invalid], ""
    try:
        validate_password(password, User(email=email, **names))
    except ValidationError as weak:
        problems["password"] = weak.error_list  # con el código de cada validador
    if problems:
        raise ValidationError(problems)
    try:
        with transaction.atomic():  # savepoint: la misma cuenta pudo nacer en otra organización
            user = User.objects.create_user(email=email, password=password, **names)
    except IntegrityError:
        raise AccountExists from None
    return user.pk, True


def logout(request: HttpRequest) -> None:
    """Cierra la sesión y después audita: un fallo de auditoría nunca la mantiene viva."""
    user_id, client = request.user.pk, client_of(request)
    django_logout(request)  # borra la fila de la sesión
    try:
        platform.record("auth.logout", actor_type=platform.Actor.USER, actor_id=user_id, **client)
    except Exception as error:
        unaudited(error, "auth.logout")
