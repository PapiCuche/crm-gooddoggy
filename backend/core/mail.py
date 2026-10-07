"""Correo saliente (ADR-019): único módulo con `django.core.mail`. Un destinatario, texto plano
y SMTP por entorno. Se llama desde una tarea, nunca dentro de una petición; quien envía un
enlace de un solo uso lo genera en esa tarea, y aquí no se guarda ni se registra el mensaje.
"""

import logging
import re
from dataclasses import dataclass
from smtplib import SMTPException

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.core.mail import EmailMessage, get_connection
from django.core.validators import validate_email

from core.redaction import mask_emails

logger = logging.getLogger(__name__)

BACKENDS = {
    "smtp": "django.core.mail.backends.smtp.EmailBackend",
    "memory": "django.core.mail.backends.locmem.EmailBackend",  # tests
}
PURPOSE = re.compile(r"[a-z][a-z0-9_]{0,31}")
SUBJECT_MAX, BODY_MAX = 150, 20_000
# Lo que separa cabeceras o direcciones: nada de esto cabe en un asunto ni en una dirección.
_BREAKS = re.compile(r"[\r\n\x00\x0b\x0c\x1c-\x1e\x85  ]")


class MailError(Exception):
    """El servidor de correo no aceptó el mensaje. La tarea que lo envía decide si reintenta."""


@dataclass(frozen=True, slots=True)
class Message:
    to: str  # una sola dirección
    subject: str
    body: str  # texto plano
    purpose: str  # etiqueta para el log: "invitation", "password_reset"


def _checked(message: Message) -> None:
    if not PURPOSE.fullmatch(message.purpose):
        raise ValueError(f"purpose inválido: {message.purpose!r}")
    to = message.to
    if _BREAKS.search(to) or any(mark in to for mark in ",;<> \t") or to != to.strip():
        raise ValueError("to: una sola dirección, sin nombre ni separadores")
    try:
        validate_email(to)
    except ValidationError:
        raise ValueError("to: no es una dirección de correo") from None
    subject = message.subject
    if not subject.strip() or len(subject) > SUBJECT_MAX or _BREAKS.search(subject):
        raise ValueError(f"subject: una línea de 1 a {SUBJECT_MAX} caracteres")
    if not message.body.strip() or len(message.body) > BODY_MAX or "\x00" in message.body:
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
    to = mask_emails(message.to)
    email = EmailMessage(
        subject=message.subject,
        body=message.body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[message.to],
        connection=get_connection(backend=backend, fail_silently=False),
    )
    try:
        email.send(fail_silently=False)
    except (SMTPException, OSError) as error:
        # El texto del error puede repetir la dirección: se registra solo su tipo.
        logger.warning(
            "mail.failed",
            extra={"purpose": message.purpose, "to": to, "error": type(error).__name__},
        )
        raise MailError(type(error).__name__) from None
    logger.info("mail.sent", extra={"purpose": message.purpose, "to": to})
