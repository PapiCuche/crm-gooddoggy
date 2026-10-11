from typing import Any

from rest_framework import serializers

from apps.accounts.models import User


class LoginSerializer(serializers.Serializer[Any]):
    email = serializers.CharField(max_length=254)
    password = serializers.CharField(max_length=1024, trim_whitespace=False, write_only=True)


class PasswordResetRequestSerializer(serializers.Serializer[Any]):
    email = serializers.CharField(max_length=254, help_text="El correo de la cuenta.")


class UserSerializer(serializers.ModelSerializer[User]):
    class Meta:
        model = User
        fields = ("id", "email", "first_name", "last_name")
        read_only_fields = fields


class SessionSerializer(serializers.Serializer[Any]):
    user = UserSerializer()
