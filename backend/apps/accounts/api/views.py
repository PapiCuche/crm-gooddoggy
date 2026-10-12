"""Rutas de plataforma de acceso (`/api/v1/auth/…`): sin tenant (ADR-014 §4)."""

from typing import Any

from django.core.exceptions import ValidationError
from django.middleware.csrf import get_token
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.fields import get_error_detail
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts import mfa, recovery, services
from apps.accounts.api.serializers import (
    LoginSerializer,
    MfaBackupCodesSerializer,
    MfaEnrollmentConfirmSerializer,
    MfaEnrollmentSerializer,
    MfaEnrollmentStartSerializer,
    PasswordResetConfirmSerializer,
    PasswordResetRequestSerializer,
    SessionSerializer,
)
from apps.accounts.emails import canonical_email
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


class PasswordResetRequestView(APIView):
    permission_classes = [Public]

    @extend_schema(
        operation_id="auth_password_reset_request",
        tags=["auth"],
        auth=[],
        request=PasswordResetRequestSerializer,
        responses={204: None, **errors(400, 403, 429)},
    )
    def post(self, request: Request) -> Response:
        """Pide un enlace para recuperar la contraseña de la cuenta de ese correo. Responde 204
        exista o no la cuenta: si existe y puede recuperarse, le llega un correo. 400: no es
        una dirección de correo. 429: demasiadas peticiones desde esta red o para ese correo."""
        asked = PasswordResetRequestSerializer(data=request.data)
        asked.is_valid(raise_exception=True)
        try:
            email = canonical_email(asked.validated_data["email"])
        except ValidationError:
            message = "No es una dirección de correo."
            raise serializers.ValidationError({"email": [message]}) from None
        recovery.request_link(request._request, email=email)
        return Response(status=204)


class PasswordResetConfirmView(APIView):
    permission_classes = [Public]

    @extend_schema(
        operation_id="auth_password_reset_confirm",
        tags=["auth"],
        auth=[],
        request=PasswordResetConfirmSerializer,
        responses={204: None, **errors(400, 403, 429)},
    )
    def post(self, request: Request) -> Response:
        """Cambia la contraseña de la cuenta de un enlace de recuperación. Cierra todas sus
        sesiones y no inicia ninguna. 400 `INVALID_RESET_LINK`: el enlace no vale, sin decir por
        qué (no existe, caducó, ya se usó o hay uno más nuevo). 400 `VALIDATION_ERROR` en
        `password`: el enlace vale y no se gasta, pero la contraseña no cumple las reglas. 429:
        demasiados intentos desde esta red."""
        sent = PasswordResetConfirmSerializer(data=request.data)
        sent.is_valid(raise_exception=True)
        try:
            recovery.change_password(request._request, **sent.validated_data)
        except ValidationError as weak:
            raise serializers.ValidationError({"password": get_error_detail(weak)}) from None
        return Response(status=204)


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


class MfaTotpView(APIView):
    permission_classes = [Authenticated]

    @extend_schema(
        operation_id="auth_mfa_totp_start",
        tags=["auth"],
        request=MfaEnrollmentStartSerializer,
        responses={201: MfaEnrollmentSerializer, **errors(400, 401, 403, 409, 429)},
    )
    def post(self, request: Request) -> Response:
        """Empieza el alta del dispositivo TOTP de la cuenta: con su contraseña actual, crea el
        dispositivo sin confirmar (sustituye a otro sin confirmar) y devuelve su secreto, que no
        se vuelve a enseñar. No protege ni exige nada hasta confirmarlo. 400 en `password`: no
        es la contraseña de la cuenta. 409 `MFA_ALREADY_ENABLED`: ya hay un dispositivo
        confirmado. 429: demasiados intentos desde esta red."""
        sent = MfaEnrollmentStartSerializer(data=request.data)
        sent.is_valid(raise_exception=True)
        try:
            enrollment = mfa.start_enrollment(request._request, **sent.validated_data)
        except mfa.WrongPassword:
            wrong = serializers.ErrorDetail("La contraseña no es correcta.", code="invalid")
            raise serializers.ValidationError({"password": [wrong]}) from None
        return Response(MfaEnrollmentSerializer(enrollment).data, status=201)


class MfaTotpConfirmView(APIView):
    permission_classes = [Authenticated]

    @extend_schema(
        operation_id="auth_mfa_totp_confirm",
        tags=["auth"],
        request=MfaEnrollmentConfirmSerializer,
        responses={200: MfaBackupCodesSerializer, **errors(400, 401, 403, 409, 429)},
    )
    def post(self, request: Request) -> Response:
        """Confirma el alta del dispositivo TOTP con un código suyo: desde ahora la cuenta tiene
        segundo factor. Devuelve sus códigos de respaldo, que no se vuelven a enseñar, y cierra
        las demás sesiones de la cuenta. 400 en `code`: el código no vale. 401: la sesión se
        revocó mientras tanto. 409 `MFA_ENROLLMENT_NOT_STARTED`: no hay un alta empezada, o
        caducó. 409 `MFA_ALREADY_ENABLED`: ya estaba confirmada. 429: demasiados intentos."""
        sent = MfaEnrollmentConfirmSerializer(data=request.data)
        sent.is_valid(raise_exception=True)
        try:
            codes = mfa.confirm_enrollment(request._request, **sent.validated_data)
        except mfa.WrongCode:
            wrong = serializers.ErrorDetail("El código no vale.", code="invalid")
            raise serializers.ValidationError({"code": [wrong]}) from None
        return Response(MfaBackupCodesSerializer({"backup_codes": codes}).data)
