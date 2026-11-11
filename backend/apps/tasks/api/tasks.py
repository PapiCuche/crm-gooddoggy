"""Rutas de tenant de las tareas: `…/tasks/` (F5-08b, E10). Lectura: la lista y el detalle, con
`tasks.view`, que admite alcance (por responsable; `TEAM` incluye a los compañeros de equipo):
`ScopeFilter` deja en el listado y en `get_object()` solo las que la política permite. Fuera del
alcance la tarea no existe, igual que la de otra organización o una borrada: 404.

El contacto sale con `id` y `display_name`, y el nombre solo si quien consulta puede ver ese
contacto (`contacts.view` y su alcance); si no, `display_name` es `null`: tener las tareas de otro
no da sus clientes. Los filtros de la lista no usan la zona horaria: «de hoy» o «próximas» son
`due_from` y `due_to` (instantes) que calcula el cliente, y «vencidas» es `overdue`."""

from typing import Any

from django.db.models import OuterRef, QuerySet, Subquery
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework import generics, serializers
from rest_framework.filters import BaseFilterBackend

from apps.access.permissions import ScopeFilter, request_context
from apps.access.selectors import scoped
from apps.contacts.selectors import contacts
from apps.tasks import selectors
from apps.tasks.models import Task
from core.api.schema import errors
from core.tenancy import context


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


def _me() -> Any:
    tenant = context.current()
    assert tenant is not None  # noqa: S101 — `HasPermission` ya lo comprobó
    return tenant.user_id


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
            found = found.filter(assigned_user_id=_me())
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
)
class TasksView(TasksQuery, generics.ListAPIView):
    required_permissions = {"GET": "tasks.view"}
    serializer_class = TaskSerializer
    filter_backends = [ScopeFilter, TaskSearch]  # en este orden


@extend_schema_view(
    get=extend_schema(
        operation_id="tasks_retrieve",
        tags=["tasks"],
        responses={200: TaskSerializer, **errors(401, 403, 404)},
        description="Una tarea. 404: no es de la organización, está borrada o queda fuera del "
        "alcance de `tasks.view`.",
    ),
)
class TaskView(TasksQuery, generics.RetrieveAPIView):
    required_permissions = {"GET": "tasks.view"}
    serializer_class = TaskSerializer
    lookup_url_kwarg = "task_id"
