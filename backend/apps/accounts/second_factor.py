"""Dar por bueno un código del segundo factor de una cuenta (F2-103, ADR-023 §2, §5 y §6).

`accept` decide si lo presentado vale contra el dispositivo confirmado de la cuenta, sea el
código de su aplicación de autenticación o uno de respaldo, y **lo gasta** al aceptarlo: el
paso de un código TOTP queda como el último aceptado, y un código de respaldo, usado. Lo
comparten todas las rutas que comprueban un código de un dispositivo confirmado: el segundo
paso del acceso, el step-up, quitar el factor y regenerar los códigos de respaldo.

No cuenta intentos, no audita y no abre sesión: eso es de cada ruta, que antes cuenta el
intento con `code_limit` y, si `accept` devuelve un método, lo devuelve. No dice por qué un
código no vale. Nada de aquí registra un código ni un secreto.
"""

import logging

from django.db import connection
from django.utils import timezone

from apps.accounts import backup_codes, mfa, totp
from apps.accounts.models import MfaBackupCode, MfaDevice, User
from core import sealed
from core.observability import reporting

logger = logging.getLogger(__name__)

TOTP, BACKUP_CODE = "totp", "backup_code"  # el método, como lo audita el acceso (ADR-023 §5)


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
    gastado. Fuera de una transacción se niega.

    Seis dígitos son un código TOTP: vale el de un paso de la ventana posterior al último
    aceptado (ADR-023 §2). Un secreto que no se abre es un código que no vale, y se reporta
    (ADR-023 §3). Cualquier otra cosa se mira como código de respaldo de esa cuenta, escrito
    como lo teclee una persona; vale si no está gastado (ADR-023 §6), también con el secreto
    ilegible. Lo presentado llega ya sin espacios alrededor.

    `NoDevice` si la cuenta no tiene un dispositivo confirmado: quien llama lo comprueba antes
    de contar el intento.
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
