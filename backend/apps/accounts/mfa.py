"""El segundo factor de una cuenta (E01-03, ADR-023): dar de alta un dispositivo TOTP.

`start_enrollment` es lo que hace la ruta que empieza el alta: comprueba la contraseña, crea el
dispositivo sin confirmar y devuelve, una sola vez, su secreto. El secreto se guarda cifrado
(`core.sealed`, ADR-022) y no vuelve a salir.
"""

from dataclasses import dataclass
from datetime import timedelta

from django.db import connection, transaction
from django.http import HttpRequest

from apps.accounts import throttle, totp
from apps.accounts.models import MfaDevice, User
from apps.accounts.services import client_of, rate_limited, unaudited
from apps.audit import platform
from apps.audit.services import Entity, Result
from core import sealed
from core.api.errors import ApiError

PURPOSE = "mfa.totp"  # el propósito de `core.sealed` (ADR-022 §2)
ISSUER = "Good Doggy CRM"  # lo que enseña la aplicación de autenticación; sin dos puntos
PENDING_FOR = timedelta(minutes=10)  # lo que vive un alta sin confirmar (ADR-023 §4)
ALREADY_ENABLED = "MFA_ALREADY_ENABLED"
STARTED = "auth.mfa.enrollment_started"  # ADR-023 §10 dejó el nombre a este work item


class WrongPassword(Exception):
    """La contraseña presentada no es la de la cuenta. La ruta lo dice en su campo."""


@dataclass(frozen=True)
class Enrollment:
    """Lo que se enseña una vez al empezar un alta. Lleva el secreto: no se registra."""

    secret: str
    uri: str
    expires_in: int

    def __repr__(self) -> str:
        return "Enrollment(…)"  # nunca el secreto


def owner(user_id: object) -> str:
    """El dueño de un secreto para `core.sealed`: estable y único (ADR-022 §4)."""
    return f"user:{user_id}"


def start_enrollment(request: HttpRequest, *, password: str) -> Enrollment:
    """Empieza el alta del dispositivo TOTP de la cuenta con sesión (ADR-023 §4).

    Pide la contraseña actual: una sesión sola no ata un factor a la cuenta. Una contraseña
    incorrecta lanza `WrongPassword`, cuenta en el límite de intentos del acceso (por dirección;
    `ApiError` 429 si se alcanza) y deja una fila de auditoría denegada. La correcta devuelve lo
    que contó, como un acceso correcto: empezar altas no bloquea el acceso.

    Con un dispositivo ya confirmado lanza `ApiError` `MFA_ALREADY_ENABLED`, se pueda leer su
    secreto o no (ADR-023 §3). Si no, sustituye el alta sin confirmar que hubiera por una nueva,
    con la fila de la cuenta bloqueada y su fila de auditoría en la misma transacción: sin esa
    fila no hay alta empezada. Sin claves de cifrado (ADR-022 §6) lanza `ImproperlyConfigured`.
    """
    if connection.in_atomic_block:  # la fila de un rechazo se desharía con esa transacción
        raise RuntimeError("start_enrollment no se llama dentro de una transacción")
    user, client = request.user, client_of(request)
    assert isinstance(user, User)  # noqa: S101 — la ruta exige sesión
    attempt = throttle.keys(user.email, client["ip"])
    try:
        started = throttle.admit(attempt)  # antes de mirar la contraseña
    except throttle.Refused as refused:
        raise rate_limited(refused.seconds) from None
    if not user.check_password(password):
        throttle.failed(attempt)
        try:
            platform.record(
                STARTED,
                actor_type=platform.Actor.USER,
                actor_id=user.pk,
                entity=Entity("user", user.pk),
                metadata={"reason": "password"},
                result=Result.DENIED,
                **client,
            )
        except Exception as error:
            unaudited(error, STARTED)
        raise WrongPassword
    # Solo cuentan los fallos. Aquí y no en la transacción del alta (el acceso lo hace en la
    # suya): si esa se deshace (409, 500), la contraseña era la buena y el intento se devuelve.
    throttle.forgive(attempt, started)
    secret = totp.new_secret()
    with transaction.atomic():
        User.objects.select_for_update(no_key=True).get(pk=user.pk)  # un alta a la vez
        devices = MfaDevice.objects.filter(user=user, type=MfaDevice.Type.TOTP)
        if devices.filter(confirmed_at__isnull=False).exists():
            raise ApiError(ALREADY_ENABLED, 409)
        # Solo la que quedara a medias (su secreto ya no vale): aquí nunca se borra una confirmada.
        devices.filter(confirmed_at__isnull=True).delete()
        devices.create(
            user=user,
            type=MfaDevice.Type.TOTP,
            secret=sealed.seal(PURPOSE, owner(user.pk), secret),
        )
        platform.record(  # en la transacción del alta: sin esta fila, no hay alta empezada
            STARTED,
            actor_type=platform.Actor.USER,
            actor_id=user.pk,
            entity=Entity("user", user.pk),
            **client,
        )
    return Enrollment(
        secret=totp.as_text(secret),
        uri=totp.uri(secret, issuer=ISSUER, account=user.email),
        expires_in=int(PENDING_FOR.total_seconds()),
    )
