"""Rutas de tenant de la importación de stock: `…/inventory/imports/` (F3-20, E04-07).

El archivo llega en base64 dentro del JSON: la API solo lee JSON (ADR-014), y así el tope de
tamaño se mira en `Content-Length` antes de interpretar el cuerpo."""

import base64
import binascii
from typing import Any
from uuid import UUID

from django.db.models import QuerySet
from django.http import HttpResponse
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework import generics, serializers
from rest_framework.exceptions import NotFound
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.imports import csvfile
from apps.imports.models import DataImport, DataImportRow
from apps.inventory import imports
from apps.inventory.api.views import _tenant
from apps.inventory.selectors import stock_import_rows, stock_imports
from core.api.errors import ApiError
from core.api.schema import errors

BODY_MAX = csvfile.MAX_BYTES * 4 // 3 + 4096  # el archivo en base64 y el resto del JSON
TOO_BIG = f"El archivo pasa de {csvfile.MAX_BYTES // 1024} KB: divídelo."
CSV: dict[Any, Any] = {(200, "text/csv"): OpenApiTypes.BINARY}
NOT_APPLIED = "Solo se revierte una importación aplicada."
PERMISSION = "inventory.adjust"  # un lote de ajustes; `products.import` es del catálogo


class StockImportResultSerializer(serializers.Serializer[Any]):
    applied = serializers.IntegerField(help_text="Filas con su ajuste en el ledger.")
    unchanged = serializers.IntegerField()
    stale = serializers.IntegerField(help_text="Celdas que cambiaron desde la previsualización.")
    failed = serializers.IntegerField(help_text="Filas válidas que fallaron al aplicar.")


class StockImportRevertResultSerializer(serializers.Serializer[Any]):
    reverted = serializers.IntegerField(help_text="Celdas devueltas al saldo de antes.")
    stale = serializers.IntegerField(help_text="Celdas que cambiaron después: no se tocan.")
    failed = serializers.IntegerField()


class StockImportSerializer(serializers.Serializer[Any]):
    id = serializers.UUIDField()
    status = serializers.ChoiceField(choices=DataImport.Status.choices)
    file_name = serializers.CharField()
    total_rows = serializers.IntegerField(help_text="Las líneas con texto, sin la cabecera.")
    valid_rows = serializers.IntegerField()
    error_rows = serializers.IntegerField()
    new_count = serializers.IntegerField(help_text="Celdas que aún no tenían saldo.")
    update_count = serializers.IntegerField(help_text="Celdas cuyo saldo cambiaría.")
    unchanged_count = serializers.IntegerField(source="summary.unchanged")
    duplicate_of = serializers.UUIDField(
        source="summary.duplicate_of",
        allow_null=True,
        help_text="Otra importación de stock con el mismo archivo. Solo es un aviso.",
    )
    encoding = serializers.CharField(
        source="summary.encoding", help_text="utf-8 o windows-1252 (CSV), o xlsx."
    )
    ignored_columns = serializers.ListField(
        child=serializers.CharField(), source="summary.ignored_columns"
    )
    created_at = serializers.DateTimeField()
    applied_at = serializers.DateTimeField(allow_null=True)
    result = StockImportResultSerializer(
        source="summary.result", allow_null=True, help_text="Al aplicarla."
    )
    revert = StockImportRevertResultSerializer(
        source="summary.revert", allow_null=True, help_text="Al revertirla."
    )


class StockImportRowSerializer(serializers.Serializer[Any]):
    id = serializers.UUIDField()
    row_number = serializers.IntegerField(help_text="La línea del archivo (la cabecera es la 1).")
    action = serializers.ChoiceField(choices=DataImportRow.Action.choices)
    sku = serializers.CharField(source="raw.sku", help_text="Tal como vino.")
    almacen = serializers.CharField(source="raw.almacen")
    cantidad = serializers.CharField(source="raw.cantidad")
    quantity = serializers.IntegerField(source="normalized.quantity", allow_null=True)
    on_hand_seen = serializers.IntegerField(
        source="normalized.on_hand_seen",
        allow_null=True,
        help_text="El saldo al previsualizar: aplicar no pisa una celda que cambió.",
    )
    errors = serializers.ListField(child=serializers.CharField())


class StockImportCreateSerializer(serializers.Serializer[Any]):
    file_name = serializers.CharField(max_length=imports.NAME_MAX)
    content = serializers.CharField(
        max_length=BODY_MAX, trim_whitespace=False, help_text="El archivo CSV o XLSX, en base64."
    )

    def validate_content(self, value: str) -> bytes:
        try:
            return base64.b64decode(value, validate=True)
        except binascii.Error:
            raise serializers.ValidationError("No es base64.") from None


class RowFilterSerializer(serializers.Serializer[Any]):
    action = serializers.ChoiceField(
        choices=DataImportRow.Action.choices, required=False, help_text="Solo las de esa acción."
    )


def _found(import_id: UUID) -> DataImport:
    found: DataImport | None = stock_imports().filter(pk=import_id).first()
    if found is None:
        raise NotFound
    return found


class TemplateView(APIView):
    required_permissions = {"GET": PERMISSION}

    @extend_schema(
        operation_id="inventory_imports_template",
        tags=["inventory"],
        responses={**CSV, **errors(401, 403, 404)},
        description="La plantilla CSV: `sku`, `almacen` (el código) y `cantidad` (lo que hay, "
        "no lo que entra).",
    )
    def get(self, request: Request, **kwargs: Any) -> HttpResponse:
        disposition = 'attachment; filename="plantilla-stock.csv"'
        headers = {"Content-Disposition": disposition}
        return HttpResponse(imports.template(), "text/csv; charset=utf-8", headers=headers)


