"""Cuerpos de la API: JSON, y solo en UTF-8."""

import codecs
import json
from collections.abc import Mapping
from typing import IO, Any

from rest_framework.exceptions import ParseError, UnsupportedMediaType
from rest_framework.parsers import JSONParser

MAX_DEPTH = 100  # niveles de `[` o `{`; ningún cuerpo de la API pasa de unos pocos
_NESTS = (dict, list)  # una tupla: `isinstance` con `dict | list` tarda el triple


def _nested_beyond(value: Any, limit: int) -> bool:
    """¿Anida más de `limit` niveles? Por niveles y sin recursión: un valor muy anidado no
    puede desbordar aquí."""
    level = [value] if isinstance(value, _NESTS) else []
    for _ in range(limit):
        if not level:
            return False
        level = [
            child
            for item in level
            for child in (item.values() if isinstance(item, dict) else item)
            if isinstance(child, _NESTS)
        ]
    return bool(level)


class Utf8JSONParser(JSONParser):
    """El `charset` de `Content-Type` no elige el códec.

    Python registra como códecs `zlib`, `bz2` o `rot13`. Aceptarlos dejaría a un cliente sin
    sesión hacer que el servidor descomprima un cuerpo cientos de veces mayor que el límite de
    tamaño de Django, que solo mide los bytes recibidos.

    Un cuerpo que anida más de `MAX_DEPTH` niveles se rechaza como cualquier JSON roto: 400
    `PARSE_ERROR`. Sin ese límite hay dos formas de acabar en un 500 con su traza en el log:
    el anidamiento que desborda al analizador (`RecursionError`) y, antes de llegar ahí, el
    que el analizador lee pero desborda después a quien lo recorre (un campo de DRF hace
    `str()` de lo que recibe).

    Una cadena con un sustituto Unicode suelto (el escape de medio carácter, sin su pareja)
    tampoco es texto: no se puede guardar ni devolver en UTF-8. Los campos de texto la rechazan
    uno a uno; un campo de opción la repite en su mensaje de error, y la respuesta acababa en
    un 500. Aquí se rechaza entera, en valores y en claves: 400 `PARSE_ERROR`.
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
            data = super().parse(stream, media_type, parser_context)
        except RecursionError:
            raise ParseError from None
        if _nested_beyond(data, MAX_DEPTH):
            raise ParseError
        try:
            json.dumps(data, ensure_ascii=False).encode()
        except UnicodeEncodeError:
            raise ParseError from None
        return data
