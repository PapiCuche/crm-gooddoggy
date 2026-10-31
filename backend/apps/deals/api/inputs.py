"""Lo que entra por las rutas de escritura de pipelines y motivos de pérdida (F5-02b, F5-02c):
serializadores estrictos (el tipo exacto de cada campo, ninguna clave desconocida) y los topes de
tamaño que los comandos no ponen (F5-01d los llevará a los comandos): `requires_fields` hasta 4 KB
serializado y 4 niveles, sin NaN, infinitos ni NUL; hasta 30 etapas por pipeline, 50 pipelines y
100 motivos de pérdida por organización. Los textos se limpian con el criterio de los comandos,
para que un dato inválido sea un 400 en su campo."""

import json
import math
import re
from collections.abc import Callable
from typing import Any

from drf_spectacular.extensions import OpenApiSerializerExtension
from rest_framework import serializers

from apps.deals.commands import CODE_MAX, DAYS_MAX, NAME_MAX, _code
from apps.deals.models import CODE, COLOR, REASON_CODE, Kind, StageType
from core.api.strict import Choice, Closed, Flag, Text, Whole
from core.text import name as clean_name

STAGES_MAX, PIPELINES_MAX, REASONS_MAX = 30, 50, 100
FIELDS_BYTES, FIELDS_DEPTH = 4096, 4
COLOR_RE = re.compile(COLOR)
ORDER = (-32768, 32767)  # `sort_order` de un pipeline o un motivo, como lo validan los comandos
SHAPE = f"Un objeto JSON de hasta {FIELDS_DEPTH} niveles, sin NaN, infinitos ni NUL."
NEW_STAGE_REQUIRED = ("name", "code", "stage_type", "default_probability", "color")


def _name(value: str) -> str:
    return clean_name(value, NAME_MAX)


def sentence(error: Exception) -> str:
    text = f"{error}."
    return text[:1].upper() + text[1:]


class Checked(Text):
    """Un texto que además cumple la regla del comando; su `ValueError` es un 400 de este campo,
    con el texto de la regla (que no repite lo recibido)."""

    def __init__(self, clean: Callable[[str], str], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.clean = clean

    def to_internal_value(self, data: Any) -> str:
        try:
            return self.clean(super().to_internal_value(data))
        except ValueError as error:
            raise serializers.ValidationError(sentence(error)) from None


def _color(value: str) -> str:
    text = value.strip().lower()
    if not COLOR_RE.fullmatch(text):
        raise ValueError("un color #RRGGBB, como #1f9d55")
    return text


def _plain(value: Any, depth: int) -> bool:
    """JSON de verdad y acotado: sin NaN ni infinitos, sin NUL (PostgreSQL no lo guarda), con
    claves de texto y a lo más `FIELDS_DEPTH` niveles de objeto o lista (el primero es 1)."""
    kind = type(value)
    if kind is str:
        return "\x00" not in value
    if kind is float:
        return math.isfinite(value)
    if kind in (int, bool) or value is None:
        return True
    if depth > FIELDS_DEPTH or kind not in (dict, list):
        return False
    if kind is list:
        return all(_plain(item, depth + 1) for item in value)
    return all(
        type(key) is str and "\x00" not in key and _plain(item, depth + 1)
        for key, item in value.items()
    )


class RequiresFields(serializers.DictField):
    """Qué datos pide una etapa antes de entrar en ella (objeto JSON, solo se guarda)."""

    def to_internal_value(self, data: Any) -> dict[str, Any]:
        if type(data) is not dict or not _plain(data, 1):
            raise serializers.ValidationError(SHAPE)
        size = len(json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode())
        if size > FIELDS_BYTES:
            raise serializers.ValidationError(f"Hasta {FIELDS_BYTES} bytes serializado.")
        found: dict[str, Any] = data
        return found


class StageFields(Closed, serializers.Serializer[Any]):
    """Los datos de una etapa, todos opcionales; el código y el orden no se cambian."""

    name = Checked(_name, max_length=NAME_MAX, required=False)
    stage_type = Choice(choices=StageType.choices, required=False)
    default_probability = Whole(
        min_value=0, max_value=100, required=False, help_text="WON 100, LOST 0."
    )
    stale_after_days = Whole(
        min_value=1, max_value=DAYS_MAX, allow_null=True, required=False, help_text="`null`: nunca."
    )
    color = Checked(_color, max_length=7, required=False, help_text="`#rrggbb`.")
    requires_fields = RequiresFields(
        required=False,
        help_text=f"Objeto JSON de hasta {FIELDS_BYTES} bytes y {FIELDS_DEPTH} niveles.",
    )
    is_active = Flag(required=False)


class NewStageSerializer(StageFields):
    code = Checked(
        lambda v: _code(v, CODE, str.lower),
        max_length=CODE_MAX,
        help_text="Minúsculas, cifras y guiones; único en el pipeline. No cambia.",
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        for name in NEW_STAGE_REQUIRED:
            self.fields[name].required = True


class PipelineCreateSerializer(Closed, serializers.Serializer[Any]):
    name = Checked(_name, max_length=NAME_MAX)
    code = Checked(
        lambda v: _code(v, CODE, str.lower),
        max_length=CODE_MAX,
        help_text="Minúsculas, cifras y guiones; único en la organización. No cambia.",
    )
    kind = Choice(choices=Kind.choices)
    currency = Choice(choices=[("PEN", "PEN")], required=False, help_text="Solo `PEN`.")
    sort_order = Whole(min_value=ORDER[0], max_value=ORDER[1], required=False)
    is_default = Flag(
        required=False,
        help_text="El primer pipeline de la organización lo es aunque se diga que no.",
    )
    stages = NewStageSerializer(
        many=True,
        min_length=1,
        max_length=STAGES_MAX,
        help_text="En el orden en que se numeran: una WON, al menos una LOST y una OPEN.",
    )


def version_required(serializer: type[serializers.Serializer[Any]]) -> Any:
    """drf-spectacular trata todo `PATCH` como parcial; aquí la `version` sigue obligatoria."""

    class Schema(OpenApiSerializerExtension):
        target_class = serializer

        def map_serializer(self, auto_schema: Any, direction: Any) -> Any:
            schema = auto_schema._map_serializer(self.target, direction, bypass_extensions=True)
            return {**schema, "required": ["version"]}

    return serializer


class LostReasonCreateSerializer(Closed, serializers.Serializer[Any]):
    name = Checked(_name, max_length=NAME_MAX)
    code = Checked(
        lambda v: _code(v, REASON_CODE, str.upper),
        max_length=CODE_MAX,
        help_text="`MAYÚSCULAS_Y_GUION_BAJO`; único entre los vivos. No cambia.",
    )
    sort_order = Whole(
        min_value=ORDER[0],
        max_value=ORDER[1],
        required=False,
        help_text="Sin él, al final; tras 32767 hay que indicarlo (400).",
    )


@version_required
class LostReasonUpdateSerializer(Closed, serializers.Serializer[Any]):
    name = Checked(_name, max_length=NAME_MAX, required=False)
    sort_order = Whole(min_value=ORDER[0], max_value=ORDER[1], required=False)
    is_active = Flag(required=False)
    version = Whole(min_value=1, max_value=2**31 - 1, help_text="La que se leyó; si no, 409.")
