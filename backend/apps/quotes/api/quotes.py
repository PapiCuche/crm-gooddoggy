"""Rutas de tenant de las cotizaciones: `…/quotes/` (F5-09d, E13), solo lectura: la lista con
`quotes.view`, que admite alcance por quien creó la cotización (`TEAM` incluye a sus compañeros de
equipo): `ScopeFilter` deja en el listado solo las que la política permite. El detalle llega con
F5-09d2 (fuera del alcance, o de otra organización, será un 404) y la escritura, con F5-09e.

El contacto sale con `id` y `display_name`, y el nombre solo si quien consulta puede ver ese
contacto (`contacts.view` y su alcance); si no, `display_name` es `null`: tener las cotizaciones de
otro no da sus clientes. Los importes son texto con dos decimales y los precios son los que se
congelaron al cotizar. Nunca salen costos ni márgenes: la cotización no los guarda (ADR-006)."""

from typing import Any

from django.db.models import OuterRef, QuerySet, Subquery
from django.utils import timezone
from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework import generics, serializers
from rest_framework.filters import BaseFilterBackend

from apps.access.permissions import ScopeFilter, request_context
from apps.access.selectors import scoped
from apps.contacts.selectors import contacts
from apps.quotes import selectors
from apps.quotes.models import Quote
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
    )
)
class QuotesView(QuotesQuery, generics.ListAPIView):
    required_permissions = {"GET": "quotes.view"}
    serializer_class = QuoteSerializer
    filter_backends = [ScopeFilter, QuoteSearch]  # en este orden
