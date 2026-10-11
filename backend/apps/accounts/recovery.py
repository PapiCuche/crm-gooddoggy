"""Recuperación de contraseña (E01-02, ADR-021): pedir el enlace, emitirlo y enviarlo.

`request_link` es lo que hace la ruta que lo pide: cuenta el intento, lo audita y encola la
tarea, sin buscar la cuenta. `issue_link` es lo que hace la tarea `accounts.send_password_reset`:
la única pieza que ve el enlace. Lo genera, guarda su SHA-256 y lo pone en el correo; no lo
registra ni lo devuelve. `change_password` es lo que hace la ruta que recibe el enlace de
vuelta: comprueba que vale y cambia la contraseña de su cuenta.
"""

import hashlib
import re
import secrets
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ImproperlyConfigured
from django.db import connection, transaction
from django.http import HttpRequest
from django.utils import timezone

from apps.accounts import throttle
from apps.accounts.models import PasswordReset, User
from apps.accounts.services import client_of, revoke_sessions, unaudited
from apps.audit import platform
from apps.audit.services import Entity, Result
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


# El límite de pedir enlaces y el de presentarlos (ADR-021 §2 y §5): por ventana fija, en la
# tabla de los contadores de acceso pero con claves propias (`rst:`). Una sentencia que bloquea la
# fila: dos instancias no se pisan. Devuelve cuántos van en la ventana y los segundos que quedan.
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
# Como la purga del acceso, salta las filas bloqueadas: `_admit` bloquea dos (dirección y después
# identificador), y un DELETE que espera a una con la otra ya borrada se interbloquea con él.
_PURGE = f"""
DELETE FROM login_throttles WHERE id IN (
  SELECT id FROM login_throttles
  WHERE left(key, 4) = 'rst:' AND updated_at < now() - {_SPAN}
  FOR UPDATE SKIP LOCKED)
"""  # noqa: S608 — texto fijo, sin datos
QUEUED_FOR = 15 * 60  # segundos: una tarea que nadie recogió en ese tiempo ya no se ejecuta
BROKER_WAIT = 3  # segundos que la petición espera a un broker que no contesta
# La conexión del pool de Celery reintenta conectar aunque se publique con `retry=False`, y la
# de Redis no tiene plazo: la ruta, que es pública, publica con una propia que no reintenta y que
# espera eso como mucho al conectar y por cada respuesta: no es un plazo para toda la publicación.
_NO_WAIT = {"max_retries": 0, "socket_connect_timeout": BROKER_WAIT, "socket_timeout": BROKER_WAIT}


SECRET = re.compile(r"[A-Za-z0-9_-]{43}")  # la forma de `secrets.token_urlsafe(32)`
INVALID_LINK = "INVALID_RESET_LINK"


def _count(counters: dict[str, str]) -> None:
    """Cuenta el intento en cada clave (`{ámbito: clave}`, en su orden) y lo rechaza (429 con
    `Retry-After`) si alguna pasó el tope de su ámbito. Un intento rechazado no cuenta."""
    with transaction.atomic(), connection.cursor() as cursor:
        for scope, key in counters.items():  # siempre en el mismo orden: sin interbloqueos
            limit, window = settings.PASSWORD_RESET_THROTTLE[scope]
            cursor.execute(_COUNT, {"key": f"rst:{key}", "window": window})
            asked, wait = cursor.fetchone()
            if asked > limit:
                transaction.set_rollback(True)
                error = ApiError("RATE_LIMITED", 429)
                error.wait = wait  # el manejador de errores lo pasa a la cabecera
                raise error


