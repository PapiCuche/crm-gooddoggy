"""Correo saliente (ADR-019): único módulo con `django.core.mail`. Un destinatario, texto plano
y SMTP por entorno. Se llama desde una tarea, nunca dentro de una petición; quien envía un
enlace de un solo uso lo genera en esa tarea, y aquí no se guarda ni se registra el mensaje.
"""

import re
from dataclasses import dataclass, field
from smtplib import SMTPException

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.core.mail import EmailMessage, get_connection
from django.core.validators import validate_email

from core.observability.logging import get_logger
from core.redaction import EMAIL_MASK

# structlog, con los campos por nombre: el `extra` de `logging` no llega a la salida JSON.
logger = get_logger(__name__)

BACKENDS = {
    "smtp": "django.core.mail.backends.smtp.EmailBackend",
    "memory": "django.core.mail.backends.locmem.EmailBackend",  # tests
}
PURPOSE = re.compile(r"[a-z][a-z0-9_]{0,31}")
SUBJECT_MAX, BODY_MAX = 150, 20_000
# Lo que separa cabeceras o direcciones: nada de esto cabe en un asunto ni en una dirección.
# Tampoco un sustituto suelto: no se puede codificar y fallaría con la conexión ya abierta.
_BREAKS = re.compile(r"[\r\n\x00\x0b\x0c\x1c-\x1e\x85\u2028\u2029\ud800-\udfff]")
_BODY_BAD = re.compile(r"[\x00\ud800-\udfff]")


class MailError(Exception):
    """El servidor de correo no aceptó el mensaje. La tarea que lo envía decide si reintenta."""


@dataclass(frozen=True, slots=True, kw_only=True)
class Message:
    """Por nombre, no por posición: un cuerpo puesto donde va el propósito saldría en el `repr`,
    que solo enseña el propósito: lo demás no debe llegar a un log ni a una traza."""

    to: str = field(repr=False)  # una sola dirección
    subject: str = field(repr=False)
    body: str = field(repr=False)  # texto plano
    purpose: str  # etiqueta para el log: "invitation", "password_reset"


def _checked(message: Message) -> None:
    if not PURPOSE.fullmatch(message.purpose):
        raise ValueError("purpose: un identificador en minúsculas de hasta 32 caracteres")
    to = message.to
    if _BREAKS.search(to) or any(mark in to for mark in ",;<> \t") or to != to.strip():
        raise ValueError("to: una sola dirección, sin nombre ni separadores")
    # Lo validado es lo que sale: el transporte quita las comillas (`"a"@x` sale `a@x`), descifra
    # las palabras codificadas (`=?utf-8?b?YW5h?=@x` sale `ana@x`) y pasa el dominio por IDNA.
    if not to.isascii() or '"' in to or "=?" in to:
        raise ValueError("to: en ASCII, sin comillas ni palabras codificadas")
    valid = True
    try:
        validate_email(to)
    except ValidationError:  # lleva la dirección en `params`: el error sale fuera, sin contexto
        valid = False
    if not valid:
        raise ValueError("to: no es una dirección de correo")
    subject = message.subject
    if not subject.strip() or len(subject) > SUBJECT_MAX or _BREAKS.search(subject):
        raise ValueError(f"subject: una línea de 1 a {SUBJECT_MAX} caracteres")
    if not message.body.strip() or len(message.body) > BODY_MAX or _BODY_BAD.search(message.body):
        raise ValueError(f"body: texto de 1 a {BODY_MAX} caracteres")


def send(message: Message) -> None:
    """Entrega `message` o lanza: `ValueError` si el mensaje no es válido (no se intenta),
    `ImproperlyConfigured` si no hay servidor configurado y `MailError` si la entrega falla."""
    _checked(message)
    backend = BACKENDS.get(settings.MAIL_BACKEND)
    if backend is None:
        raise ImproperlyConfigured(f"MAIL_BACKEND desconocido: {settings.MAIL_BACKEND!r}")
    if not settings.DEFAULT_FROM_EMAIL or (
        settings.MAIL_BACKEND == "smtp" and not settings.EMAIL_HOST
    ):
        raise ImproperlyConfigured("El correo saliente no está configurado (EMAIL_HOST, MAIL_FROM)")
    email = EmailMessage(
        subject=message.subject,
        body=message.body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[message.to],
        connection=get_connection(backend=backend, fail_silently=False),
    )
    failure = ""
    try:
        email.send(fail_silently=False)
    except (SMTPException, OSError) as error:
        failure = type(error).__name__  # su texto puede repetir la dirección: solo el tipo
    if failure:  # fuera del `except`: `MailError` no lleva el original ni como `__context__`
        logger.warning("mail.failed", purpose=message.purpose, to=EMAIL_MASK, error=failure)
        raise MailError(failure)
    # La marca, no `mask_emails`: su patrón deja a la vista direcciones válidas (`ana=@x.pe`).
    logger.info("mail.sent", purpose=message.purpose, to=EMAIL_MASK)
