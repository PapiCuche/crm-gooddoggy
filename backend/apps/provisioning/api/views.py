"""Accesos de prueba en el login (F2-117, ADR-026 §5): qué cuentas sintéticas se pueden usar."""

from typing import Any

from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.exceptions import NotFound
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.provisioning import test_accounts
from core.api.permissions import Public
from core.api.schema import errors


class TestAccountSerializer(serializers.Serializer[Any]):
    email = serializers.EmailField(help_text="Correo de la cuenta sintética, del dominio `.test`.")
    role = serializers.CharField(help_text="Nombre de su rol de plantilla. Solo para enseñarlo.")


class TestAccountsSerializer(serializers.Serializer[Any]):
    accounts = TestAccountSerializer(many=True)


class TestAccountsView(APIView):
    """Con accesos de prueba activos, las cuentas sintéticas que existen, están activas y tienen
    su rol en la organización de pruebas. Sin ellos, 404 como una ruta que no existe. Nunca
    contraseñas ni nada que sirva para entrar: el login sigue siendo el de siempre."""

    permission_classes = [Public]

    @extend_schema(
        operation_id="auth_test_accounts",
        tags=["auth"],
        auth=[],
        responses={200: TestAccountsSerializer, **errors(404)},
    )
    def get(self, request: Request) -> Response:
        try:
            shown = test_accounts.listed()
        except test_accounts.Disabled:
            raise NotFound from None
        accounts = [{"email": item.email, "role": item.role} for item in shown]
        return Response(TestAccountsSerializer({"accounts": accounts}).data)