def _admit(email: str, ip: str | None) -> None:
    """Cuenta la petición por dirección de red y por identificador, exista o no la cuenta."""
    attempt = throttle.keys(email, ip)
    _count({"ip": attempt["ip"], "identifier": attempt["identifier"]})


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
    correo. El correo viaja como argumento de la tarea, enmascarado en los eventos y el log de
    Celery; sus inspecciones (`inspect active`, `reserved`) enseñan el real.
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
    broker = send_password_reset.app.connection_for_write(
        connect_timeout=BROKER_WAIT, transport_options=_NO_WAIT
    )
    with broker:  # perezosa: se abre al publicar y se cierra al salir
        send_password_reset.apply_async(
            args=[email],
            argsrepr=f"({EMAIL_MASK!r},)",
            expires=QUEUED_FOR,
            retry=False,
            connection=broker,
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


def _refuse(reason: str, user_id: Any, client: dict[str, Any]) -> ApiError:
    """El enlace no vale: la misma respuesta sea cual sea la razón (ADR-021 §5). La razón solo
    queda en la auditoría, con la cuenta si el secreto era de una; si no se puede escribir, la
    respuesta no cambia (ADR-013 §5)."""
    try:
        platform.record(
            "auth.password.changed",
            actor_type=platform.Actor.ANONYMOUS,
            entity=Entity("user", user_id) if user_id else None,
            metadata={"method": "reset_link", "reason": reason},
            result=Result.DENIED,
            **client,
        )
    except Exception as error:
        unaudited(error, "auth.password.changed")
    return ApiError(INVALID_LINK, 400, "El enlace no vale o ya caducó. Pide uno nuevo.")


def change_password(request: HttpRequest, *, token: str, password: str) -> None:
    """Cambia la contraseña de la cuenta del enlace `token`, si el enlace vale (ADR-021 §5).

    Vale si su hash está en la tabla, es el último emitido para su cuenta, no se usó, no caducó
    y su cuenta sigue activa y no es de personal de plataforma; si no, `ApiError`
    `INVALID_RESET_LINK`, igual para todas las razones. Con un enlace que vale, una contraseña
    que no pasa los validadores lanza su `ValidationError` y no lo gasta. Al cambiarla, en una
    transacción y con la fila de la cuenta bloqueada antes que la del enlace (el orden de
    `issue_link`): contraseña, enlace usado, sesiones revocadas y auditoría, o nada. No inicia
    sesión. Antes de todo, el límite de intentos por dirección de red (`ApiError` 429).
    """
    if connection.in_atomic_block:  # la fila de un rechazo se desharía con esa transacción
        raise RuntimeError("change_password no se llama dentro de una transacción")
    client = client_of(request)
    # Su propia clave (`rst:chg:ip:…`): presentar enlaces no gasta el tope de pedirlos.
    _count({"change": "chg:" + throttle.keys("", client["ip"])["ip"]})
    if not SECRET.fullmatch(token):
        raise _refuse("malformed", None, client)
    found = PasswordReset.objects.filter(token_hash=token_hash(token)).values("pk", "user_id")
    if not (link_ref := found.first()):
        raise _refuse("unknown", None, client)
    with transaction.atomic():
        user = User.objects.select_for_update(no_key=True).get(pk=link_ref["user_id"])
        link = PasswordReset.objects.select_for_update().get(pk=link_ref["pk"])
        newer = PasswordReset.objects.filter(user=user, created_at__gt=link.created_at)
        if link.used_at is not None:
            reason = "used"
        elif link.expires_at <= timezone.now():
            reason = "expired"
        elif newer.exists():
            reason = "superseded"
        elif not user.is_active or user.is_platform_staff:
            reason = "account"
        else:
            reason = ""
            validate_password(password, user)  # `ValidationError`: el enlace no se gasta
            user.set_password(password)
            user.save(update_fields=["password", "updated_at"])
            link.used_at = timezone.now()
            link.save(update_fields=["used_at"])
            revoke_sessions(user.pk)
            platform.record(  # en la transacción del cambio: sin esta fila, no hay cambio
                "auth.password.changed",
                actor_type=platform.Actor.ANONYMOUS,
                entity=Entity("user", user.pk),
                metadata={"method": "reset_link"},
                **client,
            )
    if reason:  # fuera de la transacción: la fila del rechazo se confirma por sí misma
        raise _refuse(reason, user.pk, client)
