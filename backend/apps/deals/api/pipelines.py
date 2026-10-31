"""Rutas de tenant de los pipelines, sus etapas y los motivos de pérdida (F5-02, E09-05):
`…/pipelines/` y `…/lost-reasons/`.

Son configuración de la organización, no oportunidades: leerlos pide `opportunities.view` con
cualquier alcance (las políticas de `Pipeline`, `PipelineStage` y `LostReason` son `Everyone`: el
alcance decidirá qué oportunidades se ven, F5-03, no qué pipelines). Sin el permiso, 403.
"""

from typing import Any

from django.db.models import QuerySet
from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework import generics, serializers

from apps.deals import selectors
from apps.deals.models import LostReason, Pipeline, StageType
from core.api.schema import errors


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
)
class PipelinesView(generics.ListAPIView):
    required_permissions = {"GET": "opportunities.view"}
    serializer_class = PipelineSerializer

    def get_queryset(self) -> QuerySet[Pipeline]:
        return selectors.pipelines()


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
