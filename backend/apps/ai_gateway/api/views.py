"""Las cuentas del proveedor de IA de la organización (F8-03c; ADR-034 §4): listar, crear, rotar la
clave, borrar (desactivar), probar la conexión (F8-03d) y cambiar el modelo (F8-05b6). Cada ruta
pide `ai.manage` y, después, el segundo factor de hace 15 minutos como mucho (`step_up_methods`,
D-F7-11): sin el permiso, 403 `PERMISSION_DENIED`, nunca `STEP_UP_REQUIRED`. Antes de todo eso, en
las escrituras (POST y DELETE, y PUT del modelo), el tope por persona (`apps.ai_gateway.throttle`,
429); listar no cuenta (F8-03e). El cuerpo se valida antes del 404. Una cuenta que no existe, es de
otra organización o ya está desactivada da el mismo 404. Ninguna respuesta lleva la clave ni su
cifrado (solo `key_last_four`, el proveedor, el estado y las fechas), ni el cuerpo del proveedor: la
prueba devuelve solo su código.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from uuid import UUID

from django.conf import settings
from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework import serializers
from rest_framework.exceptions import NotFound
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.access.permissions import rbac_errors
from apps.ai_gateway import accounts, keys
from apps.ai_gateway.models import AccountStatus, AIProviderAccount, Provider
from apps.ai_gateway.probe import Probe
from core.api.errors import ApiError
from core.api.schema import errors
from core.tenancy import context

MANAGE = "ai.manage"
STEP_UP = {"GET", "POST", "DELETE"}  # todas: la cuenta de un tercero con costo (D-F7-11)
EXISTS = "La organización ya tiene una cuenta de ese proveedor: rota su clave."
CONFLICT = "Otra persona está guardando la misma cuenta. Vuelve a intentarlo."
MALFORMED = "La clave no tiene el formato de una clave de API."
UNKNOWN = "Proveedor no admitido."
MODEL_REFUSED = "Modelo no admitido: elige uno de la lista permitida de su proveedor."
KEY_HELP = "Solo se escribe: ninguna respuesta la devuelve."
LIMITED = " 429 `RATE_LIMITED`: el tope por persona de las escrituras, con `Retry-After`."


class AiProviderAccountSerializer(serializers.Serializer[Any]):
    id = serializers.UUIDField()
    provider = serializers.ChoiceField(choices=Provider.choices)
    status = serializers.ChoiceField(choices=AccountStatus.choices)
    model = serializers.CharField(help_text="Con el que se estima, reserva, llama y cobra.")
    key_last_four = serializers.CharField(help_text="Lo único que se enseña de la clave.")
    configured_at = serializers.DateTimeField(help_text="Cuándo se guardó la clave vigente.")
    rotated_at = serializers.DateTimeField(allow_null=True)
    revoked_at = serializers.DateTimeField(allow_null=True)
    updated_at = serializers.DateTimeField()


# La clave se valida aquí, antes de buscar la cuenta (el cuerpo, antes del 404). Ningún mensaje de
# error repite lo que se envió.
class AiProviderKeySerializer(serializers.Serializer[Any]):
    api_key = serializers.CharField(
        write_only=True, max_length=256, trim_whitespace=False, help_text=KEY_HELP
    )

    def validate_api_key(self, value: str) -> str:
        if not keys.KEY_FORMAT.fullmatch(value):
            raise serializers.ValidationError(MALFORMED)
        return value


class AiProviderAccountCreateSerializer(AiProviderKeySerializer):
    # Sin `{input}`: un campo cambiado de sitio pondría la clave en el mensaje (ADR-034 §4).
    provider = serializers.ChoiceField(
        choices=Provider.choices, error_messages={"invalid_choice": UNKNOWN}
    )


class AiProviderModelSerializer(serializers.Serializer[Any]):
    # Ningún mensaje repite el modelo enviado; el de otro proveedor lo rechaza el comando.
    model = serializers.CharField(
        max_length=64, error_messages=dict.fromkeys(
            ("invalid", "required", "null", "blank", "max_length"), MODEL_REFUSED),
        help_text="Uno de la lista permitida del proveedor de la cuenta (`AI_MODELS`).",
    )  # fmt: skip

    def validate_model(self, value: str) -> str:
        if not any(value in listed for listed in settings.AI_MODELS.values()):
            raise serializers.ValidationError(MODEL_REFUSED)
        return value


class AiProviderTestSerializer(serializers.Serializer[Any]):
    result = serializers.ChoiceField(
        choices=[(code.value, code.value) for code in Probe],
        help_text="Solo el código de la prueba, nunca lo que contestó el proveedor.",
    )


def shown(account: AIProviderAccount) -> dict[str, Any]:
    return AiProviderAccountSerializer(account).data  # type: ignore[no-any-return]


@contextmanager
def account_errors() -> Iterator[None]:
    """Lo que lanzan los comandos, con la forma del contrato (ADR-014). Nunca con la clave."""
    try:
        with rbac_errors():
            yield
    except keys.MalformedKey:
        raise serializers.ValidationError({"api_key": [MALFORMED]}) from None
    except accounts.AccountNotFound:
        raise NotFound from None
    except accounts.InvalidModel:
        raise serializers.ValidationError({"model": [MODEL_REFUSED]}) from None
    except accounts.AccountExists:
        raise ApiError("AI_ACCOUNT_EXISTS", 409, EXISTS) from None
    except keys.AccountConflict:
        raise ApiError("AI_ACCOUNT_CONFLICT", 409, CONFLICT) from None


def provider_of(account_id: UUID) -> str:
    """El proveedor de la cuenta `account_id` de esta organización; si no es suya, 404. Si está
    desactivada, el 404 lo da el comando (`AccountNotFound`)."""
    found = AIProviderAccount.objects.filter(pk=account_id)
    provider: str | None = found.values_list("provider", flat=True).first()
    if provider is None:
        raise NotFound
    return provider


@extend_schema_view(
    get=extend_schema(
        operation_id="ai_provider_accounts_list",
        tags=["ai"],
        responses={200: AiProviderAccountSerializer(many=True), **errors(401, 403, 404)},
        description="Las cuentas de proveedor de IA de la organización, activas y desactivadas "
        "(`ai.manage` y segundo factor reciente). Nunca la clave ni su cifrado: solo "
        "`key_last_four`. No cuenta en el tope por persona de las escrituras.",
    ),
    post=extend_schema(
        operation_id="ai_provider_accounts_create",
        tags=["ai"],
        request=AiProviderAccountCreateSerializer,
        responses={201: AiProviderAccountSerializer, **errors(400, 401, 403, 404, 409, 429)},
        description="Guarda la clave de un proveedor (`ai.manage` y segundo factor reciente), "
        "cifrada; reactiva una cuenta desactivada. 400 en `api_key`: no tiene el formato de una "
        "clave. 409 `AI_ACCOUNT_EXISTS`: ya hay una activa (se rota). 409 `AI_ACCOUNT_CONFLICT`: "
        "otra petición la creó a la vez." + LIMITED,
    ),
)
class AccountsView(APIView):
    required_permissions = {"GET": MANAGE, "POST": MANAGE}
    step_up_methods = STEP_UP

    def get(self, request: Request, **kwargs: Any) -> Response:
        listed = AIProviderAccount.objects.order_by("provider")
        return Response([shown(account) for account in listed])

    def post(self, request: Request, **kwargs: Any) -> Response:
        wanted = AiProviderAccountCreateSerializer(data=request.data)
        wanted.is_valid(raise_exception=True)
        with account_errors():
            made = accounts.create(context.require(), **wanted.validated_data)
        return Response(shown(made), status=201)


@extend_schema_view(
    post=extend_schema(
        operation_id="ai_provider_accounts_rotate_key",
        tags=["ai"],
        request=AiProviderKeySerializer,
        responses={200: AiProviderAccountSerializer, **errors(400, 401, 403, 404, 429)},
        description="Sustituye la clave de la cuenta (`ai.manage` y segundo factor reciente): la "
        "anterior se invalida y su cifrado se borra. 404: no existe, es de otra organización o "
        "está desactivada." + LIMITED,
    ),
)
class AccountKeyView(APIView):
    required_permissions = {"POST": MANAGE}
    step_up_methods = STEP_UP

    def post(self, request: Request, account_id: UUID, **kwargs: Any) -> Response:
        wanted = AiProviderKeySerializer(data=request.data)  # el cuerpo, antes del 404
        wanted.is_valid(raise_exception=True)
        with account_errors():
            done = accounts.rotate(
                context.require(), provider=provider_of(account_id), **wanted.validated_data
            )
        return Response(shown(done))


@extend_schema_view(
    delete=extend_schema(
        operation_id="ai_provider_accounts_destroy",
        tags=["ai"],
        responses={204: None, **errors(401, 403, 404, 429)},
        description="Desactiva la cuenta (`ai.manage` y segundo factor reciente): el cifrado de su "
        "clave se borra. Para volver a usarla se guarda una clave nueva. 404: no existe, es de "
        "otra organización o ya está desactivada." + LIMITED,
    ),
)
class AccountView(APIView):
    required_permissions = {"DELETE": MANAGE}
    step_up_methods = STEP_UP

    def delete(self, request: Request, account_id: UUID, **kwargs: Any) -> Response:
        with account_errors():
            accounts.disable(context.require(), provider=provider_of(account_id))
        return Response(status=204)


@extend_schema_view(
    post=extend_schema(
        operation_id="ai_provider_accounts_test",
        tags=["ai"],
        request=None,
        responses={200: AiProviderTestSerializer, **errors(401, 403, 404, 429)},
        description="Prueba la clave de la cuenta contra el proveedor (`ai.manage` y segundo "
        "factor reciente) con la llamada más barata, sin reintentos, y la audita "
        "(`ai.account.tested`). 200 con el código: `OK`, `CREDENTIALS_REJECTED`, "
        "`PROVIDER_BILLING` o `PROVIDER_UNAVAILABLE`; nunca el cuerpo del proveedor ni la clave. "
        "404: no existe, es de otra organización o está desactivada." + LIMITED,
    ),
)
class AccountTestView(APIView):
    required_permissions = {"POST": MANAGE}
    step_up_methods = STEP_UP

    def post(self, request: Request, account_id: UUID, **kwargs: Any) -> Response:
        with account_errors():
            result = accounts.check(context.require(), provider=provider_of(account_id))
        return Response({"result": result.value})


@extend_schema_view(
    put=extend_schema(
        operation_id="ai_provider_accounts_set_model",
        tags=["ai"],
        request=AiProviderModelSerializer,
        responses={200: AiProviderAccountSerializer, **errors(400, 401, 403, 404, 429)},
        description="Elige el modelo de la cuenta (`ai.manage` y segundo factor reciente) entre "
        "los de la lista permitida de su proveedor y lo audita (`ai.account.model_changed`, con "
        "el antes y el después). Cambia cuánto gasta la organización. 400 en `model`: fuera de "
        "la lista (nunca repite lo enviado). 404: no existe, es de otra organización o está "
        "desactivada." + LIMITED,
    ),
)
class AccountModelView(APIView):
    required_permissions = {"PUT": MANAGE}
    step_up_methods = {"PUT"}

    def put(self, request: Request, account_id: UUID, **kwargs: Any) -> Response:
        wanted = AiProviderModelSerializer(data=request.data)  # el cuerpo, antes del 404
        wanted.is_valid(raise_exception=True)
        with account_errors():
            done = accounts.change_model(
                context.require(), provider=provider_of(account_id), **wanted.validated_data
            )
        return Response(shown(done))
