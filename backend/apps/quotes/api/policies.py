"""Rutas de las políticas de descuento por rol (F5-09b5; 04 §L.3): `…/quotes/discount-policies/`.
`GET` las lista y `PUT` fija el límite de un rol para una lista (o todas), las dos con
`discount_policies.manage` (sensible, sin alcance). `PUT` pide además un segundo factor de hace 15
minutos como mucho, que se comprueba **después** del permiso: quien no lo tiene recibe el 403 sin
motivo, nunca `STEP_UP_REQUIRED`. Las reglas (nadie da más de lo que tiene, la versión, la
auditoría) son del comando `apps.quotes.policies`.

Cuerpo cerrado y estricto: un campo desconocido (también `organization_id`) o de otro tipo es 400.
Un rol o una lista que no existe, de otra organización, o el rol Owner, es el mismo 400. Los
importes y porcentajes viajan como texto con dos decimales; la política no lleva costos."""

from typing import Any

from django.db.models import QuerySet
from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework import generics, serializers
from rest_framework.request import Request
from rest_framework.response import Response

from apps.access.permissions import rbac_errors
from apps.quotes import policies
from apps.quotes.models import DiscountPolicy
from core.api.errors import ApiError
from core.api.schema import errors
from core.api.strict import Closed, Id, Text, Whole
from core.tenancy import context

EXISTS = "Ese rol ya tiene una política para esa lista: cámbiala con su versión."
STALE = "Otra persona cambió la política: vuelve a cargarla y repite el cambio."
FIGURE = "Texto con hasta 2 decimales, sin exponente."


class DiscountPolicySerializer(serializers.ModelSerializer[DiscountPolicy]):
    role_id = serializers.UUIDField()
    price_list_id = serializers.UUIDField(
        source="applies_to_price_list_id", allow_null=True, help_text="`null`: todas las listas."
    )

    class Meta:
        model = DiscountPolicy
        fields = (
            "id", "role_id", "price_list_id", "max_discount_percent", "max_discount_amount",
            "currency", "version", "created_at", "updated_at",
        )  # fmt: skip


class DiscountPolicySetSerializer(Closed, serializers.Serializer[Any]):
    role_id = Id()
    price_list_id = Id(required=False, allow_null=True, help_text="Sin ella: todas las listas.")
    max_discount_percent = Text(max_length=16, help_text=f"De 0 a 100. {FIGURE}")
    max_discount_amount = Text(
        required=False, allow_null=True, max_length=24, help_text=f"Tope en importe. {FIGURE}"
    )
    currency = Text(
        required=False,
        allow_null=True,
        max_length=3,
        help_text="La del tope: con lista, la de la lista; sin ella, va con el tope y solo él.",
    )
    version = Whole(
        required=False, allow_null=True, min_value=1, help_text="Sin ella crea; con ella cambia."
    )


@extend_schema_view(
    get=extend_schema(
        operation_id="discount_policies_list",
        tags=["quotes"],
        responses={200: DiscountPolicySerializer(many=True), **errors(400, 401, 403, 404)},
        description="Las políticas de descuento de la organización, paginadas por creación "
        "(ADR-016). Un rol sin política tiene 0 %; el rol Owner no tiene límite.",
    ),
    put=extend_schema(
        operation_id="discount_policies_set",
        tags=["quotes"],
        request=DiscountPolicySetSerializer,
        responses={
            200: DiscountPolicySerializer,
            201: DiscountPolicySerializer,
            **errors(400, 401, 403, 404, 409),
        },
        description="Fija el límite de un rol para una lista (o todas): sin `version` la crea "
        "(201), con ella la cambia (200; lo que no se envía se quita; si nada cambia no escribe "
        "ni audita). Pide un segundo factor reciente: 403 `STEP_UP_REQUIRED`. 403 sin motivo si "
        "el límite pasa del que tiene quien actúa (también al tocar uno mayor que el suyo). "
        "400: un rol o una lista que no es de la organización, o el rol Owner. 409 "
        "`DISCOUNT_POLICY_EXISTS` (sin `version`, ya hay una) o `STALE_VERSION`.",
    ),
)
class DiscountPoliciesView(generics.ListAPIView):
    # PATCH y DELETE se declaran para que quien tiene el permiso reciba 405 (quien no, 403), y
    # piden segundo factor: un método que se añada aquí no nace sin él.
    required_permissions = {"GET": policies.MANAGE, "PUT": policies.MANAGE,
                            "PATCH": policies.MANAGE, "DELETE": policies.MANAGE}  # fmt: skip
    step_up_methods = {"PUT", "PATCH", "DELETE"}
    serializer_class = DiscountPolicySerializer
    ordering = "id"

    def get_queryset(self) -> QuerySet[DiscountPolicy]:
        return DiscountPolicy.objects.all()

    def put(self, request: Request, **kwargs: Any) -> Response:
        wanted = DiscountPolicySetSerializer(data=request.data)
        wanted.is_valid(raise_exception=True)
        tenant = context.current()
        assert tenant is not None  # noqa: S101 — `HasPermission` ya lo comprobó
        try:
            with rbac_errors():
                row = policies.set_discount_policy(tenant, **wanted.validated_data)
        except policies.Invalid as invalid:
            raise serializers.ValidationError({invalid.field: [str(invalid)]}) from None
        except policies.DuplicatePolicy:
            raise ApiError("DISCOUNT_POLICY_EXISTS", 409, EXISTS) from None
        except policies.StalePolicy:
            raise ApiError("STALE_VERSION", 409, STALE) from None
        created = wanted.validated_data.get("version") is None
        return Response(DiscountPolicySerializer(row).data, status=201 if created else 200)
