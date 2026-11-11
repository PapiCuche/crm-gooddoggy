"""Rutas de tenant de las tareas: `…/tasks/` (F5-08b, E10). Lectura: la lista y el detalle, con
`tasks.view`, que admite alcance (por responsable; `TEAM` incluye a los compañeros de equipo):
`ScopeFilter` deja en el listado y en `get_object()` solo las que la política permite. Fuera del
alcance la tarea no existe, igual que la de otra organización o una borrada: 404. Escritura
(F5-08b2): crear con `tasks.create` y cambiar o borrar con `tasks.view` y `tasks.update`; la fila
se bloquea (`FOR NO KEY UPDATE`) al decidir el alcance y las escrituras responden con el detalle.

El contacto sale con `id` y `display_name`, y el nombre solo si quien consulta puede ver ese
contacto (`contacts.view` y su alcance); si no, `display_name` es `null`: tener las tareas de otro
no da sus clientes. Al crear, el contacto y la oportunidad han de estar en el alcance de
`contacts.view` y `opportunities.view` de quien crea (y la oportunidad, ser del contacto): si no,
el mismo 400 que para uno que no existe. Borrar no exige versión y repetirlo es un 404 (no es
idempotente). Los filtros de la lista no usan la zona horaria: «de hoy» o «próximas» son
`due_from` y `due_to` (instantes con zona) que calcula el cliente, y «vencidas» es `overdue`."""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from uuid import UUID

from django.core.exceptions import ObjectDoesNotExist
from django.db.models import OuterRef, QuerySet, Subquery
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework import generics, serializers
from rest_framework.exceptions import NotFound
from rest_framework.filters import BaseFilterBackend
from rest_framework.request import Request
from rest_framework.response import Response

from apps.access.permissions import ScopeFilter, request_context
from apps.access.selectors import scoped
from apps.contacts.selectors import contacts
from apps.deals import selectors as deals
from apps.tasks import commands, selectors
from apps.tasks.api import inputs
from apps.tasks.models import Task
from core.api.errors import ApiError
from core.api.schema import errors
from core.tenancy import context

STALE = "Otra persona cambió la tarea: vuelve a cargarla y repite el cambio."
CLOSED = "Está completada o cancelada: reábrela antes de cambiarla."
OPEN = "Está abierta: solo se reabre una completada o cancelada."
GENERIC = "Dato no admitido."
BAD_REFERENCE = {  # sin repetir el id recibido
    "contact_id": "No es un contacto de la organización que puedas ver.",
    "opportunity_id": "No es una oportunidad de ese contacto que puedas ver.",
    "assigned_user_id": "No es un miembro activo de la organización.",
}


class TaskContactRefSerializer(serializers.Serializer[Any]):
    id = serializers.UUIDField(source="contact_id", allow_null=True)
    display_name = serializers.CharField(
        source="contact_name",
        allow_null=True,
        help_text="`null` si no hay contacto o quien consulta no puede verlo (`contacts.view`).",
    )


class TaskSerializer(serializers.Serializer[Any]):
    id = serializers.UUIDField()
    type = serializers.ChoiceField(choices=Task.Type.choices)
    title = serializers.CharField(help_text="Texto libre.")
    description = serializers.CharField(help_text="Texto libre, con saltos de línea.")
    assigned_user_id = serializers.UUIDField(allow_null=True, help_text="El responsable.")
    contact = TaskContactRefSerializer(source="*")
    opportunity_id = serializers.UUIDField(allow_null=True)
    due_at = serializers.DateTimeField(allow_null=True)
    is_all_day = serializers.BooleanField(help_text="`due_at` es un día: se muestra sin hora.")
    reminder_at = serializers.DateTimeField(allow_null=True)
    status = serializers.ChoiceField(choices=Task.Status.choices)
    priority = serializers.ChoiceField(choices=Task.Priority.choices)
    completed_at = serializers.DateTimeField(allow_null=True)
    completed_by_user_id = serializers.UUIDField(allow_null=True)
    cancel_reason = serializers.CharField(allow_null=True, help_text="Texto libre.")
    source = serializers.ChoiceField(choices=Task.Source.choices)
    created_at = serializers.DateTimeField()
    updated_at = serializers.DateTimeField()
    version = serializers.IntegerField(help_text="Sube con cada cambio.")


