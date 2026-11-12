"""Rutas de tenant de las reservas de stock: `…/inventory/reservations/` (F3-35, E04-05). El
servicio es `apps.inventory.reservations` (F3-33); aquí van los permisos, el cuerpo cerrado y los
errores. `inventory.manage` reserva y libera (en el catálogo no tiene alcance: quien lo tiene actúa
sobre toda la organización). `inventory.view` lista (F3-35b), del más reciente al más antiguo:
«las activas primero por vencimiento» no cabe en ADR-016 (solo `id` o `-id`), decisión pendiente."""

from typing import Any
from uuid import UUID

from django.db.models import QuerySet
from django.utils.dateparse import parse_datetime
from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework import generics, serializers
from rest_framework.exceptions import NotFound
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.inventory import reservations as service
from apps.inventory import stock
from apps.inventory.api.stock import INACTIVE, NOT_ENOUGH, VariantLabelSerializer
from apps.inventory.api.views import _checked, _tenant
from apps.inventory.models import InventoryReservation
from apps.inventory.selectors import reservations
from core.api.errors import ApiError
from core.api.schema import errors
from core.api.strict import Choice, Closed, Id, Text, Whole

NOT_ACTIVE = "La reserva venció o se consumió: ya no se libera."
VARIANT_OFF = "La variante no está activa."
TOO_MANY = "La celda ya tiene el máximo de reservas activas: libera alguna."
REUSED = "Esa clave de idempotencia ya se usó para otra reserva."


class Instant(serializers.DateTimeField):
    """Texto ISO 8601 con zona horaria: sin ella el instante es ambiguo, y no se adivina."""

    def to_internal_value(self, value: Any) -> Any:
        try:
            parsed = parse_datetime(value) if isinstance(value, str) else None
        except ValueError:  # bien formado pero imposible: `2030-02-30T10:00:00Z`
            parsed = None
        if parsed is None or parsed.utcoffset() is None:
            raise serializers.ValidationError("Una fecha y hora ISO 8601 con zona horaria.")
        return super().to_internal_value(value)


class ReservationSerializer(serializers.Serializer[Any]):
    id = serializers.UUIDField()
    variant_id = serializers.UUIDField()
    variant = VariantLabelSerializer(read_only=True)
    warehouse_id = serializers.UUIDField()
    quantity = serializers.IntegerField()
    status = serializers.ChoiceField(choices=InventoryReservation.Status.choices)
    expires_at = serializers.DateTimeField()
    reason = serializers.CharField()
    created_by_user_id = serializers.UUIDField(allow_null=True)
    created_at = serializers.DateTimeField()
    ended_at = serializers.DateTimeField(allow_null=True)
    ended_by_user_id = serializers.UUIDField(allow_null=True)
    end_reason = serializers.CharField(allow_null=True)


class ReservationReleaseSerializer(Closed, serializers.Serializer[Any]):
    reason = Text(max_length=stock.REASON_MAX)

    def validate_reason(self, value: str) -> str:
        return str(_checked(stock.reason, value))


class ReservationCreateSerializer(ReservationReleaseSerializer):
    variant_id = Id()
    warehouse_id = Id()
    quantity = Whole(min_value=1, max_value=stock.QUANTITY_MAX)
    expires_at = Instant()
    idempotency_key = Text(
        max_length=100,
        help_text="De 8 a 100 letras ASCII, cifras y `. _ : -`, sin empezar por `import:`.",
    )

    def validate_idempotency_key(self, value: str) -> str:
        return str(_checked(stock.idempotency_key, value))


class ReservationFilterSerializer(serializers.Serializer[Any]):
    status = Choice(
        choices=InventoryReservation.Status.choices,
        required=False,
        help_text="Solo las de ese estado.",
    )
    variant_id = Id(required=False, help_text="Solo las de esa variante.")
    warehouse_id = Id(required=False, help_text="Solo las de ese almacén.")


