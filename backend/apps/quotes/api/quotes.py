"""Rutas de tenant de las cotizaciones: `…/quotes/` (F5-09d y F5-09d2, E13). Lectura: la
lista y el detalle con `quotes.view`, que admite alcance por quien creó la cotización (`TEAM`
incluye a sus compañeros de equipo): `ScopeFilter` deja en el listado y en `get_object()` solo las
que la política permite. Fuera del alcance la cotización no existe, igual que la de otra
organización o una inexistente: 404. Escritura (F5-09e): crear con `quotes.create` y
`quotes.view` y cambiar con `quotes.view` y `quotes.update`, comprobados antes de leer el cuerpo o
la fila; el alcance lo decide el comando sobre la fila bloqueada y la respuesta es el detalle de
`GET`. Los errores, en `apps.quotes.api.errors`.

El contacto sale con `id` y `display_name`, y el nombre solo si quien consulta puede ver ese
contacto (`contacts.view` y su alcance); si no, `display_name` es `null`: tener las cotizaciones de
otro no da sus clientes. Los importes son texto con dos decimales y los precios son los que se
congelaron al cotizar. Nunca salen costos ni márgenes: la cotización no los guarda (ADR-006)."""

from typing import Any
from uuid import UUID

from django.db.models import OuterRef, Prefetch, QuerySet, Subquery
from django.utils import timezone
from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework import generics, serializers, views
from rest_framework.filters import BaseFilterBackend
from rest_framework.request import Request
from rest_framework.response import Response

from apps.access.permissions import ScopeFilter, request_context
from apps.access.selectors import scoped
from apps.contacts.selectors import contacts
from apps.quotes import commands, discounts, selectors
from apps.quotes.api import inputs, limit
from apps.quotes.api.errors import domain
from apps.quotes.models import Quote, QuoteItem
from core.api.schema import errors
from core.tenancy import context

MONEY = {"max_digits": 14, "decimal_places": 2}


class QuoteContactRefSerializer(serializers.Serializer[Any]):
    id = serializers.UUIDField(source="contact_id")
    display_name = serializers.CharField(
        source="contact_name",
        allow_null=True,
        help_text="`null` si quien consulta no puede ver el contacto (`contacts.view`).",
    )


class QuoteItemSerializer(serializers.ModelSerializer[QuoteItem]):
    """Una línea con lo que se congeló al cotizar."""

    product_name = serializers.CharField(source="product_name_snapshot")
    variant_name = serializers.CharField(source="variant_name_snapshot")
    sku = serializers.CharField(source="sku_snapshot")
    promotion_id = serializers.UUIDField(source="promotion_id_snapshot", allow_null=True)
    promotion_name = serializers.CharField(source="promotion_name_snapshot", allow_null=True)

    class Meta:
        model = QuoteItem
        fields = (
            "id", "sort_order", "variant_id", "product_name", "variant_name", "sku", "quantity",
            "list_unit_price", "promotion_id", "promotion_name", "unit_price", "discount_type",
            "discount_value", "discount_amount", "final_unit_price", "line_total", "currency",
        )  # fmt: skip


LIST_FIELDS: tuple[str, ...] = (
    "id", "number", "revision", "status", "contact", "opportunity_id", "price_list_id",
    "currency", "tax_mode", "tax_rate", "subtotal", "discount_total", "tax_total", "total",
    "valid_until", "cancelled_at", "created_by_user_id", "created_at", "updated_at", "version",
)  # fmt: skip


class QuoteSerializer(serializers.ModelSerializer[Quote]):
    """La cotización de la lista: sin líneas ni texto libre."""

    contact = QuoteContactRefSerializer(source="*")
    status = serializers.ChoiceField(choices=Quote.Status.choices)
    tax_mode = serializers.ChoiceField(choices=Quote.Tax.choices)

    class Meta:
        model = Quote
        fields = LIST_FIELDS


class QuoteDetailSerializer(QuoteSerializer):
    """Más las notas (texto libre), el motivo de cancelación y las líneas en orden."""

    items = QuoteItemSerializer(many=True)

    class Meta:
        model = Quote
        fields = (*LIST_FIELDS, "notes_to_customer", "internal_notes", "cancel_reason", "items")


class QuoteSearchSerializer(serializers.Serializer[Any]):
    mine = serializers.BooleanField(required=False, help_text="Las que creé yo.")
    status = serializers.ChoiceField(choices=Quote.Status.choices, required=False)
    contact_id = serializers.UUIDField(required=False)
    opportunity_id = serializers.UUIDField(required=False)
    number = serializers.RegexField(
        r"^COT-[0-9]{6,}$", max_length=20, required=False, help_text="El número exacto."
    )
    expired = serializers.BooleanField(
        required=False,
        help_text="Enviadas o vistas con la validez ya pasada, aún sin marcar como vencidas.",
    )


