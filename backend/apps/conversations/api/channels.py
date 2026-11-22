"""Leer, conectar, rotar el token y desconectar las cuentas de canal (F7-02d y F7-07a, ADR-029 §2;
Messenger e Instagram en F7b-06a, ADR-035 §3, con las mismas garantías).

`channels.manage`, comprobado antes de leer el cuerpo o la base; el cuerpo, antes del 404. Una
cuenta que no existe, es de otra organización o ya está desconectada da el mismo 404 (al conectar,
rotar o desconectar, también la Sandbox). Ninguna respuesta lleva el token ni su cifrado: solo sus
últimas cuatro letras, las fechas y el estado.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from django.db.models import QuerySet
from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_view
from rest_framework import generics, serializers
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.access.permissions import rbac_errors
from apps.conversations import accounts, graph, selectors
from apps.conversations.api import inputs
from apps.conversations.models import (
    LIMIT_TIERS,
    PAGE_SUBSCRIPTIONS,
    QUALITY_RATINGS,
    STATUS_DETAILS,
    AccountStatus,
    ChannelAccount,
    Credential,
    Platform,
)
from core.api.errors import ApiError
from core.api.schema import errors
from core.tenancy import context

REFUSED = "Meta no aceptó ese token para ese número. Revisa el token, el WABA y el número."
UNAVAILABLE = "No se pudo consultar a Meta ahora. Vuelve a intentarlo en unos minutos."
DISABLED = "El canal de WhatsApp no está habilitado en este despliegue."
SOCIAL_REFUSED = "Meta no aceptó ese token para esa página. Revisa el token, la página y la cuenta."
SOCIAL_DISABLED = "Los canales de Messenger e Instagram no están habilitados en este despliegue."
FIELD = {"MESSENGER": "page_id", "INSTAGRAM": "instagram_account_id"}  # la cuenta que se registra
PLATFORM = OpenApiParameter("platform", str, OpenApiParameter.PATH, enum=["messenger", "instagram"])


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


class SocialAccountSerializer(serializers.Serializer[Any]):
    id = serializers.UUIDField()
    platform = serializers.ChoiceField(choices=["MESSENGER", "INSTAGRAM"])
    name = serializers.CharField()
    display_identifier = serializers.CharField()
    page_id = serializers.CharField(help_text="En Instagram, la página vinculada.")
    instagram_account_id = serializers.CharField(allow_null=True)
    status = serializers.ChoiceField(choices=AccountStatus.choices)
    status_detail = serializers.ChoiceField(choices=STATUS_DETAILS, allow_null=True)
    last_healthcheck_at = serializers.DateTimeField(allow_null=True)
    token_last_four = serializers.CharField()
    token_rotated_at = serializers.DateTimeField(allow_null=True)
    token_expires_at = serializers.DateTimeField(allow_null=True)
    version = serializers.IntegerField()
    created_at = serializers.DateTimeField()


class ChannelAccountSerializer(serializers.Serializer[Any]):
    id = serializers.UUIDField()
    platform = serializers.ChoiceField(choices=Platform.choices)
    name = serializers.CharField()
    status = serializers.ChoiceField(choices=AccountStatus.choices)
    status_detail = serializers.ChoiceField(choices=STATUS_DETAILS, allow_null=True)
    quality_rating = serializers.ChoiceField(choices=QUALITY_RATINGS, allow_null=True)
    messaging_limit_tier = serializers.ChoiceField(choices=LIMIT_TIERS, allow_null=True)
    last_healthcheck_at = serializers.DateTimeField(allow_null=True)
    external_account_id = serializers.CharField(help_text="El `phone_number_id` de WhatsApp.")
    external_parent_id = serializers.CharField(
        allow_null=True, help_text="El WABA; `null` en la Sandbox."
    )
    token_last_four = serializers.CharField(
        allow_null=True,
        help_text="Lo único que se enseña del token; `null` si la cuenta no tiene credencial viva.",
    )
    has_standby_conversations = serializers.BooleanField(
        help_text="Instagram o Messenger: alguna conversación no cerrada de la cuenta está en "
        "`standby` (otra app controla el hilo, ADR-035 §4)."
    )
    page_subscription = serializers.ChoiceField(
        choices=PAGE_SUBSCRIPTIONS, allow_null=True, read_only=True,
        help_text="Messenger o Instagram: si la página tiene suscrita la app del CRM según la "
        "última respuesta de Meta; `UNKNOWN` si no se comprobó, no se sabe o esa respuesta tiene "
        "más de cuatro intervalos de salud (una hora por omisión). `null` en las demás.",
    )  # fmt: skip
    version = serializers.IntegerField()
    created_at = serializers.DateTimeField()
    updated_at = serializers.DateTimeField()


def shown(account: ChannelAccount, credential: Credential) -> dict[str, Any]:
    if account.platform != Platform.WHATSAPP:
        return shown_social(account, credential)
    keep = (
        "id", "name", "display_identifier", "status", "status_detail", "quality_rating",
        "messaging_limit_tier", "last_healthcheck_at", "version", "created_at",
    )  # fmt: skip
    data: dict[str, Any] = {key: getattr(account, key) for key in keep}
    data |= {"phone_number_id": account.external_account_id, "waba_id": account.external_parent_id}
    data |= {"token_last_four": credential.last_four, "token_rotated_at": credential.rotated_at}
    data["token_expires_at"] = credential.expires_at
    return WhatsAppAccountSerializer(data).data  # type: ignore[no-any-return]


def shown_social(account: ChannelAccount, credential: Credential) -> dict[str, Any]:
    keep = (
        "id", "platform", "name", "display_identifier", "status", "status_detail",
        "last_healthcheck_at", "version", "created_at",
    )  # fmt: skip
    data: dict[str, Any] = {key: getattr(account, key) for key in keep}
    instagram = account.platform == Platform.INSTAGRAM
    data["page_id"] = account.external_parent_id if instagram else account.external_account_id
    data["instagram_account_id"] = account.external_account_id if instagram else None
    data |= {"token_last_four": credential.last_four, "token_rotated_at": credential.rotated_at}
    data["token_expires_at"] = credential.expires_at
    return SocialAccountSerializer(data).data  # type: ignore[no-any-return]


@contextmanager
def channel_errors(platform: str = Platform.WHATSAPP) -> Iterator[None]:
    """Lo que lanzan los comandos de cuentas, con la forma del contrato (ADR-014). La cuenta que no
    se puede registrar es un 400 en su campo, con el mismo texto que una mal formada."""
    whatsapp = platform == Platform.WHATSAPP
    try:
        with rbac_errors():
            yield
    except accounts.BadNumber as error:  # el mismo error que un id mal formado: sin oráculo
        field = "phone_number_id" if whatsapp else FIELD[platform]
        raise serializers.ValidationError({field: [str(error)]}) from None
    except graph.GraphRejected:
        refused = REFUSED if whatsapp else SOCIAL_REFUSED
        raise ApiError("CHANNEL_CREDENTIALS_REJECTED", 400, refused) from None
    except graph.GraphUnavailable:
        raise ApiError("CHANNEL_PROVIDER_UNAVAILABLE", 503, UNAVAILABLE) from None
    except graph.ChannelNotEnabled:
        disabled = DISABLED if whatsapp else SOCIAL_DISABLED
        raise ApiError("CHANNEL_NOT_ENABLED", 409, disabled) from None


@extend_schema_view(
    post=extend_schema(
        operation_id="channels_whatsapp_connect",
        tags=["conversations"],
        request=inputs.ConnectSerializer,
        responses={201: WhatsAppAccountSerializer, **errors(400, 401, 403, 409, 429, 503)},
        description="Registra un número de WhatsApp (`channels.manage`, MFA reciente): comprueba "
        "con Meta el token y guarda la cuenta `PENDING` con el token cifrado. 400 en "
        "`phone_number_id`: no se puede registrar (mal formado o repetido: el mismo error); "
        "`CHANNEL_CREDENTIALS_REJECTED`; 409 `CHANNEL_NOT_ENABLED`; "
        "503 `CHANNEL_PROVIDER_UNAVAILABLE`; 429 `RATE_LIMITED` (5 peticiones cada 10 minutos por "
        "persona, compartido con Messenger e Instagram).",
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
        platform = self.kwargs.get("platform", Platform.WHATSAPP)  # de la ruta, nunca del cuerpo
        with channel_errors(platform):
            done = accounts.rotate_token(
                context.require(), channel_account_id=self.kwargs["account_id"],
                access_token=wanted.validated_data["access_token"], platform=platform,
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
        platform = self.kwargs.get("platform", Platform.WHATSAPP)
        with channel_errors(platform):
            accounts.disconnect(
                context.require(), channel_account_id=self.kwargs["account_id"], platform=platform
            )
        return Response(status=204)


@extend_schema_view(
    post=extend_schema(
        operation_id="channels_social_connect",
        tags=["conversations"],
        parameters=[PLATFORM],
        request=inputs.SocialConnectSerializer,
        responses={201: SocialAccountSerializer, **errors(400, 401, 403, 409, 429, 503)},
        description="Registra una página de Messenger o una cuenta de Instagram como la ruta de "
        "WhatsApp (ADR-035 §3): Meta comprueba la página (y su cuenta de Instagram) y se suscribe "
        "la app. El 400 sin oráculo va en `page_id` (Messenger) o `instagram_account_id`.",
    ),
)
class SocialConnectView(APIView):
    required_permissions = {"POST": "channels.manage"}
    step_up_methods = {"POST"}  # D-F7-11

    def post(self, request: Request, **kwargs: Any) -> Response:
        platform = self.kwargs["platform"]
        body = inputs.SocialConnectSerializer(data=request.data, context={"platform": platform})
        body.is_valid(raise_exception=True)
        with channel_errors(platform):
            made = accounts.connect_social(
                context.require(), platform=platform, **body.validated_data
            )
        return Response(shown(*made), status=201)


@extend_schema_view(
    post=extend_schema(
        operation_id="channels_social_rotate_token",
        tags=["conversations"],
        parameters=[PLATFORM],
        request=inputs.TokenSerializer,
        responses={200: SocialAccountSerializer, **errors(400, 401, 403, 404, 409, 429, 503)},
        description="Como en WhatsApp; Meta comprueba la misma página (y la misma cuenta de "
        "Instagram). 404 también si la cuenta es de otra plataforma.",
    ),
)
class SocialRotateTokenView(RotateTokenView):
    pass


@extend_schema_view(
    delete=extend_schema(
        operation_id="channels_social_disconnect",
        tags=["conversations"],
        parameters=[PLATFORM],
        responses={204: None, **errors(401, 403, 404, 429)},
        description="Como en WhatsApp. 404 también si la cuenta es de otra plataforma.",
    ),
)
class SocialDisconnectView(DisconnectView):
    pass


@extend_schema_view(
    get=extend_schema(
        operation_id="channels_list",
        tags=["conversations"],
        responses={200: ChannelAccountSerializer(many=True), **errors(400, 401, 403, 404)},
        description="Las cuentas de canal vivas de la organización (WhatsApp y Sandbox), por "
        "creación (ADR-016). Pide `channels.manage`: sin él, 403 aunque no haya cuentas. Solo "
        "lectura; nunca el id de la credencial, el cifrado ni más del final del token. En la "
        "Sandbox, `external_parent_id` y `token_last_four` son `null`.",
    ),
)
class ChannelsView(generics.ListAPIView):
    required_permissions = {"GET": "channels.manage"}
    serializer_class = ChannelAccountSerializer

    def get_queryset(self) -> QuerySet[ChannelAccount]:
        return selectors.channel_accounts()


@extend_schema_view(
    get=extend_schema(
        operation_id="channels_retrieve",
        tags=["conversations"],
        responses={200: ChannelAccountSerializer, **errors(401, 403, 404)},
        description="Una cuenta de canal (`channels.manage`). 404: no existe, es de otra "
        "organización o está desconectada, sin distinguirlo. En la Sandbox, `external_parent_id` y "
        "`token_last_four` son `null`.",
    ),
)
class ChannelView(generics.RetrieveAPIView):
    required_permissions = {"GET": "channels.manage"}
    serializer_class = ChannelAccountSerializer
    lookup_url_kwarg = "account_id"

    def get_queryset(self) -> QuerySet[ChannelAccount]:
        return selectors.channel_accounts()
