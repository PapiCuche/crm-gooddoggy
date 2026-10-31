"""Rutas de tenant de las oportunidades: `…/opportunities/` (F5-04, E09-07). Lectura: la
lista (F5-04b) y el detalle con sus líneas y el historial de etapas (F5-04c). Escritura: crear
(F5-04d), cambiar (F5-04e), asignar y borrar (F5-04f), mover y reabrir (F5-04g); ganar y
perder, después.

`opportunities.view` admite alcance (`AssignedTo`: por responsable, `TEAM` incluye a los
compañeros de equipo): `ScopeFilter` deja en el listado y en `get_object()` solo las que la
política permite. Fuera del alcance la oportunidad no existe: 404, igual que la de otra
organización o una borrada.

El contacto sale con `id` y `display_name`, y el nombre solo si quien consulta puede ver ese
contacto (`contacts.view` y su alcance); si no, `display_name` es `null`: tener las oportunidades
de otro no da sus clientes. Ningún otro dato del contacto sale por aquí.

Crear pide `opportunities.create` y `contacts.view`, comprobados antes de leer nada: el contacto
ha de estar en el alcance de `contacts.view`, y si no lo está (o no existe, o es de otra
organización) el 400 de `contact_id` es el mismo, así que crear no sirve para sondear contactos.
Quien crea es el creador y el responsable; el responsable se cambia con `…/assign/`. Cambiar pide
`opportunities.view` y `opportunities.update`: el alcance de los dos se cruza en SQL, sobre la fila
bloqueada (`FOR NO KEY UPDATE`) hasta el final de la petición, y fuera de él es el mismo 404 que un
id que no existe. Las escrituras responden con el detalle, como lo lee `GET`: quien escribe puede
no poder leer.
"""

from collections.abc import Callable, Container, Iterator
from contextlib import contextmanager
from typing import Any
from uuid import UUID

from django.core.exceptions import ObjectDoesNotExist
from django.db.models import OuterRef, QuerySet, Subquery
from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework import generics, serializers
from rest_framework.exceptions import NotFound
from rest_framework.filters import BaseFilterBackend
from rest_framework.request import Request
from rest_framework.response import Response

from apps.access.permissions import ScopeFilter, request_context
from apps.access.selectors import scoped
from apps.contacts.selectors import contacts
from apps.deals import commands as pipeline_commands
from apps.deals import opportunities as commands
from apps.deals import opportunity_stages as stages
from apps.deals import selectors
from apps.deals.api import opportunity_inputs as inputs
from apps.deals.models import Opportunity, StageType
from core.api.errors import ApiError
from core.api.schema import errors
from core.tenancy import context
from core.text import line

TEXT_MAX = 200  # como el título
STALE = "Otra persona cambió la oportunidad: vuelve a cargarla y repite el cambio."
CLOSED = "Está ganada o perdida: reábrela antes de cambiarla."
CLOSING = "Es una etapa de cierre: se llega ganando o perdiendo."
NOT_CLOSED = "Está abierta: solo se reabre una ganada o perdida."
UNAVAILABLE = "El pipeline de la oportunidad no está activo."
GENERIC = "Dato no admitido."
BAD_REFERENCE = {  # sin repetir el id recibido
    "contact_id": "No es un contacto de la organización que puedas ver.",
    "pipeline_id": "No es un pipeline activo de la organización.",
    "variant_id": "No es una variante activa de la organización.",
    "assigned_user_id": "No es un miembro activo de la organización.",
    "stage_id": "No es una etapa activa del pipeline de la oportunidad.",
}


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
        wanted = OpportunitySearchSerializer(data={k: params[k] for k in names if k in params})
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


def locked(found: QuerySet[Opportunity]) -> QuerySet[Opportunity]:
    """Las filas bloqueadas hasta el final de la petición (`FOR NO KEY UPDATE`), sin los `JOIN` de
    la etapa y el motivo: tras esperar un bloqueo, PostgreSQL vuelve a comprobar la fila con la
    unión que ya había leído, y una etapa que cambió mientras esperaba la dejaría fuera (un 404
    donde corresponde el 409 `STALE_VERSION`). El alcance, en cambio, sí se vuelve a decidir."""
    return found.select_related(None).select_for_update(no_key=True, of=("self",))


