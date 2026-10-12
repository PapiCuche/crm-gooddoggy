"""El segundo factor de una cuenta (E01-03, ADR-023): dar de alta un dispositivo TOTP.

`start_enrollment` es lo que hace la ruta que empieza el alta: comprueba la contraseña, crea el
dispositivo sin confirmar y devuelve, una sola vez, su secreto. El secreto se guarda cifrado
(`core.sealed`, ADR-022) y no vuelve a salir. `confirm_enrollment` es lo que hace la ruta que
la confirma: con un código del dispositivo, la cuenta pasa a tener segundo factor y códigos de
respaldo. `reseal` es lo que hace la tarea `accounts.reseal_mfa_secrets`: tras una rotación de
clave, vuelve a cifrar con la vigente.
"""

import logging
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import UUID

from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.http import HttpRequest
from django.utils import timezone

from apps.accounts import backup_codes, throttle, totp
from apps.accounts.models import ENROLLMENT_FAILURES, MfaBackupCode, MfaDevice, User
from apps.accounts.recovery import NOTICE_QUEUED_FOR, enqueue
from apps.accounts.services import EPOCH, client_of, rate_limited, revoke_sessions, unaudited
from apps.audit import platform
from apps.audit.services import Entity, Result
from core import mail, sealed
from core.api.errors import NOT_AUTHENTICATED, ApiError
from core.observability import reporting

logger = logging.getLogger(__name__)

PURPOSE = "mfa.totp"  # el propósito de `core.sealed` (ADR-022 §2)
ISSUER = "Good Doggy CRM"  # lo que enseña la aplicación de autenticación; sin dos puntos
PENDING_FOR = timedelta(minutes=10)  # lo que vive un alta sin confirmar (ADR-023 §4)
ALREADY_ENABLED = "MFA_ALREADY_ENABLED"
STARTED = "auth.mfa.enrollment_started"  # ADR-023 §10 dejó el nombre a este work item
ENABLED = "auth.mfa.enabled"
NOT_STARTED = "MFA_ENROLLMENT_NOT_STARTED"
MFA_AT = "mfa_at"  # en la sesión: cuándo se verificó el segundo factor (ADR-003 §4, step-up)
ENABLED_SUBJECT = "Tu cuenta de Good Doggy CRM tiene un segundo factor"
ENABLED_BODY = """Hola:

Se dio de alta un segundo factor en tu cuenta de Good Doggy CRM: un dispositivo con una
aplicación de autenticación, y sus códigos de respaldo. Las demás sesiones que tenías abiertas
se cerraron.

Si fuiste tú, guarda los códigos de respaldo en un lugar seguro: son la forma de entrar si
pierdes el dispositivo.

Si no fuiste tú, lo hizo alguien que conoce tu contraseña y tenía abierta una sesión tuya.
Cambia la contraseña con un enlace de recuperación: eso cierra todas tus sesiones. Y avisa
cuanto antes a quien administra tu organización, te llegue o no ese enlace:
cambiar la contraseña no quita ese segundo factor.
"""


class WrongPassword(Exception):
    """La contraseña presentada no es la de la cuenta. La ruta lo dice en su campo."""


class WrongCode(Exception):
    """El código presentado no vale. La ruta lo dice en su campo, sin decir por qué."""


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


def _denied(action: str, user_id: UUID, reason: str, client: dict[str, Any]) -> None:
    """La fila de un rechazo, fuera de toda transacción; si no se puede escribir, la respuesta
    no cambia (ADR-013 §5)."""
    try:
        platform.record(
            action,
            actor_type=platform.Actor.USER,
            actor_id=user_id,
            entity=Entity("user", user_id),
            metadata={"reason": reason},
            result=Result.DENIED,
            **client,
        )
    except Exception as error:
        unaudited(error, action)


