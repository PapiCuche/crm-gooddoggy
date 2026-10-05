"""Cuerpos de la API: JSON, y solo en UTF-8."""

import codecs
from collections.abc import Mapping
from typing import IO, Any

from rest_framework.exceptions import ParseError, UnsupportedMediaType
from rest_framework.parsers import JSONParser


class Utf8JSONParser(JSONParser):
    """El `charset` de `Content-Type` no elige el códec.

    Python registra como códecs `zlib`, `bz2` o `rot13`. Aceptarlos dejaría a un cliente sin
    sesión hacer que el servidor descomprima un cuerpo cientos de veces mayor que el límite de
    tamaño de Django, que solo mide los bytes recibidos.

    Un anidamiento que desborda al analizador (cientos de miles de `[`) es un cuerpo que no se
    puede leer, como cualquier JSON roto: 400 `PARSE_ERROR`, no un 500 con su traza en el log.
    """

    def parse(
        self,
        stream: IO[Any],
        media_type: str | None = None,
        parser_context: Mapping[str, Any] | None = None,
    ) -> Any:
        # DRF siempre pasa un `encoding`, y Django ya descarta un charset que no conoce: el
        # valor por defecto y el `LookupError` solo cubren una llamada directa.
        declared = (parser_context or {}).get("encoding") or "utf-8"
        try:
            codec = codecs.lookup(declared).name
        except LookupError:
            codec = ""
        if codec != "utf-8":
            raise UnsupportedMediaType(media_type or "")
        try:
            return super().parse(stream, media_type, parser_context)
        except RecursionError:
            raise ParseError from None