def _tenant() -> Any:
    tenant = context.current()
    assert tenant is not None  # noqa: S101 — `HasPermission` ya lo comprobó
    return tenant


@contextmanager
def domain(body: Container[str] | None = None) -> Iterator[None]:
    """Lo que lanzan los comandos, con la forma del contrato: una carrera o un estado que no
    admite el cambio es un 409 con su código; una referencia que no sirve, un 400 en su campo (sin
    repetir el valor); lo que no existe, un 404. `body`: los campos del cuerpo que una referencia
    puede nombrar; otra (el pipeline de la oportunidad, que ya no está activo) no es del cliente:
    409 `PIPELINE_UNAVAILABLE`."""
    try:
        yield
    except ObjectDoesNotExist:
        raise NotFound from None
    except pipeline_commands.StaleVersion:
        raise ApiError("STALE_VERSION", 409, STALE) from None
    except commands.OpportunityClosed:
        raise ApiError("OPPORTUNITY_CLOSED", 409, CLOSED) from None
    except stages.ClosingStage:
        raise ApiError("CLOSING_STAGE", 409, CLOSING) from None
    except stages.NotClosed:
        raise ApiError("NOT_CLOSED", 409, NOT_CLOSED) from None
    except stages.MissingFields as missing:
        message = f"La etapa exige datos que faltan: {', '.join(missing.fields)}."
        raise ApiError("MISSING_FIELDS", 409, message) from None
    except commands.BadReference as bad:
        if body is not None and bad.field not in body:
            raise ApiError("PIPELINE_UNAVAILABLE", 409, UNAVAILABLE) from None
        field = "items" if bad.field == "variant_id" else bad.field  # una línea no es un campo
        message = BAD_REFERENCE.get(bad.field, "No es de la organización.")
        raise serializers.ValidationError({field: [message]}) from None
    except ValueError:  # un comando rechazó algo que el serializador dejó pasar
        raise serializers.ValidationError(GENERIC) from None


def reply(request: Request, opportunity_id: UUID, status: int = 200) -> Response:
    """La oportunidad con sus líneas y su historial, como la lee `GET …/{id}/`."""
    found = selectors.with_detail(with_contact_name(selectors.opportunities(), request))
    return Response(OpportunityDetailSerializer(found.get(pk=opportunity_id)).data, status=status)


def _seen_contact(request: Request, contact_id: UUID) -> None:
    """El contacto está en el alcance de `contacts.view` de quien crea, bloqueado hasta el final
    de la petición como lo bloquea el comando (un cambio de responsable a la vez no se cuela).
    `BadReference("contact_id")` si no: el mismo error que para uno que no existe."""
    ectx = request_context(request)
    assert ectx is not None  # noqa: S101 — `HasPermission` ya lo comprobó
    mine = scoped(ectx, "contacts.view", contacts()).filter(pk=contact_id)
    if not mine.select_for_update(no_key=True).values_list("pk", flat=True)[:1]:
        raise commands.BadReference("contact_id")


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
        "mismos. 400: un filtro repetido, un UUID o un `status` que no lo son, `q` que no es una "
        f"línea de texto imprimible o pasa de {TEXT_MAX} caracteres.",
    ),
    post=extend_schema(
        operation_id="opportunities_create",
        tags=["deals"],
        request=inputs.OpportunityCreateSerializer,
        responses={201: OpportunityDetailSerializer, **errors(400, 401, 403, 404)},
        description="Crea una oportunidad abierta de un contacto, en la primera etapa abierta del "
        "pipeline elegido (o del predeterminado), con su número `OPP-…`; quien la crea es su "
        "responsable. Con `opportunities.create` y `contacts.view`. 400 en `contact_id` si no es "
        "un contacto vivo que quien crea pueda ver (el mismo error que para uno que no existe), "
        "en `pipeline_id` o en `items` (una variante) si no son activos, y por cada dato inválido.",
    ),
)
class OpportunitiesView(OpportunitiesQuery, generics.ListAPIView):
    required_permissions = {
        "GET": "opportunities.view",
        "POST": ("opportunities.create", "contacts.view"),
    }
    serializer_class = OpportunitySerializer
    filter_backends = [ScopeFilter, OpportunitySearch]  # en este orden

    def post(self, request: Request, **kwargs: Any) -> Response:
        wanted = inputs.OpportunityCreateSerializer(data=request.data)
        wanted.is_valid(raise_exception=True)
        fields = dict(wanted.validated_data)
        contact_id = fields.pop("contact_id")
        with domain():
            _seen_contact(request, contact_id)
            made = commands.create_opportunity(_tenant(), contact_id=contact_id, **fields)
        return reply(request, made.pk, 201)


