"""Cifrado en reposo de los secretos que la aplicación tiene que poder leer (ADR-022).

Para lo que no admite un hash: el secreto TOTP de una cuenta, la credencial de un proveedor.
AES-256-GCM con una clave derivada, por propósito, de la clave maestra vigente (`SECRETS_KEKS`).
Lo cifrado queda atado a su propósito y a su dueño: copiado a otra fila, no se abre.

Nada de aquí registra ni devuelve en un error la clave, el texto en claro ni el cifrado.
"""

import os
import re

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

FORMAT = 1  # primer byte de todo valor cifrado: cambia si cambia cualquier cosa de abajo
PURPOSE = re.compile(r"[a-z][a-z0-9_.]{0,31}")  # una constante del código: `mfa.totp`
OWNER_MAX = 200
PLAINTEXT_MAX = 4096  # bytes: aquí caben secretos, no documentos
_INFO = b"good-doggy-crm/sealed/v1/"
_HEAD, _NONCE, _TAG = 3, 12, 16  # formato y versión de la clave; nonce; etiqueta de GCM


class SealError(Exception):
    """El valor no se puede abrir: su clave ya no está, se alteró, o es de otro propósito o de
    otro dueño. No dice cuál: quien llama lo trata como un secreto que no existe."""


def _bound(purpose: str, owner: str) -> bytes:
    """Lo que ata el cifrado a su sitio (los datos asociados de GCM). `owner` identifica la fila
    dueña del secreto (`user:<id>`): no es secreto, y no cambia mientras el secreto viva."""
    if not PURPOSE.fullmatch(purpose):
        raise ValueError("purpose: un identificador en minúsculas de hasta 32 caracteres")
    if not (0 < len(owner) <= OWNER_MAX and owner.isascii() and owner.isprintable()):
        raise ValueError(f"owner: de 1 a {OWNER_MAX} caracteres ASCII imprimibles")
    return purpose.encode() + b"\x00" + owner.encode()


def _cipher(version: int, purpose: str) -> AESGCM:
    """La clave de un propósito, derivada de la clave maestra de esa versión (HKDF-SHA256): la
    maestra nunca cifra datos, y lo cifrado para un propósito no se abre con la clave de otro."""
    master = settings.SECRETS_KEKS.get(version)
    if master is None:
        raise SealError
    info = _INFO + purpose.encode()
    return AESGCM(HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=info).derive(master))


def _keys() -> dict[int, bytes]:
    keys: dict[int, bytes] = settings.SECRETS_KEKS
    if not keys:  # falla cerrado: sin clave no se guarda ni se lee ningún secreto
        raise ImproperlyConfigured("SECRETS_KEKS es obligatorio para cifrar o descifrar un secreto")
    return keys


def seal(purpose: str, owner: str, plaintext: bytes) -> bytes:
    """Cifra `plaintext` para ese propósito y ese dueño, con la clave vigente (la de versión más
    alta). Devuelve formato, versión de la clave, nonce y cifrado con su etiqueta: lo que se
    guarda. Dos llamadas iguales dan valores distintos."""
    version = max(_keys())
    bound = _bound(purpose, owner)
    if not isinstance(plaintext, bytes) or not 0 < len(plaintext) <= PLAINTEXT_MAX:
        raise ValueError(f"plaintext: de 1 a {PLAINTEXT_MAX} bytes")
    head = bytes([FORMAT]) + version.to_bytes(2, "big")
    nonce = os.urandom(_NONCE)
    return head + nonce + _cipher(version, purpose).encrypt(nonce, plaintext, head + bound)


def unseal(purpose: str, owner: str, sealed: bytes) -> bytes:
    """Abre lo que `seal` cifró para ese mismo propósito y ese mismo dueño. `SealError` si no
    se puede, sin decir por qué."""
    _keys()
    bound = _bound(purpose, owner)
    sealed = bytes(sealed)  # un `BinaryField` devuelve `memoryview`
    if len(sealed) <= _HEAD + _NONCE + _TAG or sealed[0] != FORMAT:
        raise SealError
    head, nonce, body = sealed[:_HEAD], sealed[_HEAD : _HEAD + _NONCE], sealed[_HEAD + _NONCE :]
    try:
        return _cipher(int.from_bytes(head[1:], "big"), purpose).decrypt(nonce, body, head + bound)
    except InvalidTag:
        raise SealError from None


def stale(sealed: bytes) -> bool:
    """¿Está cifrado con una clave que ya no es la vigente (o en un formato que no es este)?
    Lo usa quien vuelve a cifrar tras una rotación (ADR-022 §5). No abre nada."""
    sealed = bytes(sealed)
    current = max(_keys())
    return (
        len(sealed) < _HEAD or sealed[0] != FORMAT or sealed[1:_HEAD] != current.to_bytes(2, "big")
    )
