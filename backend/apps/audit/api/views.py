"""Ruta de tenant de `audit`: la auditoría de la organización, para leer (F2-73) y filtrar
(F2-74). E01-13."""

import re
from collections.abc import Callable
from typing import Any

from django.db.models import QuerySet
from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework import generics, serializers
from rest_framework.exceptions import ValidationError

from apps.audit.models import ACTOR_TYPES, AuditLog
from apps.audit.selectors import entries
from apps.audit.services import Result
from core.api.schema import errors
from core.outbox import NAME, TYPE


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


def _shaped(pattern: re.Pattern[str]) -> Callable[[str], None]:
    """La forma que `record()` exige al escribir: lo que no la tiene no puede estar guardado."""

    def check(value: str) -> None:
        if not pattern.fullmatch(value):
            raise ValidationError("No tiene la forma de este campo.")

    return check


class AuditFilterSerializer(serializers.Serializer[Any]):
    """Los filtros del listado (F2-74): por valor exacto, y se cumplen todos los que se envían."""

    actor_type = serializers.ChoiceField(choices=ACTOR_TYPES, required=False)
    actor_id = serializers.UUIDField(required=False, help_text="Quién lo hizo.")
    action = serializers.CharField(
        required=False,
        max_length=100,
        trim_whitespace=False,
        validators=[_shaped(NAME)],
        help_text="La acción exacta, por ejemplo `role.created`.",
    )
    entity_type = serializers.CharField(
        required=False, max_length=50, trim_whitespace=False, validators=[_shaped(TYPE)]
    )
    entity_id = serializers.UUIDField(required=False, help_text="La historia de una entidad.")


@extend_schema_view(
    get=extend_schema(
        operation_id="audit_list",
        tags=["audit"],
        parameters=[AuditFilterSerializer],
        responses={200: AuditEntrySerializer(many=True), **errors(400, 401, 403, 404)},
    )
)
class AuditLogView(generics.ListAPIView[AuditLog]):
    """La auditoría de la organización, de la fila más reciente a la más antigua. Paginada por
    cursor (ADR-016). Cada fila llega como se guardó: los valores con aspecto de secreto se
    redactaron al escribirla (ADR-011). Solo lectura. Los filtros son por valor exacto y se
    cumplen todos; la página siguiente se pide con los mismos. 400: un filtro con un valor que
    ese campo no puede tener, o repetido."""

    required_permissions = {"GET": "audit.view"}
    serializer_class = AuditEntrySerializer
    ordering = "-id"  # UUIDv7: el orden en que se escribieron

    def get_queryset(self) -> QuerySet[AuditLog]:
        params = self.request.query_params
        # Un diccionario corriente, no el de la consulta: con este, DRF daría un filtro vacío
        # (`?action=`) por no enviado y devolvería la lista sin filtrar.
        names = [name for name in AuditFilterSerializer().fields if name in params]
        filters = AuditFilterSerializer(data={name: params[name] for name in names})
        filters.is_valid(raise_exception=True)
        # Un filtro repetido no elige uno de sus valores en silencio: es un error del cliente.
        twice = {name: ["Una sola vez."] for name in names if len(params.getlist(name)) > 1}
        if twice:
            raise ValidationError(twice)
        return entries().filter(**filters.validated_data)