@extend_schema_view(
    get=extend_schema(
        operation_id="opportunities_retrieve",
        tags=["deals"],
        responses={200: OpportunityDetailSerializer, **errors(401, 403, 404)},
        description="Una oportunidad con sus líneas y el historial de sus etapas. 404: no es de "
        "la organización, está borrada o queda fuera del alcance de `opportunities.view`.",
    ),
    patch=extend_schema(
        operation_id="opportunities_update",
        tags=["deals"],
        request=inputs.OpportunityUpdateSerializer,
        responses={200: OpportunityDetailSerializer, **errors(400, 401, 403, 404, 409)},
        description="Cambia lo enviado de una oportunidad abierta si `version` sigue vigente. Con "
        "`opportunities.view` y `opportunities.update`; fuera del alcance de uno u otro: 404. "
        "`items` reemplaza todas las líneas. 409: `STALE_VERSION`, `OPPORTUNITY_CLOSED` (ganada o "
        "perdida: se reabre antes). No cambia el contacto, el pipeline, la etapa ni el "
        "responsable. Sin cambios no escribe ni sube `version`.",
    ),
    delete=extend_schema(
        operation_id="opportunities_delete",
        tags=["deals"],
        responses={204: None, **errors(401, 403, 404)},
        description="Borrado lógico, de una abierta o de una cerrada. Con `opportunities.view` y "
        "`opportunities.delete`; fuera del alcance de uno u otro, o ya borrada: 404.",
    ),
)
class OpportunityView(OpportunitiesQuery, generics.RetrieveAPIView):
    required_permissions = {
        "GET": "opportunities.view",
        "PATCH": ("opportunities.view", "opportunities.update"),
        "DELETE": ("opportunities.view", "opportunities.delete"),
    }
    serializer_class = OpportunityDetailSerializer
    lookup_url_kwarg = "opportunity_id"

    def get_queryset(self) -> QuerySet[Opportunity]:
        found = super().get_queryset()
        if self.request.method in ("PATCH", "DELETE"):
            # El alcance se decide sobre la fila bloqueada hasta el final de la petición: un
            # cambio de responsable (`…/assign/`) no se cuela entre la comprobación y la escritura.
            return locked(found)
        return selectors.with_detail(found)

    def patch(self, request: Request, opportunity_id: UUID, **kwargs: Any) -> Response:
        wanted = inputs.OpportunityUpdateSerializer(data=request.data)
        wanted.is_valid(raise_exception=True)
        fields = dict(wanted.validated_data)
        version = fields.pop("version")
        found = self.get_object()
        with domain():
            commands.update_opportunity(
                _tenant(), opportunity_id=found.pk, version=version, **fields
            )
        return reply(request, found.pk)

    def delete(self, request: Request, opportunity_id: UUID, **kwargs: Any) -> Response:
        found = self.get_object()
        with domain():
            commands.delete_opportunity(_tenant(), opportunity_id=found.pk)
        return Response(status=204)


