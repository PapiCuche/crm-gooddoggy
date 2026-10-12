"""Dar por bueno un código del segundo factor de una cuenta (F2-103, ADR-023 §2, §5 y §6).

`accept` decide si lo presentado vale contra el dispositivo confirmado de la cuenta, sea el
código de su aplicación de autenticación o uno de respaldo, y **lo gasta** al aceptarlo: el
paso de un código TOTP queda como el último aceptado, y un código de respaldo, usado. Lo
comparten todas las rutas que comprueban un código de un dispositivo confirmado: el segundo
paso del acceso, el step-up, quitar el factor y regenerar los códigos de respaldo.

`accept` no cuenta intentos, no audita y no abre sesión. Las rutas no lo llaman a mano: usan
`verify`, que lleva el orden entero (F2-104): cuenta el intento con `code_limit` antes, bloquea
la cuenta y después el dispositivo, devuelve el intento solo si el código valió, y audita y
avisa cuando se alcanza el tope (ADR-023 §5 y §10). No dice por qué un código no vale. Nada de
aquí registra un código ni un secreto.
"""

import logging
from collections.abc import Callable
from typing import Any
from uuid import UUID

from django.conf import settings
from django.db import connection, transaction
from django.utils import timezone

from apps.accounts import backup_codes, code_limit, mfa, totp
from apps.accounts.models import MfaBackupCode, MfaDevice, User
from apps.accounts.recovery import NOTICE_QUEUED_FOR, enqueue
from apps.accounts.services import unaudited
from apps.audit import platform
from apps.audit.services import Entity, Result
from core import mail, sealed
from core.observability import reporting

logger = logging.getLogger(__name__)

TOTP, BACKUP_CODE = "totp", "backup_code"  # el método, como lo audita el acceso (ADR-023 §5)
THROTTLED = "auth.mfa.throttled"  # ADR-023 §10 dejó el nombre a este work item
LIMIT_SUBJECT = "Tu cuenta de Good Doggy CRM no acepta códigos del segundo factor por ahora"
LIMIT_BODY = """Hola:

Se fallaron demasiados códigos del segundo factor de tu cuenta de Good Doggy CRM. Para que
nadie acierte uno a base de probar, tu cuenta deja de aceptar códigos durante un tiempo,
también los correctos: como mucho {hours} horas. Después vuelve a aceptarlos sola.

Si fuiste tú, espera y vuelve a intentarlo con el código que muestre entonces tu aplicación
de autenticación.

Si no fuiste tú, quien lo hizo conoce tu contraseña o tiene abierta una sesión tuya. Cambia
la contraseña con un enlace de recuperación: eso cierra todas tus sesiones, y quien tenía la
anterior ya no llega a probar códigos. Cambiarla no adelanta el final de la espera. Y avisa
a quien administra tu organización, te llegue o no ese enlace.
"""


class Capped(Exception):
    """La cuenta alcanzó el tope de códigos fallidos: no se mira ninguno, tampoco el correcto."""

    def __init__(self, seconds: int) -> None:
        super().__init__(seconds)
        self.seconds = seconds


class NoDevice(Exception):
    """La cuenta no tiene un dispositivo confirmado: no hay segundo factor que comprobar."""


def _unreadable() -> None:
    """Nadie sabría que falta una clave: la respuesta no lo distingue de un código que no vale."""
    lost = mfa.UnreadableSecrets(1)
    try:
        reporting.reporter().capture_exception(lost, {"action": "mfa.accept"})
    except Exception:  # tampoco si el reporte falla: sería un 500 solo en este caso
        logger.exception("secreto TOTP ilegible al comprobar un código: no se pudo reportar")


