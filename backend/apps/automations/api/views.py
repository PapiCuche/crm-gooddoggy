"""Rutas de tenant de las automatizaciones (F11-07; ADR-049 §7, §8): leer las reglas y el
interruptor (`automations.view`), cambiar el modo y los parámetros de una regla
(`automations.manage`), pausar (`automations.kill_switch` o `automations.manage`), reanudar
(`automations.manage`). La organización es la de la ruta; el cuerpo solo aporta `version`, `mode`
y `params` (cualquier otro campo se ignora). `global_enabled` (`AUTOMATIONS_ENABLED`) solo se lee:
ninguna ruta lo enciende. Nada de aquí crea tareas ni mensajes ni cambia otros módulos.

El historial (F11-07b, `automations.view`) muestra el id y el número de la conversación o de la
cotización solo si `conversations.view` o `quotes.view` de quien consulta, con su alcance, la cubre;
si no, «sin acceso»: sin id ni número. Nunca el texto ni el nombre del cliente ni importes.

Pendiente: `PATCH` y pausar responden con el estado completo también a un rol propio con
`automations.manage` o `automations.kill_switch` sin `automations.view`."""

import datetime as dt
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from django.db.models import Case, CharField, OuterRef, QuerySet, Subquery, When
from drf_spectacular.utils import extend_schema, extend_schema_field, extend_schema_view
from rest_framework import generics, serializers
from rest_framework.exceptions import NotFound
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.access.permissions import AnyOf, rbac_errors, request_context
from apps.access.selectors import scoped
from apps.automations import commands
from apps.automations import params as schema
from apps.automations.models import AutomationRule, AutomationRun, AutomationSwitch, Mode, RunStatus
from apps.conversations.models import Conversation
from apps.quotes.models import Quote
from core.api.errors import ApiError
from core.api.schema import errors
from core.tenancy import context

STALE = "Otra persona cambió la regla: vuelve a cargarla y repite el cambio."
TOO_SHORT = "Antes de ON, la regla tiene que llevar al menos 7 días seguidos en SIMULATE."
OFF_GLOBALLY = " Con `global_enabled = false` nada se ejecuta, esté la regla como esté."
STOPPERS = AnyOf((commands.KILL, commands.MANAGE))
CHOICE = {"invalid_choice": "OFF, SIMULATE u ON."}  # sin repetir lo enviado


class AutomationRuleSerializer(serializers.Serializer[Any]):
    key = serializers.ChoiceField(choices=schema.KEYS)
    mode = serializers.ChoiceField(choices=Mode.choices)
    params = serializers.DictField(help_text="Todos los parámetros, con los valores por omisión.")
    version = serializers.IntegerField(help_text="0: todavía sin configurar. Sube con cada cambio.")
    simulate_since = serializers.DateTimeField(allow_null=True, help_text="Entró en `SIMULATE`.")
    on_allowed_at = serializers.SerializerMethodField(
        help_text="Desde cuándo puede pasar a `ON` (7 días en `SIMULATE`); null fuera de él."
    )
    updated_at = serializers.DateTimeField(allow_null=True, help_text="null: sin configurar.")

    def get_on_allowed_at(self, rule: AutomationRule) -> dt.datetime | None:
        since = rule.simulate_since if rule.mode == Mode.SIMULATE else None
        return None if since is None else since + commands.SIMULATE_FOR


class AutomationSwitchSerializer(serializers.Serializer[Any]):
    global_enabled = serializers.BooleanField(
        help_text="`AUTOMATIONS_ENABLED`: apagado, no se ejecuta ninguna regla. Solo lectura."
    )
    paused = serializers.BooleanField()
    paused_changed_at = serializers.DateTimeField(allow_null=True, help_text="null: nunca.")


class AutomationsSerializer(AutomationSwitchSerializer):
    rules = AutomationRuleSerializer(many=True)


class AutomationRuleWriteSerializer(serializers.Serializer[Any]):
    version = serializers.IntegerField(min_value=0, max_value=2**31 - 1)
    mode = serializers.ChoiceField(choices=Mode.choices, required=False, error_messages=CHOICE)
    params = serializers.DictField(
        required=False, help_text="Solo los que cambian; una clave fuera del esquema: 400."
    )


