from typing import Any

from rest_framework import serializers

from apps.accounts.models import User


class LoginSerializer(serializers.Serializer[Any]):
    email = serializers.CharField(max_length=254)
    password = serializers.CharField(max_length=1024, trim_whitespace=False, write_only=True)


class PasswordResetRequestSerializer(serializers.Serializer[Any]):
    email = serializers.CharField(max_length=254, help_text="El correo de la cuenta.")


class PasswordResetConfirmSerializer(serializers.Serializer[Any]):
    token = serializers.CharField(
        max_length=128, trim_whitespace=False, help_text="El secreto del enlace, del fragmento."
    )
    password = serializers.CharField(max_length=1024, trim_whitespace=False, write_only=True)


class MfaEnrollmentStartSerializer(serializers.Serializer[Any]):
    password = serializers.CharField(
        max_length=1024,
        trim_whitespace=False,
        write_only=True,
        help_text="La contraseña actual de la cuenta.",
    )


class MfaEnrollmentSerializer(serializers.Serializer[Any]):
    secret = serializers.CharField(help_text="El secreto en base32. Solo se enseña esta vez.")
    uri = serializers.CharField(help_text="La URI `otpauth://` para un código QR.")
    expires_in = serializers.IntegerField(help_text="Segundos que quedan para confirmar el alta.")


class MfaEnrollmentConfirmSerializer(serializers.Serializer[Any]):
    code = serializers.CharField(
        max_length=16, help_text="El código de seis dígitos de la aplicación de autenticación."
    )


class MfaDisableSerializer(serializers.Serializer[Any]):
    password = serializers.CharField(
        max_length=1024,
        trim_whitespace=False,
        write_only=True,
        help_text="La contraseña actual de la cuenta.",
    )
    code = serializers.CharField(
        max_length=32,
        help_text="El código de la aplicación de autenticación, o un código de respaldo.",
    )


class MfaRegenerateSerializer(serializers.Serializer[Any]):
    password = serializers.CharField(
        max_length=1024,
        trim_whitespace=False,
        write_only=True,
        help_text="La contraseña actual de la cuenta.",
    )
    code = serializers.CharField(
        max_length=32,
        help_text="El código de la aplicación de autenticación. Uno de respaldo no vale aquí.",
    )


class MfaStatusSerializer(serializers.Serializer[Any]):
    enabled = serializers.BooleanField(help_text="La cuenta tiene un dispositivo confirmado.")
    enabled_at = serializers.DateTimeField(
        allow_null=True, help_text="Desde cuándo; `null` si no lo tiene."
    )
    enrollment_expires_in = serializers.IntegerField(
        allow_null=True,
        help_text="Segundos que quedan para confirmar un alta empezada; `null` si no hay ninguna.",
    )
    backup_codes_left = serializers.IntegerField(
        help_text="Códigos de respaldo que quedan sin gastar."
    )
    step_up_expires_in = serializers.IntegerField(
        allow_null=True,
        help_text="Segundos que le quedan al step-up de esta sesión; `null` si no hay uno vigente.",
    )


class LoginPendingSerializer(serializers.Serializer[Any]):
    mfa_required = serializers.BooleanField(
        help_text="Siempre `true`: falta el segundo paso (`POST /auth/login/mfa/`)."
    )


class LoginMfaSerializer(serializers.Serializer[Any]):
    code = serializers.CharField(
        max_length=32,
        help_text="El código de la aplicación de autenticación, o un código de respaldo.",
    )


class MfaStepUpSerializer(LoginMfaSerializer):
    pass


class MfaStepUpResultSerializer(serializers.Serializer[Any]):
    step_up_expires_in = serializers.IntegerField(
        help_text="Segundos que vale la verificación desde ahora (15 minutos)."
    )


class MfaBackupCodesSerializer(serializers.Serializer[Any]):
    backup_codes = serializers.ListField(
        child=serializers.CharField(),
        help_text="Los códigos de respaldo, de un solo uso. Solo se enseñan esta vez.",
    )


class UserSerializer(serializers.ModelSerializer[User]):
    class Meta:
        model = User
        fields = ("id", "email", "first_name", "last_name")
        read_only_fields = fields


class SessionSerializer(serializers.Serializer[Any]):
    user = UserSerializer()