def accept(account: User, presented: str) -> str | None:
    """El método con el que `presented` vale como segundo factor de `account` (`TOTP` o
    `BACKUP_CODE`), ya gastado; `None` si no vale, sin decir por qué.

    Corre en la transacción de quien llama y bloquea la fila del dispositivo: dos peticiones
    con el mismo código no pasan las dos, y si esa transacción se deshace, el código no queda
    gastado. Fuera de una transacción se niega. Quien llama bloquea **antes** la fila de la
    cuenta (`FOR NO KEY UPDATE`), como `mfa.confirm_enrollment`: la cuenta y después el
    dispositivo. Quien tocara la cuenta después de `accept` (revocar las sesiones al quitar el
    factor) se cruzaría con quien lleva ese orden, y la base deshace a una de las dos.

    Seis dígitos son un código TOTP: vale el de un paso de la ventana posterior al último
    aceptado (ADR-023 §2). Un secreto que no se abre es un código que no vale, y se reporta
    (ADR-023 §3). Cualquier otra cosa se mira como código de respaldo de esa cuenta, escrito
    como lo teclee una persona; vale si no está gastado (ADR-023 §6), también con el secreto
    ilegible. Lo presentado llega ya sin espacios alrededor.

    `NoDevice` si la cuenta no tiene un dispositivo confirmado. Quien llama lo comprueba antes
    de contar el intento y aun así lo trata: el factor se pudo quitar entre esa comprobación y
    esta llamada, y ese intento queda contado. Nunca es un acierto: con él no se abre sesión
    ni se da nada por verificado.

    Devuelve `BACKUP_CODE` también a la ruta que solo admite un código TOTP (regenerar los
    códigos, ADR-023 §6), y ya gastado: si esa ruta lo rechaza sin deshacer su transacción, el
    código de respaldo se pierde sin haber servido.
    """
    if not connection.in_atomic_block:  # gastar el código y lo que lo acepta van juntos
        raise RuntimeError("second_factor.accept se llama dentro de una transacción")
    confirmed = MfaDevice.objects.select_for_update().filter(
        user=account, type=MfaDevice.Type.TOTP, confirmed_at__isnull=False
    )
    device = confirmed.first()
    if device is None:
        raise NoDevice
    now = timezone.now()
    if len(presented) == totp.DIGITS and presented.isascii() and presented.isdigit():
        try:
            secret = sealed.unseal(mfa.PURPOSE, mfa.owner(account.pk), device.secret)
        except sealed.SealError:
            _unreadable()
            return None
        step = totp.accepted_step(secret, presented, now=now.timestamp(), last=device.last_step)
        if step is None:
            return None
        confirmed.update(last_step=step)  # ese paso, y los anteriores, ya no valen
        return TOTP
    hashed = backup_codes.code_hash(presented)
    if hashed is None:
        return None
    unspent = MfaBackupCode.objects.filter(user=account, code_hash=hashed, used_at__isnull=True)
    return BACKUP_CODE if unspent.update(used_at=now) else None


def _reached(user_id: UUID, actor: UUID | None, client: dict[str, Any]) -> None:
    """El intento que alcanza el tope quedó contado (ADR-023 §5 y §10): su fila, como la del
    inicio de un bloqueo del acceso, y el aviso a la cuenta. Las dos cosas de mejor esfuerzo."""
    from apps.accounts.tasks import send_mfa_code_limit  # noqa: PLC0415 — ciclo de imports

    limit, window = settings.MFA_CODE_THROTTLE
    try:
        platform.record(
            THROTTLED,
            actor_type=platform.Actor.ANONYMOUS if actor is None else platform.Actor.USER,
            actor_id=actor,
            entity=Entity("user", user_id),
            metadata={"attempts": limit, "seconds": window},
            result=Result.DENIED,
            **client,
        )
    except Exception as error:
        unaudited(error, THROTTLED)
    try:
        enqueue(send_mfa_code_limit, str(user_id), NOTICE_QUEUED_FOR)
    except Exception as error:
        logger.exception("aviso del tope de códigos sin encolar")
        try:
            reporting.reporter().capture_exception(error, {"action": THROTTLED})
        except Exception:
            logger.exception("aviso del tope de códigos: no se pudo reportar")


