"""Conectar, rotar el token y desconectar una cuenta de WhatsApp (F7-02b, ADR-029 §2).

`channels.manage`, comprobado antes de leer el cuerpo; el cuerpo, antes del 404. Una cuenta que no
existe, es de otra organización, es Sandbox o ya está desconectada da el mismo 404. Ninguna
respuesta lleva el token ni su cifrado: solo sus últimas cuatro letras, las fechas y el estado.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework import serializers
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.access.permissions import rbac_errors
from apps.conversations import accounts, graph
from apps.conversations.api import inputs
from apps.conversations.models import (
    LIMIT_TIERS,
    QUALITY_RATINGS,
    STATUS_DETAILS,
    AccountStatus,
    ChannelAccount,
    Credential,
)
from core.api.errors import ApiError
from core.api.schema import errors
from core.tenancy import context

REFUSED = "Meta no aceptó ese token para ese número. Revisa el token, el WABA y el número."
UNAVAILABLE = "No se pudo consultar a Meta ahora. Vuelve a intentarlo en unos minutos."
DISABLED = "El canal de WhatsApp no está habilitado en este despliegue."


class WhatsAppAccountSerializer(serializers.Serializer[Any]):
    id = serializers.UUIDField()
    name = serializers.CharField()
    display_identifier = serializers.CharField()
    phone_number_id = serializers.CharField()
    waba_id = serializers.CharField()
    status = serializers.ChoiceField(choices=AccountStatus.choices, help_text="Nace `PENDING`.")
    status_detail = serializers.ChoiceField(choices=STATUS_DETAILS, allow_null=True)
    quality_rating = serializers.ChoiceField(choices=QUALITY_RATINGS, allow_null=True)
    messaging_limit_tier = serializers.ChoiceField(choices=LIMIT_TIERS, allow_null=True)
    last_healthcheck_at = serializers.DateTimeField(allow_null=True)
    token_last_four = serializers.CharField(help_text="Lo único que se enseña del token.")
    token_rotated_at = serializers.DateTimeField(allow_null=True)
    token_expires_at = serializers.DateTimeField(allow_null=True)
    version = serializers.IntegerField()
    created_at = serializers.DateTimeField()


def shown(account: ChannelAccount, credential: Credential) -> dict[str, Any]:
    keep = (
        "id", "name", "display_identifier", "status", "status_detail", "quality_rating",
        "messaging_limit_tier", "last_healthcheck_at", "version", "created_at",
    )  # fmt: skip
    data: dict[str, Any] = {key: getattr(account, key) for key in keep}
    data["phone_number_id"], data["waba_id"] = (
        account.external_account_id,
        account.external_parent_id,
    )
    data["token_last_four"] = credential.last_four
    data["token_rotated_at"], data["token_expires_at"] = (
        credential.rotated_at,
        credential.expires_at,
    )
    return WhatsAppAccountSerializer(data).data  # type: ignore[no-any-return]


@contextmanager
def channel_errors() -> Iterator[None]:
    """Lo que lanzan los comandos de cuentas, con la forma del contrato (ADR-014)."""
    try:
        with rbac_errors():
            yield
    except accounts.BadNumber:  # el mismo error que un número mal formado: sin oráculo
        raise serializers.ValidationError({"phone_number_id": [accounts.NUMBER_REFUSED]}) from None
    except graph.GraphRejected:
        raise ApiError("CHANNEL_CREDENTIALS_REJECTED", 400, REFUSED) from None
    except graph.GraphUnavailable:
        raise ApiError("CHANNEL_PROVIDER_UNAVAILABLE", 503, UNAVAILABLE) from None
    except graph.ChannelNotEnabled:
        raise ApiError("CHANNEL_NOT_ENABLED", 409, DISABLED) from None


@extend_schema_view(
    post=extend_schema(
        operation_id="channels_whatsapp_connect",
        tags=["conversations"],
        request=inputs.ConnectSerializer,
        responses={201: WhatsAppAccountSerializer, **errors(400, 401, 403, 409, 429, 503)},
        description="Registra un número de WhatsApp (`channels.manage`): comprueba con Meta que el "
        "token abre ese número en ese WABA y guarda la cuenta `PENDING` con el token cifrado; "
        "pasa a `CONNECTED` con el primer evento firmado. 400 en `phone_number_id`: el número "
        "no se puede registrar (mal formado o ya registrado: el mismo error) y "
        "`CHANNEL_CREDENTIALS_REJECTED`; 409 `CHANNEL_NOT_ENABLED`; 503 "
        "`CHANNEL_PROVIDER_UNAVAILABLE`. Pide MFA reciente (`STEP_UP_REQUIRED`, D-F7-11) y tiene "
        "un tope de peticiones (429 `RATE_LIMITED`, con `Retry-After`).",
    ),
)
class ConnectView(APIView):
    required_permissions = {"POST": "channels.manage"}
    step_up_methods = {"POST"}  # D-F7-11: una credencial que da acceso al canal

    def post(self, request: Request, **kwargs: Any) -> Response:
        wanted = inputs.ConnectSerializer(data=request.data)  # el cuerpo, antes de lo demás
        wanted.is_valid(raise_exception=True)
        with channel_errors():
            made = accounts.connect_whatsapp(context.require(), **wanted.validated_data)
        return Response(shown(*made), status=201)


@extend_schema_view(
    post=extend_schema(
        operation_id="channels_whatsapp_rotate_token",
        tags=["conversations"],
        request=inputs.TokenSerializer,
        responses={200: WhatsAppAccountSerializer, **errors(400, 401, 403, 404, 409, 429, 503)},
        description="Cambia el token de la cuenta (`channels.manage`): se comprueba con Meta "
        "contra el mismo número y WABA, sustituye al anterior (que se invalida y pierde su "
        "cifrado). 404: no existe, es de otra organización o ya está desconectada.",
    ),
)
class RotateTokenView(APIView):
    required_permissions = {"POST": "channels.manage"}
    step_up_methods = {"POST"}  # D-F7-11

    def post(self, request: Request, **kwargs: Any) -> Response:
        wanted = inputs.TokenSerializer(data=request.data)  # el cuerpo, antes del 404
        wanted.is_valid(raise_exception=True)
        with channel_errors():
            done = accounts.rotate_token(
                context.require(), channel_account_id=self.kwargs["account_id"],
                access_token=wanted.validated_data["access_token"],
            )  # fmt: skip
        return Response(shown(*done))


@extend_schema_view(
    delete=extend_schema(
        operation_id="channels_whatsapp_disconnect",
        tags=["conversations"],
        responses={204: None, **errors(401, 403, 404, 429)},
        description="Desconecta la cuenta (`channels.manage`): queda borrada, su token se "
        "invalida y su número se libera. Para volver a usarla se conecta otra cuenta.",
    ),
)
class DisconnectView(APIView):
    required_permissions = {"DELETE": "channels.manage"}

    def delete(self, request: Request, **kwargs: Any) -> Response:
        with channel_errors():
            accounts.disconnect(context.require(), channel_account_id=self.kwargs["account_id"])
        return Response(status=204)
