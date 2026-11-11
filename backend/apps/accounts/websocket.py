"""Autenticación de un WebSocket por la cookie de sesión (ADR-003 §6): la misma sesión que HTTP
(fila de `django_session` y reglas de `is_current`), nunca un token en la URL. Solo lee: no renueva
la caducidad. Una sesión que no vale es un usuario anónimo, sin distinguir por qué. Ni la cookie ni
la clave de sesión se registran."""

import time
from importlib import import_module
from typing import Any

from channels.db import database_sync_to_async
from django.conf import settings
from django.contrib.auth import get_user
from django.contrib.auth.models import AnonymousUser
from django.http import HttpRequest
from django.http.cookie import parse_cookie

from apps.accounts.middleware import is_current


def user_for(session_key: str | None) -> Any:
    """El usuario de esa sesión, o `AnonymousUser` si no existe, venció o fue revocada."""
    if not session_key:
        return AnonymousUser()
    session = import_module(settings.SESSION_ENGINE).SessionStore(session_key)
    request = HttpRequest()
    request.session = session
    user = get_user(request)  # sesión inexistente, caducada o sin usuario: anónimo
    return user if is_current(session, user, int(time.time())) else AnonymousUser()


def cookie_value(scope: dict[str, Any]) -> str | None:
    header = "; ".join(v.decode("latin-1") for k, v in scope.get("headers", ()) if k == b"cookie")
    return parse_cookie(header).get(settings.SESSION_COOKIE_NAME)


class SessionAuthMiddleware:
    """Pone `scope["user"]` y `scope["session_key"]`; el origen ya lo validó quien envuelve."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> Any:
        if scope["type"] == "websocket":
            key = cookie_value(scope)
            user = await database_sync_to_async(user_for)(key)
            scope = {**scope, "user": user, "session_key": key}
        return await self.inner(scope, receive, send)