def verify[T](
    user_id: UUID,
    presented: str,
    *,
    actor: UUID | None,
    client: dict[str, Any],
    ready: Callable[[User], None],
    accepted: Callable[[User, str], T],
) -> T:
    """Comprueba `presented` como segundo factor de la cuenta `user_id` y, si vale, ejecuta lo
    de la ruta. Es el procedimiento de toda ruta que comprueba un código de un dispositivo
    confirmado (ADR-023 §5); se llama fuera de toda transacción.

    1. Cuenta el intento (`code_limit.admit`). Con el tope alcanzado lanza `Capped`, con los
       segundos que faltan, sin mirar el código.
    2. En una transacción, bloquea la fila de la cuenta y la relee, y llama a `ready(account)`:
       ahí la ruta comprueba, bajo el bloqueo, lo que no es el código (la marca de acceso a
       medias y su época; la sesión; la contraseña). Si `ready` lanza, no se probó ningún
       código: el intento se devuelve. Una cuenta que ya no está activa no llega a `ready`:
       lanza `User.DoesNotExist`, como una que no existe, y el intento se devuelve.
    3. `accept`. Si el código no vale lanza `mfa.WrongCode` y el intento queda contado.
    4. Si vale, `accepted(account, method)` hace lo de la ruta en esa transacción (abrir la
       sesión, auditar) y el intento se devuelve con ella. Si `accepted` lanza, todo se
       deshace: el código no queda gastado y el intento queda contado, como en el acceso.

    Cuando el intento que queda contado es el que alcanza el tope, deja la fila `THROTTLED`
    (actor `actor`, o anónimo si es `None`: en el segundo paso del acceso no hay sesión) y
    encola el aviso a la cuenta; si alguna de las dos cosas falla, nada cambia para quien
    llama. La ruta trata `NoDevice`, que sale tal cual y con el intento contado, y
    `User.DoesNotExist`, con el intento devuelto. Antes de llamar ya comprobó sin bloqueo lo
    mismo que `ready`: `admit` cuenta sin preguntar quién llama.
    """
    try:
        attempt = code_limit.admit(user_id)
    except code_limit.Refused as refused:
        raise Capped(refused.seconds) from None
    looked = False
    try:
        with transaction.atomic():
            # Activa, o nada: una cuenta desactivada no comprueba códigos (ADR-023 §5).
            account = User.objects.select_for_update(no_key=True).get(pk=user_id, is_active=True)
            ready(account)
            looked = True  # desde aquí el intento cuenta, salvo que el código valga
            method = accept(account, presented)
            if method is None:
                raise mfa.WrongCode
            result = accepted(account, method)
            code_limit.give_back(attempt)  # aquí, como el acceso: si falla, nada
            return result
    except BaseException:
        kept = looked
        if not looked:
            try:
                code_limit.give_back(attempt)
            except Exception:  # queda contado de más: el error que importa es el original
                logger.exception("intento de código sin devolver")
                kept = True
        if kept and attempt.last:  # también el que no se pudo devolver: el tope se alcanzó
            _reached(user_id, actor, client)
        raise


def notify_code_limit(user_id: str) -> bool:
    """Avisa por correo a la cuenta `user_id` de que alcanzó el tope de códigos fallidos
    (ADR-023 §5). Devuelve si envió el aviso; no lo envía en los casos de `mfa.notify_enabled`.
    Sin secretos ni códigos."""
    address = mfa.notice_address(user_id)
    if address is None:
        return False
    hours = -(-settings.MFA_CODE_THROTTLE[1] // 3600)  # hacia arriba: el texto dice «como mucho»
    body = LIMIT_BODY.format(hours=hours)
    mail.send(mail.Message(to=address, subject=LIMIT_SUBJECT, body=body, purpose="mfa_code_limit"))
    return True
