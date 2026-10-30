"""`?q=` y `?ids=` de los listados del catálogo (F3-48i): productos, categorías, marcas y la
búsqueda de variantes. Como la búsqueda de contactos (F4-07): una línea de texto recortada de
hasta 254 caracteres, sin repetir lo recibido en el error, y detrás de la paginación por cursor."""

from typing import Any
from uuid import UUID

from rest_framework import serializers
from rest_framework.filters import BaseFilterBackend

from apps.catalog.selectors import only_ids, search
from core.text import line

Q_MAX = 254
IDS_MAX = 50


class CatalogSearchSerializer(serializers.Serializer[Any]):
    q = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=Q_MAX,
        help_text=f"El texto que contiene, sin distinguir mayúsculas (hasta {Q_MAX} caracteres, "
        "una línea; `%`, `_` y `\\` son literales). Vacío: sin filtro.",
    )
    ids = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=IDS_MAX * 37,
        help_text=f"Hasta {IDS_MAX} identificadores UUID separados por comas, para resolver "
        "varios en una llamada. Los que no existen, son de otra organización o están borrados "
        "no aparecen.",
    )

    def validate_q(self, value: str) -> str:
        try:
            return line(value, Q_MAX)
        except ValueError as error:
            text = f"{error}."
            raise serializers.ValidationError(text[:1].upper() + text[1:]) from None

    def validate_ids(self, value: str) -> list[UUID]:
        parts = [part.strip() for part in value.split(",")]
        if len(parts) > IDS_MAX:
            raise serializers.ValidationError(f"Hasta {IDS_MAX} identificadores.")
        try:
            return [UUID(part) for part in parts]
        except ValueError:
            raise serializers.ValidationError("Identificadores UUID separados por comas.") from None


class CatalogSearch(BaseFilterBackend):
    """`?q=` y `?ids=` sobre `view.search_fields`; se combinan. Con `view.search_required`, uno de
    los dos es obligatorio: no hay listado completo entre productos."""

    def filter_queryset(self, request: Any, queryset: Any, view: Any) -> Any:
        params = request.query_params
        names = ("q", "ids")
        if repeated := [k for k in names if len(params.getlist(k)) > 1]:  # no se elige uno
            raise serializers.ValidationError({k: ["Una sola vez."] for k in repeated})
        wanted = CatalogSearchSerializer(data={k: params[k] for k in names if k in params})
        wanted.is_valid(raise_exception=True)
        text, ids = wanted.validated_data.get("q", ""), wanted.validated_data.get("ids")
        if getattr(view, "search_required", False) and not text and ids is None:
            raise serializers.ValidationError({"q": ["Envía `q` o `ids`."]})
        found = search(queryset, text, view.search_fields)
        return found if ids is None else only_ids(found, ids)