class QuoteSearch(BaseFilterBackend):
    """Los filtros de la lista, detrás de `ScopeFilter`: recortan lo que el alcance ya dejó, y se
    combinan. Cada uno, una sola vez."""

    def filter_queryset(self, request: Any, queryset: Any, view: Any) -> QuerySet[Quote]:
        params, names = request.query_params, tuple(QuoteSearchSerializer().fields)
        if repeated := [k for k in names if len(params.getlist(k)) > 1]:
            raise serializers.ValidationError({k: ["Una sola vez."] for k in repeated})
        wanted = QuoteSearchSerializer(data={k: params[k] for k in names if params.get(k)})
        wanted.is_valid(raise_exception=True)
        asked = dict(wanted.validated_data)
        found: QuerySet[Quote] = queryset
        if asked.pop("mine", False):
            found = found.filter(created_by_user_id=_tenant().user_id)
        if asked.pop("expired", False):
            found = found.filter(status__in=("SENT", "VIEWED"), valid_until__lt=timezone.now())
        return found.filter(**asked)


def with_contact_name(found: QuerySet[Quote], request: Any) -> QuerySet[Quote]:
    """`contact_name`: el nombre del contacto si `contacts.view` y su alcance lo dejan ver, en la
    misma consulta; si no, `NULL` (sin el permiso, `scoped()` no deja ninguna fila)."""
    ectx = request_context(request)
    assert ectx is not None  # noqa: S101 — `HasPermission` ya lo comprobó
    visible = scoped(ectx, "contacts.view", contacts().filter(pk=OuterRef("contact_id")))
    return found.annotate(contact_name=Subquery(visible.values("display_name")[:1]))


def _tenant() -> Any:
    tenant = context.current()
    assert tenant is not None  # noqa: S101 — `HasPermission` ya lo comprobó
    return tenant


class QuotesQuery:
    def get_queryset(self) -> QuerySet[Quote]:
        found = selectors.quotes()
        request = getattr(self, "request", None)
        return found if request is None else with_contact_name(found, request)  # OpenAPI: sin él


@extend_schema_view(
    get=extend_schema(
        operation_id="quotes_list",
        tags=["quotes"],
        parameters=[QuoteSearchSerializer],
        responses={200: QuoteSerializer(many=True), **errors(400, 401, 403, 404)},
        description="Las cotizaciones dentro del alcance de `quotes.view` (las que creó quien "
        "consulta; con alcance de equipo, también las de sus compañeros), paginadas por creación "
        "(ADR-016). Los filtros se combinan y la página siguiente se pide con los mismos: `mine`, "
        "`status`, `contact_id`, `opportunity_id`, `number` (exacto) y `expired`. 400: un filtro "
        "repetido o inválido. Sin líneas: están en el detalle.",
    ),
    post=extend_schema(
        operation_id="quotes_create",
        tags=["quotes"],
        request=inputs.QuoteCreateSerializer,
        responses={201: QuoteDetailSerializer, **errors(400, 401, 403, 409)},
        description="Crea un borrador de quien lo crea, con `quotes.create` y `quotes.view`, de "
        "un contacto o de una oportunidad que ve (`contacts.view`, `opportunities.view`); si no, "
        "o no existen, un único 400 general. 400 en `price_list_id` o `variant_id` si la lista no "
        "se puede usar o una variante no tiene precio vigente. 409 `QUOTE_REFUSED`: la base lo "
        "rechazó. Repetirlo crea otro borrador.",
    ),
)
class QuotesView(QuotesQuery, generics.ListAPIView):
    required_permissions = {"GET": "quotes.view", "POST": ("quotes.create", "quotes.view")}
    serializer_class = QuoteSerializer
    filter_backends = [ScopeFilter, QuoteSearch]  # en este orden

    def post(self, request: Request, **kwargs: Any) -> Response:
        wanted = inputs.QuoteCreateSerializer(data=request.data)
        wanted.is_valid(raise_exception=True)
        with domain():
            made = commands.create_quote(_tenant(), **wanted.validated_data)
        return reply(request, made.pk, 201)