@extend_schema_view(
    post=extend_schema(
        operation_id="inventory_imports_create",
        tags=["inventory"],
        request=StockImportCreateSerializer,
        responses={201: StockImportSerializer, **errors(400, 401, 403, 404)},
        description="Sube un CSV o un XLSX de cantidades absolutas y lo previsualiza: valida "
        "cada fila contra el stock de ahora y no lo cambia. Hasta 1 MiB y 1000 filas. CSV en "
        "UTF-8 (o el de Excel en Windows), separador `,`, `;` o tabulador; XLSX (por su firma, "
        "no por el nombre): la primera hoja, sin fórmulas. 400 en `content` si el archivo "
        "entero no se puede leer.",
    )
)
class StockImportsView(APIView):
    required_permissions = {"POST": PERMISSION}

    def post(self, request: Request, **kwargs: Any) -> Response:
        length = request.META.get("CONTENT_LENGTH") or ""
        if not length.isdigit() or int(length) > BODY_MAX:  # sin longitud no se lee el cuerpo
            raise serializers.ValidationError({"content": [TOO_BIG]})
        wanted = StockImportCreateSerializer(data=request.data)
        wanted.is_valid(raise_exception=True)
        try:
            data = wanted.validated_data
            job = imports.preview(_tenant(), file_name=data["file_name"], data=data["content"])
        except csvfile.Unreadable as error:
            raise serializers.ValidationError({"content": [str(error)]}) from None
        return Response(StockImportSerializer(job).data, status=201)


@extend_schema_view(
    get=extend_schema(
        operation_id="inventory_imports_rows_list",
        tags=["inventory"],
        parameters=[RowFilterSerializer],
        responses={200: StockImportRowSerializer(many=True), **errors(400, 401, 403, 404)},
        description="Las filas de una importación, en el orden del archivo (ADR-016).",
    )
)
class StockImportRowsView(generics.ListAPIView):
    required_permissions = {"GET": PERMISSION}
    serializer_class = StockImportRowSerializer

    def get_queryset(self) -> QuerySet[DataImportRow]:
        wanted = RowFilterSerializer(data=self.request.query_params.dict())
        wanted.is_valid(raise_exception=True)
        rows = stock_import_rows(_found(self.kwargs["import_id"]).pk)
        return rows.filter(**wanted.validated_data)


class StockImportApplyView(APIView):
    required_permissions = {"POST": PERMISSION}

    @extend_schema(
        operation_id="inventory_imports_apply",
        tags=["inventory"],
        request=None,
        responses={200: StockImportSerializer, **errors(401, 403, 404)},
        description="Confirma la importación: cada fila válida deja su celda con la cantidad del "
        "archivo (un ajuste en el ledger). Una celda que cambió desde la previsualización queda "
        "`STALE` y no se toca. Aplicar otra vez no cambia nada.",
    )
    def post(self, request: Request, import_id: UUID, **kwargs: Any) -> Response:
        try:
            job = imports.apply(_tenant(), import_id=import_id)
        except DataImport.DoesNotExist:
            raise NotFound from None
        return Response(StockImportSerializer(job).data)


class StockImportRevertSerializer(serializers.Serializer[Any]):
    confirm = serializers.BooleanField(help_text="`true`: lo que confirma quien revierte.")

    def validate_confirm(self, value: bool) -> bool:
        if not value:
            raise serializers.ValidationError("Confirma la reversión.")
        return value


class StockImportRevertView(APIView):
    required_permissions = {"POST": PERMISSION}

    @extend_schema(
        operation_id="inventory_imports_revert",
        tags=["inventory"],
        request=StockImportRevertSerializer,
        responses={200: StockImportSerializer, **errors(400, 401, 403, 404, 409)},
        description="Revierte una importación aplicada: cada celda que movió vuelve al saldo de "
        "antes con otro ajuste en el ledger, que no se edita. Una celda con algún movimiento "
        "después de la importación (aunque vuelva al mismo saldo) queda `STALE` y no se toca. "
        "Revertir otra vez no cambia nada. 409 `IMPORT_NOT_APPLIED` si no está aplicada.",
    )
    def post(self, request: Request, import_id: UUID, **kwargs: Any) -> Response:
        StockImportRevertSerializer(data=request.data).is_valid(raise_exception=True)
        try:
            job = imports.revert(_tenant(), import_id=import_id)
        except DataImport.DoesNotExist:
            raise NotFound from None
        except imports.NotApplied:
            raise ApiError("IMPORT_NOT_APPLIED", 409, NOT_APPLIED) from None
        return Response(StockImportSerializer(job).data)


class StockImportErrorsView(APIView):
    required_permissions = {"GET": PERMISSION}

    @extend_schema(
        operation_id="inventory_imports_errors",
        tags=["inventory"],
        responses={**CSV, **errors(401, 403, 404)},
        description="Las filas con `ERROR` o `STALE`, en CSV, con sus errores.",
    )
    def get(self, request: Request, import_id: UUID, **kwargs: Any) -> HttpResponse:
        wrong = (DataImportRow.Action.ERROR, DataImportRow.Action.STALE)
        rows = stock_import_rows(_found(import_id).pk).filter(action__in=wrong).order_by("id")
        report = csvfile.write(
            ["linea", *imports.COLUMNS, "accion", "errores"],
            (
                [r.row_number, *map(r.raw.get, imports.COLUMNS), r.action, " | ".join(r.errors)]
                for r in rows
            ),
        )
        headers = {"Content-Disposition": 'attachment; filename="errores-stock.csv"'}
        return HttpResponse(report, "text/csv; charset=utf-8", headers=headers)
