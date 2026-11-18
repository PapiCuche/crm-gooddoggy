"""Los topes de IA de la organización (F8-05b3; ADR-034 §5): leerlos y fijarlos, con las garantías
de la clave del proveedor. `ai.manage` y después el segundo factor reciente, en GET y en PUT: sin
el permiso, 403 `PERMISSION_DENIED`, nunca `STEP_UP_REQUIRED`. El PUT cuenta en el tope por persona
de las escrituras de IA (`apps.ai_gateway.throttle`, 429). La organización es la de la ruta; el
cuerpo solo aporta los dos importes (cualquier otro campo se ignora). Ningún error repite el
importe enviado."""

from decimal import Decimal
from typing import Any

from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework import serializers
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.access.permissions import rbac_errors
from apps.ai_gateway import budgets
from apps.ai_gateway.api.views import LIMITED, MANAGE
from apps.ai_gateway.models import AIBudget
from core.api.schema import errors
from core.tenancy import context

ABOVE = "El tope diario no puede superar al mensual."
INVALID = "Un importe en USD mayor que cero, menor que un millón y con 6 decimales como mucho."
FIELD = {"max_digits": 12, "decimal_places": 6}
MESSAGES = {key: INVALID for key in (
    "invalid", "min_value", "max_digits", "max_decimal_places", "max_whole_digits", "required",
    "null",
)}  # fmt: skip


class AiBudgetSerializer(serializers.Serializer[Any]):
    monthly_usd = serializers.DecimalField(
        **FIELD, allow_null=True, help_text="Tope del mes de Lima en USD; null: sin tope."
    )
    daily_usd = serializers.DecimalField(
        **FIELD, allow_null=True, help_text="Tope del día de Lima en USD; null: sin tope."
    )


class AiBudgetWriteSerializer(serializers.Serializer[Any]):
    monthly_usd = serializers.DecimalField(
        **FIELD, min_value=budgets.PLACES, error_messages=MESSAGES
    )
    daily_usd = serializers.DecimalField(
        **FIELD, min_value=budgets.PLACES, error_messages=MESSAGES,
        help_text="No puede superar al mensual.",
    )  # fmt: skip

    def validate(self, attrs: dict[str, Decimal]) -> dict[str, Decimal]:
        if attrs["daily_usd"] > attrs["monthly_usd"]:
            raise serializers.ValidationError({"daily_usd": [ABOVE]})
        return attrs


def shown(budget: AIBudget | None) -> dict[str, Any]:
    caps = {"monthly_usd": budget.monthly_usd, "daily_usd": budget.daily_usd} if budget else {
        "monthly_usd": None, "daily_usd": None}  # fmt: skip
    return AiBudgetSerializer(caps).data  # type: ignore[no-any-return]


@extend_schema_view(
    get=extend_schema(
        operation_id="ai_budget_retrieve",
        tags=["ai"],
        responses={200: AiBudgetSerializer, **errors(401, 403, 404)},
        description="Los topes mensual y diario de IA de la organización, en USD con 6 decimales "
        "(`ai.manage` y segundo factor reciente); null si no hay: sin los dos, la IA no llama "
        "(`AI_BUDGET_NOT_SET`). No cuenta en el tope por persona de las escrituras.",
    ),
    put=extend_schema(
        operation_id="ai_budget_update",
        tags=["ai"],
        request=AiBudgetWriteSerializer,
        responses={200: AiBudgetSerializer, **errors(400, 401, 403, 404, 429)},
        description="Fija los dos topes (`ai.manage` y segundo factor reciente) y lo audita "
        "(`ai.limits.changed`, con el antes y el después). Importes en USD mayores que cero, con 6 "
        "decimales como mucho; el diario no supera al mensual. Bajar un tope por debajo de lo "
        "gastado corta la siguiente llamada." + LIMITED,
    ),
)
class BudgetView(APIView):
    required_permissions = {"GET": MANAGE, "PUT": MANAGE}
    step_up_methods = {"GET", "PUT"}

    def get(self, request: Request, **kwargs: Any) -> Response:
        with rbac_errors():
            return Response(shown(budgets.read(context.require())))

    def put(self, request: Request, **kwargs: Any) -> Response:
        wanted = AiBudgetWriteSerializer(data=request.data)
        wanted.is_valid(raise_exception=True)
        try:
            with rbac_errors():
                done = budgets.set_caps(context.require(), **wanted.validated_data)
        except budgets.InvalidCap as error:
            raise serializers.ValidationError({error.field: [INVALID]}) from None
        return Response(shown(done))