@extend_schema_view(
    get=extend_schema(
        operation_id="quotes_retrieve",
        tags=["quotes"],
        responses={200: QuoteDetailSerializer, **errors(401, 403, 404)},
        description="Una cotización con sus líneas en orden, tal como se congelaron. 404: no es de "
        "la organización, no existe o queda fuera del alcance de `quotes.view`.",
    ),
    patch=extend_schema(
        operation_id="quotes_update",
        tags=["quotes"],
        request=inputs.QuoteUpdateSerializer,
        responses={200: QuoteDetailSerializer, **errors(400, 401, 403, 404, 409)},
        description="Cambia lo enviado de un borrador con `quotes.view` y `quotes.update` (fuera "
        "del alcance: 404). 400 en `variant_id` o `price_list_id` si una línea no tiene precio "
        "vigente en su lista. 409: `STALE_VERSION`, `QUOTE_NOT_DRAFT`, `QUOTE_REFUSED`. Sin "
        "cambios no escribe.",
    ),
)
class QuoteView(QuotesQuery, generics.RetrieveAPIView):
    required_permissions = {"GET": "quotes.view", "PATCH": ("quotes.view", "quotes.update")}
    serializer_class = QuoteDetailSerializer
    lookup_url_kwarg = "quote_id"

    def get_queryset(self) -> QuerySet[Quote]:
        return detail(super().get_queryset())

    def patch(self, request: Request, quote_id: UUID, **kwargs: Any) -> Response:
        wanted = inputs.QuoteUpdateSerializer(data=request.data)
        wanted.is_valid(raise_exception=True)
        with domain():
            commands.update_quote(_tenant(), quote_id=quote_id, **wanted.validated_data)
        return reply(request, quote_id)


def action(
    name: str, command: Any, body: Any, permissions: tuple[str, ...], summary: str
) -> type[views.APIView]:
    """Un `POST` sobre el precio: los permisos antes de leer; el alcance, la versión, el estado y
    la línea de esa cotización, en el comando. Responde con el detalle; `limit` audita negativas."""

    class Action(views.APIView):
        required_permissions = {"POST": permissions}

        def post(self, request: Request, quote_id: UUID, line_id: UUID | None = None,
                 **kwargs: Any) -> Response:  # fmt: skip
            wanted = body(data=request.data)
            wanted.is_valid(raise_exception=True)
            line = {} if line_id is None else {"line_id": line_id}
            with domain(), limit.refusal(request, name, quote_id, line_id):
                command(_tenant(), quote_id=quote_id, **line, **wanted.validated_data)
            return reply(request, quote_id)

    Action.__name__ = f"Quote{name.title().replace('-', '')}View"
    shown = ", ".join(f"`{code}`" for code in permissions)
    view: type[views.APIView] = extend_schema_view(
        post=extend_schema(
            operation_id=f"quotes_{name.replace('-', '_')}",
            tags=["quotes"],
            request=body,
            responses={200: QuoteDetailSerializer, **errors(400, 401, 403, 404, 409, 422, 429)},
            description=f"{summary} Con {shown}; fuera del alcance o inexistente: 404. 409: "
            "`STALE_VERSION`, `QUOTE_NOT_DRAFT`. 429 `RATE_LIMITED` (+`Retry-After`): el tope, "
            "por persona y cotización y por persona, se cuenta antes de buscarla.",
        )
    )(Action)
    return view


QuotePriceView = action(
    "price", discounts.set_manual_price, inputs.QuotePriceSerializer,
    ("quotes.view", "quotes.update", "quotes.override_price"),
    "Precio final a mano de una línea de esa cotización, menor que el vigente y con motivo; pide "
    "aprobación. 422 `PRICE_NOT_ALLOWED`, siempre igual, si no se admite.",
)  # fmt: skip
QuoteRequestApprovalView = action(
    "request-approval", discounts.submit_quote, inputs.QuoteVersionSerializer,
    ("quotes.view", "quotes.update"),
    "Envía un borrador: `APPROVED` si cabe en el límite de quien envía, o `PENDING_APPROVAL`. Un "
    "precio manual vuelve a medirse con el costo de hoy (422 `PRICE_NOT_ALLOWED`).",
)  # fmt: skip


def detail(found: QuerySet[Quote]) -> QuerySet[Quote]:
    lines = QuoteItem.objects.order_by("sort_order")  # las líneas, en una consulta más
    return found.prefetch_related(Prefetch("items", queryset=lines))


def reply(request: Request, quote_id: UUID, status: int = 200) -> Response:
    """La cotización como la lee `GET …/{id}/`, en el alcance de `quotes.view` de quien escribe
    (el comando ya lo comprobó; al crear, es suya)."""
    ectx = request_context(request)
    assert ectx is not None  # noqa: S101 — `HasPermission` ya lo comprobó
    found = scoped(ectx, "quotes.view", with_contact_name(detail(selectors.quotes()), request))
    return Response(QuoteDetailSerializer(found.get(pk=quote_id)).data, status=status)