@extend_schema_view(
    get=extend_schema(
        operation_id="inventory_reservations_list",
        tags=["inventory"],
        parameters=[ReservationFilterSerializer],
        responses={200: ReservationSerializer(many=True), **errors(400, 401, 403, 404)},
        description="Las reservas, con el SKU y el nombre de su variante, de la más reciente a la "
        "más antigua (ADR-016: no hay otro orden). Un filtro que no vale es 400 por campo. Una "
        "reserva `ACTIVE` con `expires_at` ya pasado venció y aún no la cerró el job (cada 5 "
        "minutos): sigue apartando unidades, pero liberarla es 409 `RESERVATION_NOT_ACTIVE`; "
        "trátala como vencida.",
    )
)
class ReservationsView(generics.ListAPIView):
    required_permissions = {"GET": "inventory.view", "POST": "inventory.manage"}
    serializer_class = ReservationSerializer
    ordering = "-id"  # UUIDv7: el orden en que se crearon

    def get_queryset(self) -> QuerySet[InventoryReservation]:
        wanted = ReservationFilterSerializer(data=self.request.query_params.dict())
        wanted.is_valid(raise_exception=True)
        return reservations().filter(**wanted.validated_data)

    @extend_schema(
        operation_id="inventory_reservations_create",
        tags=["inventory"],
        request=ReservationCreateSerializer,
        responses={
            201: ReservationSerializer,
            200: ReservationSerializer,
            **errors(400, 401, 403, 404, 409),
        },
        description="Aparta unidades disponibles hasta `expires_at` (con zona horaria, a 30 días "
        "como mucho). 200: la misma petición, con el mismo `expires_at` exacto, de la misma "
        "persona y con esa `idempotency_key`. 400 por campo. 409: `INSUFFICIENT_STOCK`, "
        "`WAREHOUSE_INACTIVE`, `VARIANT_INACTIVE`, `TOO_MANY_RESERVATIONS` o "
        "`IDEMPOTENCY_KEY_REUSED` (otra petición, o de otra persona, con esa clave).",
    )
    def post(self, request: Request, **kwargs: Any) -> Response:
        wanted = ReservationCreateSerializer(data=request.data)
        wanted.is_valid(raise_exception=True)
        try:
            made, new = service.reserve(_tenant(), service.Wanted(**wanted.validated_data))
        except stock.BadReference as error:
            raise serializers.ValidationError({error.field: [str(error)]}) from None
        except ValueError as error:  # solo `expires_at` llega aquí: lo demás ya lo cerró el cuerpo
            if not str(error).startswith("expires_at"):
                raise
            raise serializers.ValidationError(
                {"expires_at": [str(error).split(": ", 1)[1]]}
            ) from None
        except stock.InsufficientStock:
            raise ApiError("INSUFFICIENT_STOCK", 409, NOT_ENOUGH) from None
        except stock.WarehouseInactive:
            raise ApiError("WAREHOUSE_INACTIVE", 409, INACTIVE) from None
        except service.VariantInactive:
            raise ApiError("VARIANT_INACTIVE", 409, VARIANT_OFF) from None
        except service.TooManyReservations:
            raise ApiError("TOO_MANY_RESERVATIONS", 409, TOO_MANY) from None
        except stock.KeyReused:
            raise ApiError("IDEMPOTENCY_KEY_REUSED", 409, REUSED) from None
        return Response(ReservationSerializer(made).data, status=201 if new else 200)


class ReservationReleaseView(APIView):
    required_permissions = {"POST": "inventory.manage"}

    @extend_schema(
        operation_id="inventory_reservations_release",
        tags=["inventory"],
        request=ReservationReleaseSerializer,
        responses={200: ReservationSerializer, **errors(400, 401, 403, 404, 409)},
        description="Libera una reserva activa y devuelve sus unidades al disponible. 200 también "
        "si ya estaba liberada (no escribe nada). 409 `RESERVATION_NOT_ACTIVE`: venció o se "
        "consumió. 404: no es de la organización.",
    )
    def post(self, request: Request, reservation_id: UUID, **kwargs: Any) -> Response:
        wanted = ReservationReleaseSerializer(data=request.data)
        wanted.is_valid(raise_exception=True)
        try:
            ended, _ = service.release(_tenant(), reservation_id, wanted.validated_data["reason"])
        except InventoryReservation.DoesNotExist, stock.BadReference:
            raise NotFound from None
        except service.ReservationNotActive:
            raise ApiError("RESERVATION_NOT_ACTIVE", 409, NOT_ACTIVE) from None
        return Response(ReservationSerializer(ended).data)
