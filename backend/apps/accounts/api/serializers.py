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


class LoginPendingSerializer(serializers.Serializer[Any]):
    mfa_required = serializers.BooleanField(
        help_text="Siempre `true`: falta el segundo paso (`POST /auth/login/mfa/`)."
    )


class LoginMfaSerializer(serializers.Serializer[Any]):
    code = serializers.CharField(
        max_length=32,
        help_text="El código de la aplicación de autenticación, o un código de respaldo.",
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
