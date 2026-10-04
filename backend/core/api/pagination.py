"""Paginación por cursor de los listados de la API (ADR-016).

Es la clase por defecto de DRF: ningún listado sale entero. El cursor solo codifica una
posición en el orden. El filtro de tenant y de alcance (`ScopeFilter`) ya se aplicó al queryset
que llega aquí, así que el cursor de otra organización no puede traer filas ajenas.
"""

from typing import Any
from urllib.parse import parse_qs, urlsplit

from django.core.exceptions import ImproperlyConfigured
from django.core.exceptions import ValidationError as InvalidValue
from rest_framework import pagination
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.response import Response

LIMIT, CURSOR = "limit", "cursor"


class CursorPagination(pagination.CursorPagination):
    page_size = 50
    max_page_size = 200
    page_size_query_param = LIMIT
    cursor_query_param = CURSOR
    ordering = "id"  # UUIDv7 (ADR-004): orden de creación, único y estable

    def get_page_size(self, request: Any) -> int:
        """Fuera de rango es un error del cliente: no se recorta en silencio."""
        raw = request.query_params.get(LIMIT)
        if raw is None:
            return self.page_size
        if not (raw.isascii() and raw.isdigit() and 1 <= int(raw) <= self.max_page_size):
            raise ValidationError({LIMIT: [f"Un entero entre 1 y {self.max_page_size}."]})
        return int(raw)

    def get_ordering(self, request: Any, queryset: Any, view: Any) -> tuple[str, ...]:
        """El orden lo declara la vista (`ordering`), nunca el cliente, y acaba en `id`: sin un
        desempate único, dos páginas podrían repetir o saltarse filas."""
        declared = getattr(view, "ordering", None) or self.ordering
        ordering = (declared,) if isinstance(declared, str) else tuple(declared)
        if ordering[-1].lstrip("-") != "id":
            raise ImproperlyConfigured(f"{type(view).__name__}.ordering debe terminar en `id`")
        return ordering

    def decode_cursor(self, request: Any) -> Any:
        cursor = super().decode_cursor(request)
        if cursor is not None and (cursor.position is None or cursor.reverse):
            raise NotFound  # solo se emiten cursores hacia delante y con posición
        return cursor

    def paginate_queryset(self, queryset: Any, request: Any, view: Any = None) -> Any:
        try:
            return super().paginate_queryset(queryset, request, view)
        except NotFound, InvalidValue, ValueError:  # ilegible, o con una posición imposible
            raise ValidationError({CURSOR: ["Cursor no válido."]}) from None

    def encode_cursor(self, cursor: Any) -> str:
        """El cursor opaco, no una URL: el cliente lo devuelve tal cual en `?cursor=`."""
        link: str = super().encode_cursor(cursor)
        return parse_qs(urlsplit(link).query)[CURSOR][0]

    def get_paginated_response(self, data: Any) -> Response:
        return Response({"results": data, "next": self.get_next_link()})

    def get_paginated_response_schema(self, schema: dict[str, Any]) -> dict[str, Any]:
        after = "Cursor de la página siguiente; `null` en la última."
        return {
            "type": "object",
            "required": ["results", "next"],
            "properties": {
                "results": schema,
                "next": {"type": "string", "nullable": True, "description": after},
            },
        }

    def get_schema_operation_parameters(self, view: Any) -> list[dict[str, Any]]:
        size = {"type": "integer", "minimum": 1, "maximum": self.max_page_size}
        return [
            {
                "name": CURSOR,
                "in": "query",
                "required": False,
                "description": "El `next` de la página anterior. Sin él, la primera página.",
                "schema": {"type": "string"},
            },
            {
                "name": LIMIT,
                "in": "query",
                "required": False,
                "description": "Filas por página.",
                "schema": {**size, "default": self.page_size},
            },
        ]