class Instant(serializers.DateTimeField):
    """Un instante ISO 8601 con zona horaria: sin ella, 400 (el servidor no adivina la zona)."""

    def to_internal_value(self, value: Any) -> Any:
        parsed = parse_datetime(value) if isinstance(value, str) else None
        if parsed is None or parsed.utcoffset() is None:
            raise serializers.ValidationError("Una fecha y hora ISO 8601 con zona horaria.")
        return super().to_internal_value(value)


class TaskSearchSerializer(serializers.Serializer[Any]):
    mine = serializers.BooleanField(required=False, help_text="Las que tengo como responsable.")
    assigned_user_id = serializers.UUIDField(required=False)
    status = serializers.ChoiceField(choices=Task.Status.choices, required=False)
    overdue = serializers.BooleanField(
        required=False, help_text="Abiertas con `due_at` ya pasado (`true`)."
    )
    due_from = Instant(required=False, help_text="`due_at` desde este instante, con zona horaria.")
    due_to = Instant(required=False, help_text="`due_at` antes de este instante, con zona horaria.")
    contact_id = serializers.UUIDField(required=False)
    opportunity_id = serializers.UUIDField(required=False)


class TaskSearch(BaseFilterBackend):
    """Los filtros de la lista, detrás de `ScopeFilter`: recortan lo que el alcance ya dejó, y se
    combinan. Cada uno, una sola vez."""

    def filter_queryset(self, request: Any, queryset: Any, view: Any) -> QuerySet[Task]:
        params, names = request.query_params, tuple(TaskSearchSerializer().fields)
        if repeated := [k for k in names if len(params.getlist(k)) > 1]:
            raise serializers.ValidationError({k: ["Una sola vez."] for k in repeated})
        wanted = TaskSearchSerializer(data={k: params[k] for k in names if params.get(k)})
        wanted.is_valid(raise_exception=True)
        asked = dict(wanted.validated_data)
        found: QuerySet[Task] = queryset
        if asked.pop("mine", False):
            found = found.filter(assigned_user_id=_tenant().user_id)
        if asked.pop("overdue", False):
            found = found.filter(status__in=("PENDING", "IN_PROGRESS"), due_at__lt=timezone.now())
        if "due_from" in asked:
            found = found.filter(due_at__gte=asked.pop("due_from"))
        if "due_to" in asked:
            found = found.filter(due_at__lt=asked.pop("due_to"))
        return found.filter(**asked)


def with_contact_name(found: QuerySet[Task], request: Any) -> QuerySet[Task]:
    """`contact_name`: el nombre del contacto si `contacts.view` y su alcance lo dejan ver, en la
    misma consulta; si no, `NULL` (sin el permiso, `scoped()` no deja ninguna fila)."""
    ectx = request_context(request)
    assert ectx is not None  # noqa: S101 — `HasPermission` ya lo comprobó
    visible = scoped(ectx, "contacts.view", contacts().filter(pk=OuterRef("contact_id")))
    return found.annotate(contact_name=Subquery(visible.values("display_name")[:1]))


def locked(found: QuerySet[Task]) -> QuerySet[Task]:
    """Las filas bloqueadas hasta el final de la petición (`FOR NO KEY UPDATE`), sin los `JOIN`: el
    alcance, en cambio, sí se vuelve a decidir tras esperar."""
    return found.select_related(None).select_for_update(no_key=True, of=("self",))


def _tenant() -> Any:
    tenant = context.current()
    assert tenant is not None  # noqa: S101 — `HasPermission` ya lo comprobó
    return tenant


@contextmanager
def domain() -> Iterator[None]:
    """Lo que lanzan los comandos, con la forma del contrato: una carrera o un estado que no admite
    el cambio es un 409 con su código; una referencia que no sirve, un 400 en su campo (sin repetir
    el valor); lo que no existe, un 404."""
    try:
        yield
    except ObjectDoesNotExist:
        raise NotFound from None
    except commands.StaleVersion:
        raise ApiError("STALE_VERSION", 409, STALE) from None
    except commands.TaskClosed:
        raise ApiError("TASK_CLOSED", 409, CLOSED) from None
    except commands.TaskOpen:
        raise ApiError("TASK_OPEN", 409, OPEN) from None
    except commands.BadReference as bad:
        message = BAD_REFERENCE.get(bad.field, "No es de la organización.")
        raise serializers.ValidationError({bad.field: [message]}) from None
    except ValueError:  # un comando rechazó algo que el serializador dejó pasar
        raise serializers.ValidationError(GENERIC) from None


