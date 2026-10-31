"""Campos de serializador estrictos para las rutas que escriben (F3-48d, F5-02b).

Solo el tipo propio del campo: ni `"5"` por 5 ni `1` por `true`, y un campo que el serializador
no declara es 400 con su nombre en lugar de perderse en silencio. Nacieron en las promociones;
viven en el núcleo porque `pricing` y `deals` no se importan."""

from collections.abc import Mapping
from typing import Any

from rest_framework import serializers

UNKNOWN = "Campo desconocido."


class Strict:
    """Solo el tipo propio del campo: ni `"5"` ni `1` ni `5.0`, ni un número como texto."""

    kind: tuple[type, ...] = (str,)

    def to_internal_value(self, data: Any) -> Any:
        if type(data) not in self.kind:
            self.fail("invalid")  # type: ignore[attr-defined]
        return super().to_internal_value(data)  # type: ignore[misc]


class Text(Strict, serializers.CharField):
    pass


class Id(Strict, serializers.UUIDField):
    pass


class Whole(Strict, serializers.IntegerField):
    kind = (int,)


class Flag(Strict, serializers.BooleanField):
    kind = (bool,)


class Choice(Strict, serializers.ChoiceField):
    """Una opción de la lista, como texto; el error no repite lo recibido."""

    default_error_messages = {
        "invalid": "Opción no admitida.",
        "invalid_choice": "Opción no admitida.",
    }


class Closed:
    """Un campo que el serializador no declara es 400 con su nombre (un `overide_value` no se
    pierde en silencio)."""

    def to_internal_value(self, data: Any) -> Any:
        if isinstance(data, Mapping):
            extra = {str(key): [UNKNOWN] for key in data if key not in self.fields}  # type: ignore[attr-defined]
            if extra:
                raise serializers.ValidationError(extra)
        return super().to_internal_value(data)  # type: ignore[misc]
