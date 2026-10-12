"""El segundo paso del acceso (F2-105, ADR-023 §5): completar con un código un acceso que la
contraseña dejó a medias.

Entre la contraseña y el código no hay sesión: hay una **marca de acceso a medias** en la sesión
del navegador (`PENDING`), con la cuenta, su época de sesión, la hora y lo que hace falta para
devolver al límite por dirección el intento de la contraseña. No autentica nada: `request.user`
sigue siendo anónimo y toda ruta que pide sesión responde 401. Deja de valer a los `PENDING_FOR`
segundos, si la época de la cuenta cambió (D-F2-11) o si la cuenta ya no está activa.

`begin` es lo que hace el acceso con la contraseña correcta de una cuenta con dispositivo
confirmado: audita, cierra la sesión que hubiera y deja la marca (`mark`). `complete` es lo que
hace la ruta del segundo paso: con la marca y un código que vale (TOTP o de respaldo) abre la
sesión, como el acceso.
"""

import re
import time
from typing import Any
from uuid import UUID

from django.contrib.auth import SESSION_KEY
from django.contrib.auth import login as django_login
from django.db import transaction
from django.http import HttpRequest

from apps.accounts import mfa, second_factor, throttle, totp
from apps.accounts.models import MfaDevice, User
from apps.accounts.services import (
    AUTH_AT,
    EPOCH,
    SEEN_AT,
    audit_block,
    client_of,
    rate_limited,
    unaudited,
)
from apps.accounts.services import PENDING as PENDING  # la marca: también se lee desde aquí
from apps.audit import platform
from apps.audit.services import Entity, Result
from core.api.errors import ApiError

PENDING_FOR = 5 * 60  # segundos que vale la marca (ADR-023 §5)
NOT_STARTED = "MFA_LOGIN_NOT_STARTED"
REQUIRED = "auth.login.mfa_required"  # ADR-023 §10 dejó el nombre a su work item
BACKEND = "django.contrib.auth.backends.ModelBackend"
_APART = re.compile(r"[\s-]")  # `123 456`, como lo enseñan algunas aplicaciones


class _Stale(Exception):
    """La marca dejó de valer con la petición en curso: la época de la cuenta cambió."""


def mark(
    request: HttpRequest, user: User, attempt: dict[str, str], started: list[throttle.Blocked]
) -> None:
    """Deja la marca de acceso a medias de `user` en la sesión de `request`. `attempt` y
    `started` son los del límite por dirección al comprobar la contraseña (`throttle.keys` y
    lo que devolvió `admit`): lo contado se devuelve al completar el segundo paso, no antes
    (ADR-023 §5). Los bloqueos que empezaron con esa contraseña, que era la correcta, acaban
    aquí, como en el acceso: si siguieran, rechazarían el segundo paso de quien acertó.
    Quien llama ya cerró la sesión que hubiera y cambió su ID."""
    request.session[PENDING] = {
        "user": str(user.pk),
        "epoch": user.session_epoch,
        "at": int(time.time()),
        "keys": dict(attempt),
    }
    throttle.release(attempt, started)


def begin(
    request: HttpRequest,
    user: User,
    attempt: dict[str, str],
    started: list[throttle.Blocked],
    client: dict[str, Any],
) -> None:
    """La contraseña de `user` era la correcta y la cuenta tiene segundo factor: en vez de abrir
    sesión, deja el acceso a medias (ADR-023 §5).

    En una transacción: la fila de auditoría de esa contraseña (actor anónimo, porque no hay
    sesión, y la cuenta como entidad afectada; sin esa fila no hay marca, ADR-023 §10), el
    cierre de la sesión que el navegador tuviera, de esa cuenta o de otra, y la marca, que
    queda bajo un ID de sesión nuevo. Lo contado en el límite por dirección no se devuelve.
    """
    with transaction.atomic():
        platform.record(
            REQUIRED,
            actor_type=platform.Actor.ANONYMOUS,
            identifier=user.email,
            entity=Entity("user", user.pk),
            **client,
        )
        request.session.flush()  # borra la fila anterior; al responder se guarda con otro ID
        mark(request, user, attempt, started)


def _refuse(request: HttpRequest) -> ApiError:
    """No hay acceso a medias que completar: la marca, si la había, se borra."""
    request.session.pop(PENDING, None)
    return ApiError(NOT_STARTED, 401)