def switch_state() -> dict[str, Any]:
    row = AutomationSwitch.objects.first()  # sin fila, no está pausada
    return {"global_enabled": commands.globally_on(), "paused": bool(row and row.paused),
            "paused_changed_at": row.changed_at if row else None}  # fmt: skip


def settings_state() -> Any:
    """Lo que responden la lectura y el cambio de una regla, con `global_enabled` a la vista. Sin
    fila, una regla está en `OFF` con versión 0 (la que `set_rule` espera para crearla)."""
    stored = {rule.key: rule for rule in AutomationRule.objects.filter(key__in=schema.KEYS)}
    rules = [stored.get(key) or AutomationRule(key=key, version=0) for key in schema.KEYS]
    for rule in rules:
        rule.params = {**schema.defaults(rule.key), **rule.params}
    return AutomationsSerializer(switch_state() | {"rules": rules}).data


def _tenant() -> Any:
    tenant = context.current()
    assert tenant is not None  # noqa: S101 — `HasPermission` ya lo comprobó
    return tenant


@contextmanager
def domain() -> Iterator[None]:
    """Lo que lanza `set_rule`, con la forma del contrato y sin repetir lo enviado."""
    try:
        with rbac_errors():
            yield
    except commands.StaleVersion:
        raise ApiError("STALE_VERSION", 409, STALE) from None
    except commands.NotSimulatedLongEnough:
        raise ApiError("SIMULATION_TOO_SHORT", 409, TOO_SHORT) from None
    except commands.InvalidParams as error:  # el nombre del parámetro, nunca su valor; otro
        raise serializers.ValidationError({"params": [str(error)]}) from None  # error: 500


@extend_schema_view(
    get=extend_schema(
        operation_id="automations_retrieve", tags=["automations"],
        responses={200: AutomationsSerializer, **errors(401, 403, 404)},
        description="Las reglas registradas (sin configurar: `OFF`, versión 0), el interruptor de "
        "la organización y `global_enabled`, con `automations.view`." + OFF_GLOBALLY,
    ),
)  # fmt: skip
class AutomationsView(APIView):
    required_permissions = {"GET": commands.VIEW}

    def get(self, request: Request, **kwargs: Any) -> Response:
        return Response(settings_state())


@extend_schema_view(
    patch=extend_schema(
        operation_id="automations_rule_update", tags=["automations"],
        request=AutomationRuleWriteSerializer,
        responses={200: AutomationsSerializer, **errors(400, 401, 403, 404, 409)},
        description="Cambia el modo y los parámetros enviados de una regla si `version` sigue "
        "vigente (0: sin configurar), con `automations.manage`, y lo audita. 400 en `params` con "
        "una clave fuera del esquema o un valor fuera de rango. 404: no es una regla registrada. "
        "409: `STALE_VERSION`; `SIMULATION_TOO_SHORT` si pide `ON` sin 7 días seguidos en "
        "`SIMULATE` (reloj de la base): no cambia nada. Sin cambios no escribe. Responde como "
        "`GET …/automations/`." + OFF_GLOBALLY,
    ),
)  # fmt: skip
class RuleView(APIView):
    required_permissions = {"PATCH": commands.MANAGE}

    def patch(self, request: Request, key: str, **kwargs: Any) -> Response:
        if key not in schema.KEYS:
            raise NotFound
        wanted = AutomationRuleWriteSerializer(data=request.data)
        wanted.is_valid(raise_exception=True)
        with domain():
            commands.set_rule(_tenant(), key=key, **wanted.validated_data)
        return Response(settings_state())


def switch_view(name: str, command: Any, permission: Any, summary: str) -> type[APIView]:
    class Switch(APIView):
        required_permissions = {"POST": permission}

        def post(self, request: Request, **kwargs: Any) -> Response:
            with rbac_errors():
                command(_tenant())
            return Response(AutomationSwitchSerializer(switch_state()).data)

    Switch.__name__ = f"{name.title()}View"
    view: type[APIView] = extend_schema_view(
        post=extend_schema(
            operation_id=f"automations_{name}", tags=["automations"], request=None,
            responses={200: AutomationSwitchSerializer, **errors(401, 403, 404)},
            description=summary + " Repetirlo no cambia nada. No toca `global_enabled`.",
        )
    )(Switch)  # fmt: skip
    return view


