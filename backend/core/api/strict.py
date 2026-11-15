"""Campos de serializador estrictos para las rutas que escriben (F3-48d, F5-02b).

Solo el tipo propio del campo: ni `"5"` por 5 ni `1` por `true`, y un campo que el serializador
no declara es 400 con su nombre en lugar de perderse en silencio. Nacieron en las promociones;
viven en el núcleo porque `pricing` y `deals` no se importan. `Checked`, `sentence` y
`version_required` vinieron de `apps.deals.api.inputs` (F5-09e2b): las usan `tasks` y `quotes`."""

from collections.abc import Callable, Mapping
from typing import Any

from drf_spectacular.extensions import OpenApiSerializerExtension, OpenApiSerializerFieldExtension
from rest_framework import serializers

UNKNOWN = "Campo desconocido."
KEY_MAX = 64  # lo que se repite del nombre de una clave desconocida


class Strict:
    """Solo el tipo propio del campo: ni `"5"` ni `1` ni `5.0`, ni un número como texto."""

    kind: tuple[type, ...] = (str,)

    def to_internal_value(self, data: Any) -> Any:
        if type(data) not in self.kind:
            self.fail("invalid")  # type: ignore[attr-defined]
        return super().to_internal_value(data)  # type: ignore[misc]


class Text(Strict, serializers.CharField):
    pass


def sentence(error: Exception) -> str:
    text = f"{error}."
    return text[:1].upper() + text[1:]


class Checked(Text):
    """Un texto que además cumple la regla del comando; su `ValueError` es un 400 de este campo,
    con el texto de la regla (que no repite lo recibido)."""

    def __init__(self, clean: Callable[[str], Any], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.clean = clean

    def to_internal_value(self, data: Any) -> Any:
        try:
            return self.clean(super().to_internal_value(data))
        except ValueError as error:
            raise serializers.ValidationError(sentence(error)) from None


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


def shown_key(key: Any) -> str:
    """El nombre de una clave desconocida como se responde: sus `KEY_MAX` primeros caracteres,
    con `?` en lugar de lo que no es ASCII imprimible (una clave de 100 KB con NUL o bidi no
    vuelve en la respuesta)."""
    text = str(key)
    shown = "".join(c if c.isascii() and c.isprintable() else "?" for c in text[:KEY_MAX])
    return shown + "..." if len(text) > KEY_MAX else shown


class Closed:
    # Un campo que el serializador no declara es 400 con su nombre (un `overide_value` no se
    # pierde en silencio). Comentario y no docstring: drf-spectacular la pondría como
    # descripción de cada serializador que no tiene la suya.

    def to_internal_value(self, data: Any) -> Any:
        if isinstance(data, Mapping):
            fields = self.fields  # type: ignore[attr-defined]
            extra = {shown_key(key): [UNKNOWN] for key in data if key not in fields}
            if extra:
                raise serializers.ValidationError(extra)
        return super().to_internal_value(data)  # type: ignore[misc]


def version_required(serializer: type[serializers.Serializer[Any]]) -> Any:
    """drf-spectacular trata todo `PATCH` como parcial; aquí la `version` sigue obligatoria."""

    class Schema(OpenApiSerializerExtension):
        target_class = serializer

        def map_serializer(self, auto_schema: Any, direction: Any) -> Any:
            schema = auto_schema._map_serializer(self.target, direction, bypass_extensions=True)
            return {**schema, "required": ["version"]}

    return serializer


class BoundedList(OpenApiSerializerFieldExtension):
    """`many=True` con `min_length` o `max_length`: DRF los comprueba, pero drf-spectacular no
    los lleva al esquema (no son validadores). Aquí van como `minItems` y `maxItems`."""

    target_class = serializers.ListSerializer
    match_subclasses = True

    def map_serializer_field(self, auto_schema: Any, direction: Any) -> Any:
        schema = auto_schema._map_serializer_field(self.target, direction, bypass_extensions=True)
        bounds = {"minItems": self.target.min_length, "maxItems": self.target.max_length}
        return schema and {**schema, **{k: v for k, v in bounds.items() if v is not None}}