def _pending(request: HttpRequest) -> dict[str, Any]:
    """La marca de la sesión, si está entera y no caducó; si no, `ApiError` 401."""
    found: Any = request.session.get(PENDING)
    try:
        age = time.time() - found["at"]
        whole = (
            isinstance(found["epoch"], int)
            and set(found["keys"]) == set(throttle.SCOPES)
            and all(isinstance(key, str) for key in found["keys"].values())
        )
        UUID(found["user"])
    except KeyError, TypeError, ValueError, AttributeError, OverflowError:
        raise _refuse(request) from None
    if not whole or not 0 <= age < PENDING_FOR:
        raise _refuse(request)
    return dict(found)


def _failed(account: User, client: dict[str, Any]) -> None:
    """La fila de un código que no vale, fuera de toda transacción; si no se puede escribir, la
    respuesta no cambia (ADR-023 §10, ADR-013 §5)."""
    try:
        platform.record(
            "auth.login.failed",
            actor_type=platform.Actor.ANONYMOUS,
            identifier=account.email,
            entity=Entity("user", account.pk),
            metadata={"reason": "wrong_code"},
            result=Result.FAILED,
            **client,
        )
    except Exception as error:
        unaudited(error, "auth.login.failed")


def complete(request: HttpRequest, *, code: str) -> User:
    """Completa el acceso a medias de la sesión con `code` y abre la sesión (ADR-023 §5).

    Sin marca, con una caducada, o si la cuenta ya no está activa, cambió de época o no tiene
    dispositivo confirmado, lanza `ApiError` 401 `NOT_STARTED` y borra la marca, sin mirar el
    código. Lo comprueba antes de contar nada y otra vez con la fila de la cuenta bloqueada;
    ahí mira además que la sesión de la marca siga existiendo: una marca abre una sola sesión,
    aunque lleguen a la vez dos peticiones con ella y dos códigos que valen.

    El intento cuenta en el límite por dirección del acceso y en el tope por cuenta
    (`second_factor.verify`); alcanzado cualquiera de los dos, `ApiError` 429. Un código que no
    vale lanza `mfa.WrongCode`, deja la fila `auth.login.failed` y no borra la marca.

    Con un código que vale, en una transacción: la fila `auth.login.succeeded` con su método
    (sin ella no hay sesión), los dos intentos del límite por dirección devueltos (este y el de
    la contraseña) y la sesión abierta como en el acceso, con otro ID, la época de la cuenta y
    cuándo se verificó el segundo factor.
    """
    client = client_of(request)
    pending = _pending(request)
    account = User.objects.filter(pk=pending["user"], is_active=True).first()
    devices = MfaDevice.objects.filter(user=account, confirmed_at__isnull=False)
    if account is None or account.session_epoch != pending["epoch"] or not devices.exists():
        raise _refuse(request)
    attempt = throttle.keys(account.email, client["ip"])
    try:
        started = throttle.admit(attempt)  # antes de mirar el código
    except throttle.Refused as refused:
        raise rate_limited(refused.seconds) from None

    def ready(locked: User) -> None:
        if not request.session.exists(request.session.session_key or ""):
            # Otra petición completó este acceso (o cerró la sesión) mientras tanto: una marca
            # abre una sola sesión. La fila ya no existe: la sesión no se toca.
            raise ApiError(NOT_STARTED, 401)
        if locked.session_epoch != pending["epoch"]:  # una revocación llegó mientras tanto
            raise _Stale

    def accepted(locked: User, method: str) -> User:
        platform.record(  # en la transacción: sin esta fila, no hay sesión (ADR-013 §5)
            "auth.login.succeeded",
            actor_type=platform.Actor.USER,
            actor_id=locked.pk,
            metadata={"method": method},
            **client,
        )
        throttle.forgive(attempt, started)
        throttle.forgive(pending["keys"], [])  # el de la contraseña, que esperaba a este
        if SESSION_KEY in request.session:  # nunca debería: la marca no vive en una autenticada
            request.session.clear()
        django_login(request, locked, backend=BACKEND)  # rota el ID de sesión y el token CSRF
        now = int(time.time())
        request.session[AUTH_AT] = request.session[SEEN_AT] = request.session[mfa.MFA_AT] = now
        request.session[EPOCH] = locked.session_epoch
        request.session.pop(PENDING, None)
        return locked

    joined = _APART.sub("", code)
    shaped = len(joined) == totp.DIGITS and joined.isascii() and joined.isdigit()
    try:
        return second_factor.verify(
            account.pk,
            joined if shaped else code,
            actor=None,  # todavía no hay sesión
            client=client,
            ready=ready,
            accepted=accepted,
        )
    except mfa.WrongCode:
        throttle.failed(attempt)
        _failed(account, client)
        for blocked in started:
            audit_block(account.email, client, blocked)
        raise
    except second_factor.Capped as capped:
        raise rate_limited(capped.seconds) from None
    except _Stale, second_factor.NoDevice, User.DoesNotExist:
        raise _refuse(request) from None
