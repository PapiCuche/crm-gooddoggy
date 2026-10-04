"""Vida de la sesión (ADR-003 §2), antes de resolver el tenant.

- Absoluta: `SESSION_ABSOLUTE_AGE` desde el inicio de sesión. Pasado ese tiempo la sesión se
  cierra, haya o no actividad. Una sesión sin marca de inicio se trata como vencida.
- Por inactividad: `SESSION_COOKIE_AGE`. Django solo renueva la caducidad al guardar la
  sesión, así que se guarda como mucho una vez cada `SESSION_REFRESH_INTERVAL`, no en cada
  petición. Se guarda aquí, antes de la vista y de su transacción: si la fila ya no existe
  (otra petición cerró la sesión), esta acaba en 401 y no en un error al responder. Un fallo
  pasajero de la base de datos no es eso: la fila sigue ahí y la sesión no se cierra.
- Usuario desactivado o borrado: Django deja de autenticar la sesión pero conserva la fila.
  Aquí se destruye, para que no reviva si el usuario se reactiva (OBS-F2-03A-2).
- Revocada: la sesión guarda la época del usuario al iniciarla (`users.session_epoch`). Si ya
  no coincide, o falta, se destruye (D-F2-11). El usuario ya está cargado: sin otra consulta.

Solo cubre HTTP: una conexión WebSocket deberá comprobar lo mismo al abrirse (OBS-F2-03C-1).
"""

import time
from collections.abc import Callable

from django.conf import settings
from django.contrib.auth import SESSION_KEY
from django.contrib.auth.models import AnonymousUser
from django.contrib.sessions.backends.base import UpdateError
from django.http import HttpRequest, HttpResponseBase
from django.utils.cache import patch_vary_headers
from django.utils.http import http_date

from apps.accounts.services import AUTH_AT, EPOCH, SEEN_AT


def _end(request: HttpRequest) -> None:
    request.session.flush()  # borra la fila; `SessionMiddleware` borra la cookie al responder
    request.user = AnonymousUser()


def _renew_cookie(response: HttpResponseBase, key: str) -> None:
    """La cookie con su nueva caducidad, como la emite `SessionMiddleware` al guardar."""
    age = settings.SESSION_COOKIE_AGE
    response.set_cookie(
        settings.SESSION_COOKIE_NAME,
        key,
        max_age=age,
        expires=http_date(time.time() + age),
        domain=settings.SESSION_COOKIE_DOMAIN,
        path=settings.SESSION_COOKIE_PATH,
        secure=bool(settings.SESSION_COOKIE_SECURE),
        httponly=bool(settings.SESSION_COOKIE_HTTPONLY),
        samesite=settings.SESSION_COOKIE_SAMESITE,  # type: ignore[arg-type]
    )
    patch_vary_headers(response, ("Cookie",))


class SessionLifetimeMiddleware:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponseBase]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponseBase:
        session, renewed = request.session, False
        if SESSION_KEY in session:  # una sesión de usuario, siga valiendo o no
            now, started, seen = int(time.time()), session.get(AUTH_AT), session.get(SEEN_AT)
            age = now - started if isinstance(started, int) else settings.SESSION_ABSOLUTE_AGE
            user = request.user
            if age >= settings.SESSION_ABSOLUTE_AGE or not user.is_authenticated:
                _end(request)
            elif session.get(EPOCH) != user.session_epoch:  # revocada (D-F2-11)
                _end(request)
            elif not isinstance(seen, int) or now - seen >= settings.SESSION_REFRESH_INTERVAL:
                session[SEEN_AT] = now
                try:
                    session.save()  # renueva la caducidad de la fila
                    session.modified, renewed = False, True  # ya guardada: no otra vez al responder
                except UpdateError:  # Django lo lanza ante cualquier error al actualizar
                    key = session.session_key
                    if key and session.exists(key):
                        raise  # fallo pasajero: la sesión sigue valiendo, la petición no
                    _end(request)  # la fila se borró mientras tanto
        response = self.get_response(request)
        if renewed and not session.modified and session.session_key:
            _renew_cookie(response, session.session_key)  # la vista no tocó la sesión
        return response