def confirm_enrollment(request: HttpRequest, *, code: str) -> list[str]:
    """Confirma el alta del dispositivo TOTP de la cuenta con sesión (ADR-023 §4) y devuelve,
    una sola vez, sus códigos de respaldo.

    Vale un código del dispositivo sin confirmar, empezado hace `PENDING_FOR` como mucho. Con
    la fila de la cuenta bloqueada y releída, y después la del dispositivo, en una transacción:
    queda confirmado, con el paso de ese código como último aceptado (no vale después para
    entrar); se guardan los hashes de los códigos de respaldo; las demás sesiones se revocan
    (la época avanza) y esta adopta la época nueva, cambia de ID y anota cuándo se verificó; y
    se audita. Sin esa fila no hay confirmación.

    Si la época de la sesión ya no es la de la cuenta, la cuenta dejó de estar activa o la fila
    de la sesión ya no existe (una revocación, o un cierre de sesión, llegó con la petición en
    curso), no confirma nada: no revive con otro ID una sesión cerrada. Cierra la sesión y lanza
    `ApiError` 401. Sin alta empezada o caducada, `MFA_ENROLLMENT_NOT_STARTED`; ya confirmada,
    `MFA_ALREADY_ENABLED`. Un código que no vale lanza `WrongCode`, cuenta en el límite de
    intentos del acceso (por dirección) y deja una fila denegada; un secreto que no se abre es
    un código que no vale, y se reporta. Cada uno suma además un fallo al alta, y el que hace
    `ENROLLMENT_FAILURES` la borra (ADR-024): la siguiente petición ya no tiene alta empezada.
    Solo un acierto devuelve lo que contó, en esa transacción: un 409 o un 401 lo borrarían sin
    probar nada, también los fallos de contraseña.
    """
    from apps.accounts.tasks import send_mfa_enabled  # noqa: PLC0415 — ciclo de imports

    if connection.in_atomic_block:  # la fila de un rechazo se desharía con esa transacción
        raise RuntimeError("confirm_enrollment no se llama dentro de una transacción")
    user, client = request.user, client_of(request)
    assert isinstance(user, User)  # noqa: S101 — la ruta exige sesión
    attempt = throttle.keys(user.email, client["ip"])
    try:
        started = throttle.admit(attempt)  # antes de mirar el código
    except throttle.Refused as refused:
        raise rate_limited(refused.seconds) from None
    refused_as, reason, unreadable, codes = "", "code", False, []
    with transaction.atomic():
        account = User.objects.select_for_update(no_key=True).get(pk=user.pk)
        pending = MfaDevice.objects.select_for_update().filter(
            user=account, type=MfaDevice.Type.TOTP
        )
        device, now = pending.first(), timezone.now()
        gone = not request.session.exists(request.session.session_key or "")  # la cerró otra
        if gone or not account.is_active or request.session.get(EPOCH, 0) != account.session_epoch:
            refused_as = "session"
        elif device is not None and device.confirmed_at is not None:
            refused_as = ALREADY_ENABLED
        elif device is None or device.created_at <= now - PENDING_FOR:
            refused_as = NOT_STARTED
        else:
            try:
                secret = sealed.unseal(PURPOSE, owner(account.pk), device.secret)
                step = totp.accepted_step(secret, code, now=now.timestamp(), last=None)
            except sealed.SealError:
                step, unreadable = None, True
            if step is None:
                refused_as = "code"
                # El fallo se guarda con la fila bloqueada, venga de la dirección que venga; el
                # que agota el alta la borra: empezar otra pide la contraseña (ADR-024).
                if device.failed_codes + 1 < ENROLLMENT_FAILURES:
                    pending.update(failed_codes=device.failed_codes + 1)
                else:
                    reason = "code_limit"
                    pending.delete()
            else:
                # Juntos: la base lo exige. Y sin fallos anotados (ADR-024).
                pending.update(confirmed_at=now, last_step=step, failed_codes=0)
                codes = backup_codes.new_codes()
                MfaBackupCode.objects.filter(user=account).delete()
                hashes = [backup_codes.code_hash(shown) or "" for shown in codes]
                MfaBackupCode.objects.bulk_create(
                    MfaBackupCode(user=account, code_hash=hashed) for hashed in hashes
                )
                revoke_sessions(account.pk)  # las demás se abrieron sin segundo factor
                platform.record(  # en la transacción: sin esta fila, no hay segundo factor
                    ENABLED,
                    actor_type=platform.Actor.USER,
                    actor_id=account.pk,
                    entity=Entity("user", account.pk),
                    **client,
                )
                throttle.forgive(attempt, started)  # aquí, como el acceso: si falla, nada
                # Esta sesión sigue: guardaba la época anterior al avance, leída con la fila
                # bloqueada (ADR-023 §4). Cambia de ID y anota la verificación (ADR-003 §2).
                request.session.cycle_key()
                request.session[EPOCH] = account.session_epoch + 1
                request.session[MFA_AT] = int(time.time())
    if refused_as == "code":
        throttle.failed(attempt)
        _denied(ENABLED, user.pk, reason, client)
        if unreadable:  # nadie sabría que falta una clave: la respuesta no lo distingue
            lost = UnreadableSecrets(1)
            try:
                reporting.reporter().capture_exception(lost, {"action": "mfa.confirm"})
            except Exception:  # tampoco si el reporte falla: sería un 500 solo en este caso
                logger.exception("secreto TOTP ilegible al confirmar: no se pudo reportar")
        raise WrongCode
    if refused_as == "session":
        request.session.flush()
        raise ApiError(NOT_AUTHENTICATED, 401)
    if refused_as:
        raise ApiError(refused_as, 409)
    try:  # el aviso es de mejor esfuerzo: el segundo factor ya está dado de alta
        enqueue(send_mfa_enabled, str(user.pk), NOTICE_QUEUED_FOR)
    except Exception as error:
        logger.exception("aviso de segundo factor sin encolar")
        try:
            reporting.reporter().capture_exception(error, {"action": ENABLED})
        except Exception:
            logger.exception("aviso de segundo factor: no se pudo reportar")
    return codes


def notice_address(user_id: str) -> str | None:
    """A dónde se avisa a la cuenta `user_id`: su correo, o `None` si la cuenta no existe, está
    desactivada o `core.mail` no podría escribir a su dirección."""
    try:
        found = User.objects.filter(pk=user_id, is_active=True).values_list("email", flat=True)
        address: str | None = found.first()
    except ValueError, ValidationError:  # no es un identificador de cuenta
        return None
    return address if address is not None and mail.deliverable(address) else None


def notify_enabled(user_id: str) -> bool:
    """Avisa por correo a la cuenta `user_id` de que se le dio de alta un segundo factor
    (ADR-023 §4). Devuelve si envió el aviso. No envía nada si la cuenta no existe, está
    desactivada o `core.mail` no podría escribir a su dirección. Sin secretos ni códigos."""
    address = notice_address(user_id)
    if address is None:
        return False
    notice = mail.Message(
        to=address, subject=ENABLED_SUBJECT, body=ENABLED_BODY, purpose="mfa_enabled"
    )
    mail.send(notice)
    return True


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
