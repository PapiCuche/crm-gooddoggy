"""Ruta de tenant de `audit`: la auditoría de la organización, para leer (F2-73, E01-13)."""

from typing import Any

from django.db.models import QuerySet
from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework import generics, serializers

from apps.audit.models import ACTOR_TYPES, AuditLog
from apps.audit.selectors import entries
from apps.audit.services import Result
from core.api.schema import errors


class AuditEntrySerializer(serializers.Serializer[Any]):
    id = serializers.UUIDField()
    occurred_at = serializers.DateTimeField(help_text="Cuándo ocurrió, según la base de datos.")
    actor_type = serializers.ChoiceField(choices=ACTOR_TYPES)
    actor_id = serializers.UUIDField(
        allow_null=True, help_text="Quién lo hizo: el usuario, si `actor_type` es `USER`."
    )
    actor_label = serializers.CharField(allow_null=True)
    action = serializers.CharField(help_text="`modulo.accion`, por ejemplo `branch.updated`.")
    entity_type = serializers.CharField()
    entity_id = serializers.UUIDField(allow_null=True)
    entity_label = serializers.CharField(
        allow_null=True, help_text="Cómo se llamaba la entidad en ese momento."
    )
    changes = serializers.DictField(help_text="Por campo, `[antes, después]`. Ya redactado.")
    metadata = serializers.DictField(help_text="Datos de contexto de la acción. Ya redactado.")
    result = serializers.ChoiceField(choices=[result.value for result in Result])
    correlation_id = serializers.CharField(
        allow_null=True, help_text="Común a las filas de una misma operación."
    )


@extend_schema_view(
    get=extend_schema(
        operation_id="audit_list",
        tags=["audit"],
        responses={200: AuditEntrySerializer(many=True), **errors(400, 401, 403, 404)},
    )
)
class AuditLogView(generics.ListAPIView[AuditLog]):
    """La auditoría de la organización, de la fila más reciente a la más antigua. Paginada por
    cursor (ADR-016). Cada fila llega como se guardó: los valores con aspecto de secreto se
    redactaron al escribirla (ADR-011). Solo lectura."""

    required_permissions = {"GET": "audit.view"}
    serializer_class = AuditEntrySerializer
    ordering = "-id"  # UUIDv7: el orden en que se escribieron

    def get_queryset(self) -> QuerySet[AuditLog]:
        return entries()