@extend_schema_view(
    post=extend_schema(
        operation_id="opportunities_assign",
        tags=["deals"],
        request=inputs.OpportunityAssignSerializer,
        responses={200: OpportunityDetailSerializer, **errors(400, 401, 403, 404, 409)},
        description="Deja la oportunidad (abierta o cerrada) con ese responsable si `version` "
        "sigue vigente. Con `opportunities.assign` y `opportunities.view`: lo que alcanza lo "
        "decide el alcance de `view`; fuera de él, 404. 400 en `assigned_user_id` si no es un "
        "miembro activo de la organización. 409 `STALE_VERSION`. Si ya es el responsable no "
        "escribe. La respuesta es el detalle, aunque el nuevo responsable saque la oportunidad "
        "del alcance de quien asigna.",
    ),
)
class OpportunityAssignView(OpportunitiesQuery, generics.GenericAPIView):
    required_permissions = {"POST": ("opportunities.view", "opportunities.assign")}
    serializer_class = OpportunityDetailSerializer
    lookup_url_kwarg = "opportunity_id"

    def get_queryset(self) -> QuerySet[Opportunity]:
        # La fila queda bloqueada al decidir el alcance, como en el `PATCH`.
        return locked(super().get_queryset())

    def post(self, request: Request, opportunity_id: UUID, **kwargs: Any) -> Response:
        wanted = inputs.OpportunityAssignSerializer(data=request.data)
        wanted.is_valid(raise_exception=True)
        found = self.get_object()
        with domain():
            commands.assign_opportunity(_tenant(), opportunity_id=found.pk, **wanted.validated_data)
        return reply(request, found.pk)


class StageCommand(OpportunitiesQuery, generics.GenericAPIView):
    """Un cambio de etapa (F5-04g): `POST …/{id}/<acción>/` con `opportunities.view` y el permiso
    de la acción, sobre la fila bloqueada como el `PATCH`. Cada subclase declara su cuerpo y su
    comando. Responde con el detalle."""

    serializer_class = OpportunityDetailSerializer
    lookup_url_kwarg = "opportunity_id"
    wanted: type[serializers.Serializer[Any]]
    run: Callable[..., Opportunity]
    body: frozenset[str] = frozenset()

    def get_queryset(self) -> QuerySet[Opportunity]:
        return locked(super().get_queryset())

    def post(self, request: Request, opportunity_id: UUID, **kwargs: Any) -> Response:
        wanted = self.wanted(data=request.data)
        wanted.is_valid(raise_exception=True)
        found = self.get_object()
        with domain(self.body):
            type(self).run(_tenant(), opportunity_id=found.pk, **wanted.validated_data)
        return reply(request, found.pk)


@extend_schema_view(
    post=extend_schema(
        operation_id="opportunities_move",
        tags=["deals"],
        request=inputs.OpportunityMoveSerializer,
        responses={200: OpportunityDetailSerializer, **errors(400, 401, 403, 404, 409)},
        description="Mueve una oportunidad abierta a otra etapa abierta y activa de su pipeline "
        "(también hacia atrás): toma su probabilidad, reinicia su reloj y deja el paso en el "
        "historial. Con `opportunities.view` y `opportunities.update`; fuera del alcance: 404. "
        "400 en `stage_id` si no es una etapa activa de ese pipeline. 409: `STALE_VERSION`, "
        "`OPPORTUNITY_CLOSED`, `CLOSING_STAGE` (una etapa WON o LOST: se gana o se pierde), "
        "`MISSING_FIELDS` (la etapa exige datos que faltan), `PIPELINE_UNAVAILABLE`. Si ya está "
        "en la etapa no escribe.",
    ),
)
class OpportunityMoveView(StageCommand):
    required_permissions = {"POST": ("opportunities.view", "opportunities.update")}
    wanted = inputs.OpportunityMoveSerializer
    run = staticmethod(stages.move_opportunity)
    body = frozenset({"stage_id"})


@extend_schema_view(
    post=extend_schema(
        operation_id="opportunities_reopen",
        tags=["deals"],
        request=inputs.OpportunityMoveSerializer,
        responses={200: OpportunityDetailSerializer, **errors(400, 401, 403, 404, 409)},
        description="Devuelve una oportunidad ganada o perdida a la etapa abierta y activa "
        "elegida de su pipeline: borra la fecha de cierre, el motivo y la nota, y conserva el "
        "importe. Con `opportunities.view` y `opportunities.update`; fuera del alcance: 404. "
        "400 en `stage_id` si no es una etapa abierta y activa de ese pipeline. 409: "
        "`STALE_VERSION`, `NOT_CLOSED` (está abierta), `MISSING_FIELDS`, `PIPELINE_UNAVAILABLE`.",
    ),
)
class OpportunityReopenView(StageCommand):
    required_permissions = {"POST": ("opportunities.view", "opportunities.update")}
    wanted = inputs.OpportunityMoveSerializer
    run = staticmethod(stages.reopen_opportunity)
    body = frozenset({"stage_id"})
