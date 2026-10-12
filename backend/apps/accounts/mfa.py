"""El segundo factor de una cuenta (E01-03, ADR-023): dar de alta un dispositivo TOTP.

`start_enrollment` es lo que hace la ruta que empieza el alta: comprueba la contraseña, crea el
dispositivo sin confirmar y devuelve, una sola vez, su secreto. El secreto se guarda cifrado
(`core.sealed`, ADR-022) y no vuelve a salir. `reseal` es lo que hace la tarea
`accounts.reseal_mfa_secrets`: tras una rotación de clave, vuelve a cifrar con la vigente.
"""

import logging
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
from core.observability import reporting

logger = logging.getLogger(__name__)

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


class UnreadableSecrets(Exception):
    """Hay secretos guardados que no se pueden abrir. Solo lleva cuántos."""


def reseal() -> dict[str, int]:
    """Vuelve a cifrar con la clave vigente cada secreto TOTP que quedó cifrado con otra
    (ADR-022 §5, paso 2). Devuelve cuántos volvió a cifrar (`resealed`), cuántos no pudo abrir
    (`unreadable`) y cuántos dejó para otra pasada porque otra operación los tenía (`busy`),
    con la versión de la clave vigente en este proceso (`key`); lo deja también en el log. La
    clave anterior se puede retirar cuando una pasada cuya `key` es la nueva da cero en los
    tres y todos los procesos corren ya con la nueva (ADR-022 §5; esto no lo comprueba nada):
    uno que aún no la tiene da cero sin haber vuelto a cifrar nada, y cifra con la anterior.

    Cada fila pendiente va en su transacción, bloqueada: solo cambia `secret`. Lo que no abre (su
    clave ya no está, o el valor no es de esa cuenta) **no se toca**: en un proceso que aún no
    tiene la clave nueva, lo cifrado con ella también se ve como pendiente y tampoco se abre. Si
    hubo alguno, queda en el log y en el reporte de errores, sin el valor ni la cuenta.

    No distingue un valor legítimo de uno fabricado con una clave comprometida que siga en el
    ajuste: volver a cifrar no es una limpieza (ADR-022 §5). Sin claves lanza
    `ImproperlyConfigured`.
    """
    done = {"key": sealed.current(), "resealed": 0, "unreadable": 0, "busy": 0}  # sin clave, lanza
    listed = MfaDevice.objects.order_by("pk").values_list("pk", "user_id", "secret")
    # Leer no bloquea nada: solo lo que parece pendiente se bloquea y se vuelve a mirar.
    for pk, user_id in [row[:2] for row in listed if sealed.stale(row[2])]:
        with transaction.atomic():
            rows = MfaDevice.objects.select_for_update(skip_locked=True).filter(pk=pk)
            device = rows.first()
            if device is None:  # la borró otra operación, o la tiene bloqueada
                done["busy"] += int(MfaDevice.objects.filter(pk=pk).exists())
                continue
            if not sealed.stale(device.secret):
                continue
            try:
                secret = sealed.unseal(PURPOSE, owner(user_id), device.secret)
            except sealed.SealError:
                done["unreadable"] += 1
                continue
            sealed_again = sealed.seal(PURPOSE, owner(user_id), secret)
            MfaDevice.objects.filter(pk=pk).update(secret=sealed_again)
            done["resealed"] += 1
    logger.info(
        "secretos TOTP: clave %d, vueltos a cifrar %d, ilegibles %d, ocupados %d",
        done["key"],
        done["resealed"],
        done["unreadable"],
        done["busy"],
    )
    if done["unreadable"]:
        error = UnreadableSecrets(done["unreadable"])
        logger.error("secretos TOTP que no se pueden abrir: %d", done["unreadable"])
        try:
            reporting.reporter().capture_exception(error, {"action": "mfa.reseal"})
        except Exception:
            logger.exception("secretos TOTP ilegibles: no se pudo reportar")
    return done
