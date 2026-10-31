"""Rutas de tenant de las oportunidades: `…/opportunities/` (F5-04, E09-07). Lectura: la
lista (F5-04b) y el detalle con sus líneas y el historial de etapas (F5-04c); las escrituras y los
cambios de etapa llegan en las entregas siguientes.

`opportunities.view` admite alcance (`AssignedTo`: por responsable, `TEAM` incluye a los
compañeros de equipo): `ScopeFilter` deja en el listado y en `get_object()` solo las que la
política permite. Fuera del alcance la oportunidad no existe: 404, igual que la de otra
organización o una borrada.

El contacto sale con `id` y `display_name`, y el nombre solo si quien consulta puede ver ese
contacto (`contacts.view` y su alcance); si no, `display_name` es `null`: tener las oportunidades
de otro no da sus clientes. Ningún otro dato del contacto sale por aquí.
"""

from typing import Any

from django.db.models import OuterRef, QuerySet, Subquery
from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework import generics, serializers
from rest_framework.filters import BaseFilterBackend

from apps.access.permissions import ScopeFilter, request_context
from apps.access.selectors import scoped
from apps.contacts.selectors import contacts
from apps.deals import selectors
from apps.deals.models import Opportunity, StageType
from core.api.schema import errors
from core.text import line

TEXT_MAX = 200  # como el título


class StageRefSerializer(serializers.Serializer[Any]):
    id = serializers.UUIDField()
    name = serializers.CharField()
    type = serializers.ChoiceField(choices=StageType.choices, source="stage_type")


class ContactRefSerializer(serializers.Serializer[Any]):
    id = serializers.UUIDField(source="contact_id")
    display_name = serializers.CharField(
        source="contact_name",
        allow_null=True,
        help_text="`null` si quien consulta no puede ver ese contacto (`contacts.view`).",
    )


class LostReasonRefSerializer(serializers.Serializer[Any]):
    id = serializers.UUIDField()
    name = serializers.CharField()


class OpportunitySerializer(serializers.Serializer[Any]):
    id = serializers.UUIDField()
    number = serializers.CharField(help_text="`OPP-000123`: único en la organización.")
    title = serializers.CharField()
    contact = ContactRefSerializer(source="*")
    pipeline_id = serializers.UUIDField()
    stage = StageRefSerializer()
    status = serializers.ChoiceField(choices=StageType.choices, help_text="El tipo de la etapa.")
    amount = serializers.DecimalField(
        max_digits=14, decimal_places=2, help_text="Texto: `1500.50`."
    )
    currency = serializers.CharField()
    probability = serializers.IntegerField(help_text="De 0 a 100; ganada 100, perdida 0.")
    priority = serializers.ChoiceField(choices=Opportunity.Priority.choices)
    assigned_user_id = serializers.UUIDField(allow_null=True, help_text="El responsable.")
    next_action = serializers.CharField(allow_null=True, help_text="Texto libre.")
    expected_close_date = serializers.DateField(allow_null=True)
    stage_entered_at = serializers.DateTimeField()
    last_activity_at = serializers.DateTimeField()
    won_at = serializers.DateTimeField(allow_null=True)
    lost_at = serializers.DateTimeField(allow_null=True)
    lost_reason = LostReasonRefSerializer(allow_null=True)
    version = serializers.IntegerField(help_text="Sube con cada cambio.")


class ItemSerializer(serializers.Serializer[Any]):
    variant_id = serializers.UUIDField(help_text="El nombre y el SKU, en el catálogo.")
    quantity = serializers.IntegerField()
    estimated_unit_price = serializers.DecimalField(
        max_digits=14,
        decimal_places=2,
        help_text="Referencial: el precio firme lo fija la cotización.",
    )
    notes = serializers.CharField()


class HistorySerializer(serializers.Serializer[Any]):
    id = serializers.UUIDField()
    from_stage = StageRefSerializer(allow_null=True, help_text="`null` en el alta.")
    to_stage = StageRefSerializer()
    changed_by_user_id = serializers.UUIDField(allow_null=True)
    changed_at = serializers.DateTimeField()
    seconds_in_previous_stage = serializers.IntegerField(allow_null=True)


class OpportunityDetailSerializer(OpportunitySerializer):
    source = serializers.ChoiceField(choices=Opportunity.Source.choices)
    next_action_at = serializers.DateTimeField(allow_null=True)
    lost_reason_note = serializers.CharField(allow_null=True, help_text="Texto libre.")
    created_at = serializers.DateTimeField()
    updated_at = serializers.DateTimeField()
    items = ItemSerializer(many=True, help_text="Por id.")
    history = HistorySerializer(many=True, help_text="De la primera etapa a la última.")


