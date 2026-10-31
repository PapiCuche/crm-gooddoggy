"""Rutas de tenant de los pipelines, sus etapas y los motivos de pérdida (F5-02, E09-05):
`…/pipelines/` y `…/lost-reasons/`.

Son configuración de la organización, no oportunidades: leerlos pide `opportunities.view` con
cualquier alcance (las políticas de `Pipeline`, `PipelineStage` y `LostReason` son `Everyone`: el
alcance decidirá qué oportunidades se ven, F5-03, no qué pipelines). Sin el permiso, 403.

Crear un pipeline (F5-02b) pide solo `pipelines.manage` (sin alcance), comprobado antes de leer
nada, y responde con el pipeline y sus etapas como la lectura: quien escribe puede no tener
`opportunities.view`. Estricto: un campo desconocido o de otro tipo es 400; los topes de tamaño los
pone `inputs`.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from uuid import UUID

from django.db.models import QuerySet
from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework import generics, serializers
from rest_framework.request import Request
from rest_framework.response import Response

from apps.deals import commands, selectors
from apps.deals.api import inputs
from apps.deals.models import LostReason, Pipeline, StageType
from apps.deals.seed import lock_pipelines
from core.api.errors import ApiError
from core.api.schema import errors
from core.tenancy import context
from core.tenancy.scope import require_scope

INVARIANT = "El pipeline lleva una etapa WON, al menos una LOST y al menos una OPEN, activas."
CODE_TAKEN = "Ya hay un pipeline con ese código."
FULL = "La organización ya tiene {} pipelines."
GENERIC = "Dato no admitido."


def _tenant() -> Any:
    tenant = context.current()
    assert tenant is not None  # noqa: S101 — `HasPermission` ya lo comprobó
    return tenant


@contextmanager
def _domain(stages: str = "") -> Iterator[None]:
    """Lo que lanzan los comandos, con la forma del contrato: un conflicto es un 409 con su código;
    un dato, un 400 en su campo (o sin campo, y nunca con el valor recibido). `stages`: el campo
    que lleva las etapas de un pipeline nuevo, donde caen los errores de una etapa."""
    try:
        yield
    except commands.StageInvariant:
        raise ApiError("PIPELINE_INVARIANT", 409, INVARIANT) from None
    except commands.CodeTaken:
        raise ApiError("CODE_TAKEN", 409, CODE_TAKEN) from None
    except commands.Invalid as error:
        field = stages if stages and error.field in commands.NEW_STAGE else error.field
        raise serializers.ValidationError({field: [inputs.sentence(error)]}) from None
    except ValueError:  # un comando rechazó algo que el serializador dejó pasar
        raise serializers.ValidationError(GENERIC) from None


def _reply(pipeline_id: UUID, status: int = 200) -> Response:
    """El pipeline con sus etapas, como lo lee `GET …/pipelines/`: quien escribe no tiene por qué
    poder leer (`opportunities.view`), así que la respuesta lleva el resultado."""
    return Response(
        PipelineSerializer(selectors.pipelines().get(pk=pipeline_id)).data, status=status
    )


class StageSerializer(serializers.Serializer[Any]):
    id = serializers.UUIDField()
    name = serializers.CharField()
    code = serializers.CharField(help_text="Clave estable dentro del pipeline. No cambia.")
    sort_order = serializers.IntegerField()
    stage_type = serializers.ChoiceField(choices=StageType.choices)
    default_probability = serializers.IntegerField(help_text="De 0 a 100; WON 100, LOST 0.")
    stale_after_days = serializers.IntegerField(allow_null=True, help_text="`null`: nunca.")
    color = serializers.CharField()
    requires_fields = serializers.DictField()
    is_active = serializers.BooleanField()


class PipelineSerializer(serializers.Serializer[Any]):
    id = serializers.UUIDField()
    name = serializers.CharField()
    code = serializers.CharField(help_text="Clave estable, única en la organización. No cambia.")
    kind = serializers.ChoiceField(choices=Pipeline.Kind.choices)
    currency = serializers.CharField()
    is_default = serializers.BooleanField()
    is_active = serializers.BooleanField()
    sort_order = serializers.IntegerField()
    version = serializers.IntegerField()
    stages = StageSerializer(many=True, help_text="Todas, también las inactivas, en su orden.")


class LostReasonSerializer(serializers.Serializer[Any]):
    id = serializers.UUIDField()
    name = serializers.CharField()
    code = serializers.CharField(help_text="`MAYÚSCULAS_Y_GUION_BAJO`, único. No cambia.")
    sort_order = serializers.IntegerField()
    is_active = serializers.BooleanField()
    version = serializers.IntegerField()


@extend_schema_view(
    get=extend_schema(
        operation_id="pipelines_list",
        tags=["deals"],
        responses={200: PipelineSerializer(many=True), **errors(400, 401, 403, 404)},
        description="Los pipelines de la organización, activos o no, por creación (ADR-016), "
        "cada uno con todas sus etapas en su orden (`sort_order`, nombre, id). Con "
        "`opportunities.view` y cualquier alcance.",
    ),
    post=extend_schema(
        operation_id="pipelines_create",
        tags=["deals"],
        request=inputs.PipelineCreateSerializer,
        responses={201: PipelineSerializer, **errors(400, 401, 403, 404, 409)},
        description="Crea un pipeline con sus etapas, de una vez. Con `pipelines.manage`. "
        f"Hasta {inputs.PIPELINES_MAX} pipelines por organización y {inputs.STAGES_MAX} etapas "
        "por pipeline (400). 409: `PIPELINE_INVARIANT` (sin una WON, una LOST o una OPEN), "
        "`CODE_TAKEN`.",
    ),
)
class PipelinesView(generics.ListAPIView):
    required_permissions = {"GET": "opportunities.view", "POST": "pipelines.manage"}
    serializer_class = PipelineSerializer

    def get_queryset(self) -> QuerySet[Pipeline]:
        return selectors.pipelines()

    def post(self, request: Request, **kwargs: Any) -> Response:
        wanted = inputs.PipelineCreateSerializer(data=request.data)
        wanted.is_valid(raise_exception=True)
        tenant = _tenant()
        with _domain(stages="stages"):
            # El tope se cuenta con el bloqueo de la organización que el comando vuelve a tomar.
            lock_pipelines(require_scope(tenant), tenant.organization_id)
            if selectors.pipeline_count() >= inputs.PIPELINES_MAX:
                raise serializers.ValidationError(FULL.format(inputs.PIPELINES_MAX))
            made = commands.create_pipeline(tenant, **wanted.validated_data)
        return _reply(made.pk, 201)


@extend_schema_view(
    get=extend_schema(
        operation_id="lost_reasons_list",
        tags=["deals"],
        responses={200: LostReasonSerializer(many=True), **errors(400, 401, 403, 404)},
        description="Los motivos de pérdida, activos o no, por creación (ADR-016); `sort_order` "
        "es el que muestra la pantalla. Con `opportunities.view` y cualquier alcance.",
    ),
)
class LostReasonsView(generics.ListAPIView):
    required_permissions = {"GET": "opportunities.view"}
    serializer_class = LostReasonSerializer

    def get_queryset(self) -> QuerySet[LostReason]:
        return selectors.lost_reasons()
