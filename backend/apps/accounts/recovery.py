"""Recuperación de contraseña (E01-02, ADR-021): emitir el enlace y enviarlo por correo.

Lo llama la tarea `accounts.send_password_reset`. Es la única pieza que ve el enlace: lo genera,
guarda su SHA-256 y lo pone en el correo. No lo registra ni lo devuelve.
"""

import hashlib
import secrets
from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import PasswordReset, User
from core import mail

# ADR-021 §3 y §4: constantes del código.
LINK_LIFETIME = timedelta(minutes=60)
LINKS_PER_DAY = 5
LINK_GAP = timedelta(minutes=2)
LINK_PATH = "/restablecer"  # la pantalla que lee el secreto del fragmento
SUBJECT = "Restablece tu contraseña de Good Doggy CRM"
BODY = """Hola:

Alguien pidió restablecer la contraseña de tu cuenta de Good Doggy CRM. Si fuiste tú, abre este
enlace para elegir una nueva:

{link}

El enlace vale {minutes} minutos y una sola vez. Si pides otro, este deja de valer.

Si no fuiste tú, no hagas nada: tu contraseña no cambia.
"""


def token_hash(secret: str) -> str:
    """Lo que se guarda de un enlace, y por lo que se busca: el SHA-256 de su secreto."""
    return hashlib.sha256(secret.encode()).hexdigest()


def issue_link(email: str) -> bool:
    """Emite un enlace para la cuenta de `email` (en su forma canónica) y lo envía. Devuelve si
    emitió uno.

    No emite nada, sin decir por qué, si el correo no tiene cuenta, si la cuenta está desactivada
    o es de personal de plataforma, si `core.mail` no podría escribir a esa dirección, o si la
    cuenta llegó a uno de sus topes. Los topes se cuentan con la fila de la cuenta bloqueada. La
    fila del enlace se confirma, y el bloqueo se suelta, antes de abrir la conexión con el
    servidor de correo: un enlace emitido cuenta, se entregue o no. Si la entrega falla lanza
    `MailError`. Sin `APP_ORIGIN` lanza `ImproperlyConfigured` antes de mirar nada.
    """
    if not settings.APP_ORIGIN:
        raise ImproperlyConfigured("APP_ORIGIN es obligatorio para emitir un enlace")
    with transaction.atomic(durable=True):  # dentro de otra transacción no se confirmaría
        accounts = User.objects.select_for_update(no_key=True)
        user = accounts.filter(email=email, is_active=True, is_platform_staff=False).first()
        if user is None or not mail.deliverable(user.email):
            return False
        now = timezone.now()
        issued = PasswordReset.objects.filter(user=user)
        today = issued.filter(created_at__gt=now - timedelta(hours=24)).count()
        if today >= LINKS_PER_DAY or issued.filter(created_at__gt=now - LINK_GAP).exists():
            return False
        secret = secrets.token_urlsafe(32)  # 256 bits
        issued.create(user=user, token_hash=token_hash(secret), expires_at=now + LINK_LIFETIME)
    # El secreto va en el fragmento: el navegador no lo envía al servidor (ADR-021 §4).
    link = f"{settings.APP_ORIGIN}{LINK_PATH}#{secret}"
    minutes = int(LINK_LIFETIME.total_seconds() // 60)
    body = BODY.format(link=link, minutes=minutes)
    mail.send(mail.Message(to=user.email, subject=SUBJECT, body=body, purpose="password_reset"))
    return True