def reply(request: Request, task_id: UUID, status: int = 200) -> Response:
    """La tarea como la lee `GET …/{id}/`."""
    found = with_contact_name(selectors.tasks(), request).get(pk=task_id)
    return Response(TaskSerializer(found).data, status=status)


def _seen(request: Request, code: str, rows: QuerySet[Any], **match: Any) -> None:
    """La fila está en el alcance de `code` de quien crea; si no, `BadReference` del campo."""
    ectx = request_context(request)
    assert ectx is not None  # noqa: S101 — `HasPermission` ya lo comprobó
    field = "contact_id" if code == "contacts.view" else "opportunity_id"
    if not scoped(ectx, code, rows).filter(**match).values_list("pk", flat=True)[:1]:
        raise commands.BadReference(field)


class TasksQuery:
    def get_queryset(self) -> QuerySet[Task]:
        found = selectors.tasks()
        request = getattr(self, "request", None)
        return found if request is None else with_contact_name(found, request)  # OpenAPI: sin él


@extend_schema_view(
    get=extend_schema(
        operation_id="tasks_list",
        tags=["tasks"],
        parameters=[TaskSearchSerializer],
        responses={200: TaskSerializer(many=True), **errors(400, 401, 403, 404)},
        description="Las tareas vivas dentro del alcance de `tasks.view`, paginadas por creación "
        "(ADR-016). Los filtros se combinan y la página siguiente se pide con los mismos: `mine` "
        "(las mías), `status`, `overdue`, `due_from` y `due_to` (instantes) y los vínculos. 400: "
        "un filtro repetido o inválido.",
    ),
    post=extend_schema(
        operation_id="tasks_create",
        tags=["tasks"],
        request=inputs.TaskCreateSerializer,
        responses={201: TaskSerializer, **errors(400, 401, 403, 404)},
        description="Crea una tarea abierta de quien la crea (su responsable), con `tasks.create`. "
        "400 en `contact_id` u `opportunity_id` si no son de la organización, vivos y dentro del "
        "alcance de quien crea, o si la oportunidad no es del contacto (el mismo error que para "
        "uno que no existe), en `description` si lleva un carácter no imprimible (un tabulador) y "
        "por cada dato inválido.",
    ),
)
class TasksView(TasksQuery, generics.ListAPIView):
    required_permissions = {"GET": "tasks.view", "POST": "tasks.create"}
    serializer_class = TaskSerializer
    filter_backends = [ScopeFilter, TaskSearch]  # en este orden

    def post(self, request: Request, **kwargs: Any) -> Response:
        wanted = inputs.TaskCreateSerializer(data=request.data)
        wanted.is_valid(raise_exception=True)
        fields = dict(wanted.validated_data)
        with domain():
            if (contact := fields.get("contact_id")) is not None:
                _seen(request, "contacts.view", contacts(), pk=contact)
            if (opportunity := fields.get("opportunity_id")) is not None:
                match = {"pk": opportunity} | ({} if contact is None else {"contact_id": contact})
                _seen(request, "opportunities.view", deals.opportunities(), **match)
            made = commands.create_task(_tenant(), **fields)
        return reply(request, made.pk, 201)


