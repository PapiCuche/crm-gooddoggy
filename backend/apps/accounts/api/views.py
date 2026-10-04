"""Rutas de plataforma de acceso (`/api/v1/auth/…`): sin tenant (ADR-014 §4)."""

from typing import Any

from django.middleware.csrf import get_token
from drf_spectacular.utils import extend_schema
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts import services
from apps.accounts.api.serializers import LoginSerializer, SessionSerializer
from core.api.permissions import Authenticated, Public
from core.api.schema import errors


def _session(request: Request) -> dict[str, Any]:
    return dict(SessionSerializer({"user": request.user}).data)


class CsrfView(APIView):
    permission_classes = [Public]

    @extend_schema(operation_id="auth_csrf", tags=["auth"], auth=[], responses={204: None})
    def get(self, request: Request) -> Response:
        """Entrega la cookie `csrftoken` antes del primer método no seguro."""
        get_token(request._request)
        return Response(status=204)


class LoginView(APIView):
    permission_classes = [Public]

    @extend_schema(
        operation_id="auth_login",
        tags=["auth"],
        auth=[],
        request=LoginSerializer,
        responses={200: SessionSerializer, **errors(400, 401, 403, 429)},
    )
    def post(self, request: Request) -> Response:
        """Inicia sesión. `INVALID_CREDENTIALS` no distingue el motivo del rechazo."""
        credentials = LoginSerializer(data=request.data)
        credentials.is_valid(raise_exception=True)
        services.login(request._request, **credentials.validated_data)
        request.user = request._request.user  # la vista responde ya con la sesión nueva
        return Response(_session(request))


class LogoutView(APIView):
    permission_classes = [Authenticated]

    @extend_schema(
        operation_id="auth_logout",
        tags=["auth"],
        request=None,
        responses={204: None, **errors(401, 403)},
    )
    def post(self, request: Request) -> Response:
        services.logout(request._request)
        return Response(status=204)


class SessionView(APIView):
    permission_classes = [Authenticated]

    @extend_schema(
        operation_id="auth_session",
        tags=["auth"],
        responses={200: SessionSerializer, **errors(401)},
    )
    def get(self, request: Request) -> Response:
        """La sesión actual. 401 `NOT_AUTHENTICATED` si no hay, caducó o su usuario no está
        activo."""
        return Response(_session(request))
