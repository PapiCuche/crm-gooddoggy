"""Los campos de entrada de una tool (F8-07a; ADR-005 Parte C, ADR-034 §6). La frontera es esta
validación, no el modo estricto del proveedor. `Invalid` no lleva mensaje: nada de la entrada
vuelve al modelo ni al log. Sin `minLength`, `maxLength`, `minimum` ni `maximum` en el esquema
(la API los rechaza en modo estricto): los topes van en la descripción y los aplica `clean`."""

import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

CANONICAL = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
HIDDEN = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})  # control, formato, sin asignar…
MARKUP = frozenset("<>{}[]`|\\")  # plantillas, etiquetas y escapes: un nombre no los lleva


class Invalid(Exception):  # noqa: N818 — sin mensaje a propósito
    pass


class Field(Protocol):
    def schema(self) -> dict[str, Any]: ...

    def clean(self, value: object) -> Any: ...


@dataclass(frozen=True)
class Uuid:
    description: str

    def schema(self) -> dict[str, Any]:
        return {"type": "string", "format": "uuid", "description": self.description}

    def clean(self, value: object) -> UUID:
        if type(value) is not str or not CANONICAL.fullmatch(value):
            raise Invalid
        return UUID(value)


@dataclass(frozen=True)
class Query:
    """Una línea en NFKC, sin caracteres invisibles ni de marcado, de `low` a `high` caracteres."""

    description: str
    low: int = 2
    high: int = 80

    def schema(self) -> dict[str, Any]:
        limits = f" (de {self.low} a {self.high} caracteres, una línea)"
        return {"type": "string", "description": self.description + limits}

    def clean(self, value: object) -> str:
        if type(value) is not str or len(value) > 4 * self.high:  # antes de normalizar nada
            raise Invalid
        text = unicodedata.normalize("NFKC", value)
        if any(unicodedata.category(c) in HIDDEN or c in MARKUP for c in text):
            raise Invalid
        text = " ".join(text.split())
        if not self.low <= len(text) <= self.high:
            raise Invalid
        return text
