"""Recuperación de contraseña (E01-02, ADR-021): pedir el enlace, emitirlo y enviarlo.

`request_link` es lo que hace la ruta que lo pide: cuenta el intento, lo audita y encola la
tarea, sin buscar la cuenta. `issue_link` es lo que hace la tarea `accounts.send_password_reset`:
la única pieza que ve el enlace. Lo genera, guarda su SHA-256 y lo pone en el correo; no lo
registra ni lo devuelve.
"""

import hashlib
import secrets
from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import connection, transaction
from django.http import HttpRequest
from django.utils import timezone

from apps.accounts import throttle
from apps.accounts.models import PasswordReset, User
from apps.accounts.services import client_of
from apps.audit import platform
from core import mail
from core.api.errors import ApiError
from core.redaction import EMAIL_MASK

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


# El límite de quien pide enlaces (ADR-021 §2): por ventana fija, en la tabla de los contadores
# de acceso pero con claves propias (`rst:`). Una sentencia que bloquea la fila: dos instancias
# no se pisan. Devuelve cuántos van en la ventana y los segundos que le quedan.
_SPAN = "%(window)s::int * interval '1 second'"
_COUNT = f"""
INSERT INTO login_throttles AS t (key, failures, blocked_until, updated_at)
VALUES (%(key)s, 1, NULL, now())
ON CONFLICT (key) DO UPDATE SET
  failures = CASE WHEN t.updated_at < now() - {_SPAN} THEN 1 ELSE t.failures + 1 END,
  updated_at = CASE WHEN t.updated_at < now() - {_SPAN} THEN now() ELSE t.updated_at END
RETURNING failures,
  GREATEST(CEIL(EXTRACT(EPOCH FROM updated_at + {_SPAN} - clock_timestamp())), 1)::int
"""  # noqa: S608 — texto fijo, sin datos
_PURGE = f"""
DELETE FROM login_throttles WHERE left(key, 4) = 'rst:' AND updated_at < now() - {_SPAN}
"""  # noqa: S608 — texto fijo, sin datos
QUEUED_FOR = 15 * 60  # segundos: una tarea que nadie recogió en ese tiempo ya no se ejecuta


def _admit(email: str, ip: str | None) -> None:
    """Cuenta la petición por dirección de red y por identificador, exista o no la cuenta, y la
    rechaza (429 con `Retry-After`) si alguno pasó su tope. Una petición rechazada no cuenta."""
    attempt = throttle.keys(email, ip)
    with transaction.atomic(), connection.cursor() as cursor:
        for scope in ("ip", "identifier"):  # siempre en este orden: sin interbloqueos
            limit, window = settings.PASSWORD_RESET_THROTTLE[scope]
            cursor.execute(_COUNT, {"key": f"rst:{attempt[scope]}", "window": window})
            asked, wait = cursor.fetchone()
            if asked > limit:
                transaction.set_rollback(True)
                error = ApiError("RATE_LIMITED", 429)
                error.wait = wait  # el manejador de errores lo pasa a la cabecera
                raise error


def purge_limits() -> int:
    """Borra los contadores de peticiones cuya ventana ya pasó."""
    window = max(window for _, window in settings.PASSWORD_RESET_THROTTLE.values())
    with connection.cursor() as cursor:
        cursor.execute(_PURGE, {"window": window})
        return int(cursor.rowcount)


def request_link(request: HttpRequest, *, email: str) -> None:
    """Admite la petición de un enlace para `email` (en su forma canónica) y encola la tarea
    que decide y envía. **No busca la cuenta:** hace lo mismo exista o no (ADR-021 §2).

    Primero el límite de intentos (`ApiError` 429). Después la fila de auditoría, que se
    confirma antes de encolar: si no se puede escribir, no se encola nada y esto lanza (falla
    cerrado, ADR-013 §5). Si el broker no acepta la tarea, también lanza, igual para cualquier
    correo. El correo viaja como argumento de la tarea, enmascarado para lo que Celery enseña.
    """
    from apps.accounts.tasks import send_password_reset  # noqa: PLC0415 — las tareas importan esto

    if connection.in_atomic_block:  # la fila de auditoría no se confirmaría antes de encolar
        raise RuntimeError("request_link no se llama dentro de una transacción")
    client = client_of(request)
    _admit(email, client["ip"])
    platform.record(
        "auth.password.reset_requested",
        actor_type=platform.Actor.ANONYMOUS,
        identifier=email,
        **client,
    )
    send_password_reset.apply_async(
        args=[email], argsrepr=f"({EMAIL_MASK!r},)", expires=QUEUED_FOR, retry=False
    )


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