class OpportunitySearchSerializer(serializers.Serializer[Any]):
    pipeline_id = serializers.UUIDField(required=False)
    stage_id = serializers.UUIDField(required=False)
    status = serializers.ChoiceField(
        choices=StageType.choices, required=False, error_messages={"invalid_choice": "No válido."}
    )
    assigned_user_id = serializers.UUIDField(required=False)
    contact_id = serializers.UUIDField(required=False, help_text="La pestaña de la ficha.")
    q = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=TEXT_MAX,
        help_text="El título o el número contiene el texto, sin distinguir mayúsculas. "
        "Vacío: sin filtro.",
    )

    def validate_q(self, value: str) -> str:
        try:
            return line(value, TEXT_MAX)
        except ValueError as error:
            text = f"{error}."
            raise serializers.ValidationError(text[:1].upper() + text[1:]) from None


class OpportunitySearch(BaseFilterBackend):
    """Los filtros de la lista, detrás de `ScopeFilter`: recortan lo que el alcance ya dejó, y se
    combinan. Cada uno, una sola vez."""

    def filter_queryset(self, request: Any, queryset: Any, view: Any) -> QuerySet[Opportunity]:
        params, names = request.query_params, tuple(OpportunitySearchSerializer().fields)
        if repeated := [k for k in names if len(params.getlist(k)) > 1]:
            raise serializers.ValidationError({k: ["Una sola vez."] for k in repeated})
        given = {k: params[k] for k in names if k in params}
        if given.get("status") == "":  # `?status=` es lo mismo que no pedirlo, como `?q=`
            del given["status"]
        wanted = OpportunitySearchSerializer(data=given)
        wanted.is_valid(raise_exception=True)
        found: QuerySet[Opportunity] = queryset
        filters = dict(wanted.validated_data)
        text = filters.pop("q", "")
        if filters:
            found = found.filter(**filters)
        return selectors.matching(found, text)


def with_contact_name(found: QuerySet[Opportunity], request: Any) -> QuerySet[Opportunity]:
    """`contact_name`: el nombre del contacto si `contacts.view` y su alcance lo dejan ver (la
    misma política que la lista de contactos), en la misma consulta; si no, `NULL` (sin el
    permiso, `scoped()` no deja ninguna fila)."""
    ectx = request_context(request)
    assert ectx is not None  # noqa: S101 — `HasPermission` ya lo comprobó
    visible = scoped(ectx, "contacts.view", contacts().filter(pk=OuterRef("contact_id")))
    return found.annotate(contact_name=Subquery(visible.values("display_name")[:1]))


class OpportunitiesQuery:
    def get_queryset(self) -> QuerySet[Opportunity]:
        found = selectors.opportunities()
        request = getattr(self, "request", None)
        if request is None:  # el generador de OpenAPI
            return found
        return with_contact_name(found, request)


@extend_schema_view(
    get=extend_schema(
        operation_id="opportunities_list",
        tags=["deals"],
        parameters=[OpportunitySearchSerializer],
        responses={200: OpportunitySerializer(many=True), **errors(400, 401, 403, 404)},
        description="Las oportunidades vivas dentro del alcance de `opportunities.view`, paginadas "
        "por creación (ADR-016). Los filtros se combinan y la página siguiente se pide con los "
        "mismos. `status` y `q` vacíos: sin filtro. 400: un filtro repetido, un UUID o un `status` "
        f"que no lo son, `q` que no es una línea de texto imprimible o pasa de {TEXT_MAX} "
        "caracteres.",
    ),
)
class OpportunitiesView(OpportunitiesQuery, generics.ListAPIView):
    required_permissions = {"GET": "opportunities.view"}
    serializer_class = OpportunitySerializer
    filter_backends = [ScopeFilter, OpportunitySearch]  # en este orden


@extend_schema_view(
    get=extend_schema(
        operation_id="opportunities_retrieve",
        tags=["deals"],
        responses={200: OpportunityDetailSerializer, **errors(401, 403, 404)},
        description="Una oportunidad con sus líneas y el historial de sus etapas. 404: no es de "
        "la organización, está borrada o queda fuera del alcance de `opportunities.view`.",
    ),
)
class OpportunityView(OpportunitiesQuery, generics.RetrieveAPIView):
    required_permissions = {"GET": "opportunities.view"}
    serializer_class = OpportunityDetailSerializer
    lookup_url_kwarg = "opportunity_id"

    def get_queryset(self) -> QuerySet[Opportunity]:
        return selectors.with_detail(super().get_queryset())
