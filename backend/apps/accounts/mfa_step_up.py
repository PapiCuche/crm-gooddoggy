"""Volver a verificar el segundo factor (F2-125, ADR-023 §7): con un código válido, la sesión
anota `mfa_at` y cambia de ID. La comprobación de 15 minutos es de `core.api.step_up`.

El código pasa por `second_factor.verify`: cuenta en el tope por cuenta compartido (ADR-023 §5)
y se gasta al aceptarlo, con la cuenta bloqueada. Ni el código ni el secreto se registran."""

import time
from typing import Any

from django.db import connection
from django.http import HttpRequest

from apps.accounts import mfa, second_factor
from apps.accounts.mfa_disable import NOT_ENABLED
from apps.accounts.models import MfaDevice, User
from apps.accounts.services import EPOCH, client_of, rate_limited, unaudited
from apps.audit import platform
from apps.audit.services import Entity, Result
from core.api import step_up
from core.api.errors import NOT_AUTHENTICATED, ApiError

STEP_UP = "auth.mfa.step_up"  # ADR-023 §10 dejó el nombre a este work item


class _Stale(Exception):
    """La sesión que lo pide dejó de valer con la petición en curso."""


def _denied(user: User, client: dict[str, Any]) -> None:
    """La fila de un código que no vale, fuera de toda transacción; si no se puede escribir, la
    respuesta no cambia (ADR-013 §5)."""
    try:
        platform.record(
            STEP_UP,
            actor_type=platform.Actor.USER,
            actor_id=user.pk,
            entity=Entity("user", user.pk),
            metadata={"reason": "code"},
            result=Result.DENIED,
            **client,
        )
    except Exception as error:
        unaudited(error, STEP_UP)


def verify(request: HttpRequest, *, code: str) -> int:
    """Comprueba `code` y, si vale, deja la sesión verificada ahora y con otro ID (ADR-003 §2);
    devuelve los segundos que vale. Audita `auth.mfa.step_up` en la transacción (sin la fila no
    hay verificación). Código que no vale: `mfa.WrongCode` y fila denegada; tope: 429; sin
    dispositivo: 409 `MFA_NOT_ENABLED` (`MFA_ENFORCEMENT` no exime); sesión revocada o cerrada
    mientras tanto: la cierra y 401. No se llama dentro de una transacción."""
    if connection.in_atomic_block:  # la fila de un rechazo se desharía con esa transacción
        raise RuntimeError("mfa_step_up.verify no se llama dentro de una transacción")
    user, client = request.user, client_of(request)
    assert isinstance(user, User)  # noqa: S101 — la ruta exige sesión
    if not MfaDevice.objects.filter(user=user, confirmed_at__isnull=False).exists():
        raise ApiError(NOT_ENABLED, 409)

    def ready(account: User) -> None:
        gone = not request.session.exists(request.session.session_key or "")  # la cerró otra
        if gone or request.session.get(EPOCH, 0) != account.session_epoch:
            raise _Stale

    def accepted(account: User, method: str) -> None:
        platform.record(  # en la transacción: sin esta fila, no hay verificación
            STEP_UP,
            actor_type=platform.Actor.USER,
            actor_id=account.pk,
            entity=Entity("user", account.pk),
            metadata={"method": method},
            **client,
        )
        request.session.cycle_key()  # ADR-003 §2: la elevación rota el ID
        request.session[mfa.MFA_AT] = int(time.time())

    try:
        second_factor.verify(
            user.pk,
            second_factor.typed(code),  # `123 456` vale
            actor=user.pk,
            client=client,
            ready=ready,
            accepted=accepted,
        )
    except mfa.WrongCode:
        _denied(user, client)
        raise
    except second_factor.Capped as capped:
        raise rate_limited(capped.seconds) from None
    except second_factor.NoDevice:
        raise ApiError(NOT_ENABLED, 409) from None  # otra petición lo quitó mientras tanto
    except _Stale, User.DoesNotExist:
        request.session.flush()
        raise ApiError(NOT_AUTHENTICATED, 401) from None
    return step_up.MAX_AGE