PauseView = switch_view(
    "pause", commands.pause, STOPPERS, "Pausa todas las reglas de la organización sin tocar su "
    "configuración (`automations.kill_switch` o `automations.manage`) y lo audita.",
)  # fmt: skip
ResumeView = switch_view(
    "resume", commands.resume, commands.MANAGE,
    "Reanuda la organización (`automations.manage`) y lo audita.",
)  # fmt: skip


class AutomationSubjectSerializer(serializers.Serializer[Any]):
    type = serializers.CharField(source="subject_type", help_text="`conversation` o `quote`.")
    id = serializers.SerializerMethodField(help_text="null: sin acceso.")
    number = serializers.CharField(source="subject_number", allow_null=True)
    visible = serializers.SerializerMethodField(help_text="false: «sin acceso».")

    @extend_schema_field(serializers.UUIDField(allow_null=True))
    def get_id(self, run: Any) -> str | None:  # el id, solo junto al número que el alcance da
        return str(run.subject_id) if run.subject_number is not None else None

    def get_visible(self, run: Any) -> bool:
        return run.subject_number is not None


class AutomationRunSerializer(serializers.Serializer[Any]):
    id = serializers.UUIDField()
    rule_key = serializers.CharField()
    rule_version = serializers.IntegerField()
    subject = AutomationSubjectSerializer(source="*")
    episode_at = serializers.DateTimeField()
    step = serializers.IntegerField(help_text="1: aviso; 2: escalado.")
    mode = serializers.ChoiceField(choices=Mode.choices)
    status = serializers.ChoiceField(choices=RunStatus.choices)
    reason = serializers.CharField(allow_null=True, help_text="Un código cerrado.")
    recipients = serializers.ListField(child=serializers.UUIDField(), help_text="Ids de miembro.")
    task_ids = serializers.ListField(child=serializers.UUIDField())
    attempts = serializers.IntegerField()
    created_at = serializers.DateTimeField()
    closed_at = serializers.DateTimeField(allow_null=True)


def with_subject(found: QuerySet[AutomationRun], request: Any) -> QuerySet[AutomationRun]:
    """`subject_number`: el número del sujeto si `scoped()` de `conversations.view` o `quotes.view`
    de quien consulta lo deja ver (la misma organización y su alcance, como `ScopeFilter` en sus
    listas, que no ocultan filas con `hidden_unless`), en la misma consulta; si no, `NULL`."""
    ectx = request_context(request)
    assert ectx is not None  # noqa: S101 — `HasPermission` ya lo comprobó
    same = {"pk": OuterRef("subject_id")}
    chats = scoped(ectx, "conversations.view", Conversation.objects.filter(**same))
    quotes = scoped(ectx, "quotes.view", Quote.objects.filter(**same))
    number = Case(
        When(subject_type="conversation", then=Subquery(chats.values("number")[:1])),
        When(subject_type="quote", then=Subquery(quotes.values("number")[:1])),
        output_field=CharField(),
    )
    return found.annotate(subject_number=number)


@extend_schema_view(
    get=extend_schema(
        operation_id="automations_runs_list", tags=["automations"],
        responses={200: AutomationRunSerializer(many=True), **errors(400, 401, 403, 404)},
        description="El historial de ejecuciones, lo más reciente primero (ADR-016), con "
        "`automations.view`. El sujeto lleva su id y su número solo si `conversations.view` o "
        "`quotes.view` de quien consulta lo cubre; si no, `visible = false` y ninguno de los dos.",
    ),
)  # fmt: skip
class RunsView(generics.ListAPIView):
    required_permissions = {"GET": commands.VIEW}
    serializer_class = AutomationRunSerializer  # `ScopeFilter` por omisión: sin alcance
    ordering = "-id"  # UUIDv7: el orden en que se crearon

    def get_queryset(self) -> QuerySet[AutomationRun]:
        found = AutomationRun.objects.all()
        request = getattr(self, "request", None)
        return found if request is None else with_subject(found, request)  # OpenAPI: sin él