@extend_schema_view(
    get=extend_schema(
        operation_id="tasks_retrieve",
        tags=["tasks"],
        responses={200: TaskSerializer, **errors(401, 403, 404)},
        description="Una tarea. 404: no es de la organización, está borrada o queda fuera del "
        "alcance de `tasks.view`.",
    ),
    patch=extend_schema(
        operation_id="tasks_update",
        tags=["tasks"],
        request=inputs.TaskUpdateSerializer,
        responses={200: TaskSerializer, **errors(400, 401, 403, 404, 409)},
        description="Cambia lo enviado de una tarea abierta si `version` sigue vigente; `status` "
        "solo `PENDING` o `IN_PROGRESS`. Con `tasks.view` y `tasks.update`; fuera del alcance de "
        "uno u otro: 404. 409: `STALE_VERSION`, `TASK_CLOSED`. No cambia el responsable ni los "
        "vínculos. Sin cambios no escribe ni sube `version`.",
    ),
    delete=extend_schema(
        operation_id="tasks_delete",
        tags=["tasks"],
        responses={204: None, **errors(401, 403, 404)},
        description="Borrado lógico, de una abierta o de una cerrada, sin versión. Con "
        "`tasks.view` y `tasks.update`; fuera del alcance, o ya borrada: 404 (no es idempotente).",
    ),
)
class TaskView(TasksQuery, generics.RetrieveAPIView):
    required_permissions = {
        "GET": "tasks.view",
        "PATCH": ("tasks.view", "tasks.update"),
        "DELETE": ("tasks.view", "tasks.update"),
    }
    serializer_class = TaskSerializer
    lookup_url_kwarg = "task_id"

    def get_queryset(self) -> QuerySet[Task]:
        found = super().get_queryset()
        return locked(found) if self.request.method in ("PATCH", "DELETE") else found

    def patch(self, request: Request, task_id: UUID, **kwargs: Any) -> Response:
        wanted = inputs.TaskUpdateSerializer(data=request.data)
        wanted.is_valid(raise_exception=True)
        fields = dict(wanted.validated_data)
        version, found = fields.pop("version"), self.get_object()
        with domain():
            commands.update_task(_tenant(), task_id=found.pk, version=version, **fields)
        return reply(request, found.pk)

    def delete(self, request: Request, task_id: UUID, **kwargs: Any) -> Response:
        found = self.get_object()
        with domain():
            commands.delete_task(_tenant(), task_id=found.pk)
        return Response(status=204)


class TaskWrite(TasksQuery, generics.GenericAPIView):
    """Una escritura sobre una tarea: la fila queda bloqueada al decidir el alcance."""

    serializer_class = TaskSerializer
    lookup_url_kwarg = "task_id"

    def get_queryset(self) -> QuerySet[Task]:
        return locked(super().get_queryset())


def action(
    name: str, command: Any, body: Any, permission: str, summary: str, errors_: tuple[int, ...]
) -> type[TaskWrite]:
    """Una transición `POST …/{id}/<name>/`: `tasks.view` y el permiso de la acción, sobre la fila
    bloqueada; responde con el detalle."""

    class Action(TaskWrite):
        required_permissions = {"POST": ("tasks.view", permission)}

        def post(self, request: Request, task_id: UUID, **kwargs: Any) -> Response:
            wanted = body(data=request.data)
            wanted.is_valid(raise_exception=True)
            found = self.get_object()
            with domain():
                command(_tenant(), task_id=found.pk, **wanted.validated_data)
            return reply(request, found.pk)

    Action.__name__ = f"Task{name.title()}View"
    view: type[TaskWrite] = extend_schema_view(
        post=extend_schema(
            operation_id=f"tasks_{name}",
            tags=["tasks"],
            request=body,
            responses={200: TaskSerializer, **errors(*errors_)},
            description=f"{summary} Con `tasks.view` y `{permission}`; fuera del alcance: 404.",
        )
    )(Action)
    return view


TaskAssignView = action(
    "assign", commands.assign_task, inputs.TaskAssignSerializer, "tasks.assign",
    "Deja la tarea abierta con ese responsable si `version` sigue vigente. 400 en "
    "`assigned_user_id` si no es un miembro activo; 409 `STALE_VERSION` o `TASK_CLOSED`. Si ya es "
    "el responsable no escribe.", (400, 401, 403, 404, 409),
)  # fmt: skip
TaskCompleteView = action(
    "complete", commands.complete_task, inputs.TaskVersionSerializer, "tasks.update",
    "Completa una tarea abierta: `DONE`, con su fecha y quien la completó. 409: `STALE_VERSION`, "
    "`TASK_CLOSED`.", (400, 401, 403, 404, 409),
)  # fmt: skip
TaskCancelView = action(
    "cancel", commands.cancel_task, inputs.TaskCancelSerializer, "tasks.update",
    "Cancela una tarea abierta, con un motivo opcional (texto libre). 409: `STALE_VERSION`, "
    "`TASK_CLOSED`.", (400, 401, 403, 404, 409),
)  # fmt: skip
TaskReopenView = action(
    "reopen", commands.reopen_task, inputs.TaskVersionSerializer, "tasks.update",
    "Devuelve una tarea completada o cancelada a `PENDING`, sin fecha ni motivo. 409: "
    "`STALE_VERSION`, `TASK_OPEN` (está abierta).", (400, 401, 403, 404, 409),
)  # fmt: skip
