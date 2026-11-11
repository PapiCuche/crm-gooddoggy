"""ASGI: HTTP (Django) + WebSocket (Channels, tenancy-context §4)."""

import logging
import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.production")

django_application = get_asgi_application()

from channels.routing import ProtocolTypeRouter, URLRouter  # noqa: E402
from channels.security.websocket import WebsocketDenier  # noqa: E402
from django.conf import settings  # noqa: E402
from django.db import OperationalError, connection  # noqa: E402
from django.urls import path, re_path  # noqa: E402

from apps.accounts.websocket import ExactOriginValidator, SessionAuthMiddleware  # noqa: E402
from apps.conversations.realtime import InboxConsumer  # noqa: E402

# Rutas WS de tenant (`/ws/o/<org_slug>/…`, consumers con TenantConsumerMixin): llegan con sus
# módulos. Origen exacto (APP_ORIGIN o CSRF_TRUSTED_ORIGINS), luego la sesión (ADR-003 §6).
websocket_urlpatterns: list[object] = [
    path("ws/o/<slug:org_slug>/inbox/", InboxConsumer.as_asgi()),  # F6-07a
]
application = ProtocolTypeRouter(
    {
        "http": django_application,
        # Ruta WS desconocida: rechazo limpio (403), no un 500 por "No route found".
        "websocket": ExactOriginValidator(
            SessionAuthMiddleware(
                URLRouter([*websocket_urlpatterns, re_path(r"", WebsocketDenier.as_asgi())])
            ),
            [origin for origin in (settings.APP_ORIGIN, *settings.CSRF_TRUSTED_ORIGINS) if origin],
        ),
    }
)

if settings.ENFORCE_RUNTIME_DB_ROLE:
    # ADR-002 §1.1: con un rol no apto el proceso no arranca (el receptor de
    # connection_created lanza ImproperlyConfigured). BD caída ≠ rol no apto: se registra y
    # se reintenta en la siguiente conexión.
    try:
        connection.ensure_connection()
    except OperationalError:
        logging.getLogger(__name__).warning(
            "BD no disponible al arrancar; se verificará al conectar"
        )
    finally:
        connection.close()
