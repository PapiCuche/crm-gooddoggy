"""Lo que entra por las rutas de escritura de oportunidades (F5-04d): serializadores estrictos
(el tipo exacto de cada campo, ninguna clave desconocida) con las mismas reglas que los comandos
(`opportunities._decimal`, `_moment`, `_items`), para que un dato inválido sea un 400 en su campo
y no un error del comando. Los importes entran y salen como texto; las fechas, como ISO 8601, con
zona horaria las de fecha y hora. El responsable no se envía al crear (es quien crea; se cambia
con `…/assign/`), ni la organización, ni el pipeline o el contacto de una oportunidad existente."""

import datetime as dt
import re
from typing import Any

from django.utils.dateparse import parse_datetime
from rest_framework import serializers

from apps.deals.api.inputs import Checked, sentence, version_required
from apps.deals.models import Opportunity
from apps.deals.opportunities import (
    ACTION_MAX,
    ITEMS_MAX,
    NOTE_MAX,
    QUANTITY_MAX,
    TITLE_MAX,
    _decimal,
    _items,
    _line,
    _moment,
)
from core.api.strict import Choice, Closed, Id, Whole
from core.text import name as clean_name

DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
AMOUNT = "Texto con hasta 12 cifras enteras y 2 decimales, como `1500.50`."


def _title(value: str) -> str:
    return clean_name(_line(value, "título", TITLE_MAX), TITLE_MAX)


def _action(value: str) -> str:
    text = _line(value, "próxima acción", ACTION_MAX)
    if not text:
        raise ValueError("próxima acción: texto, o null para quitarla")
    return text


def _instant(value: str) -> dt.datetime:
    moment = parse_datetime(value)
    if moment is None:
        raise ValueError("fecha y hora ISO 8601 con zona horaria, como 2026-11-01T09:00:00-05:00")
    return _moment(moment, "fecha y hora")


def _day(value: str) -> dt.date:
    if not DAY.fullmatch(value):
        raise ValueError("fecha ISO 8601, como 2026-12-01")
    day = dt.date.fromisoformat(value)
    if not 2000 <= day.year <= 2100:
        raise ValueError("fecha entre los años 2000 y 2100")
    return day


def _amount(value: str) -> Any:
    return _decimal(value, "importe")


class ItemInputSerializer(Closed, serializers.Serializer[Any]):
    variant_id = Id(help_text="Una variante viva y activa de la organización.")
    quantity = Whole(min_value=1, max_value=QUANTITY_MAX)
    estimated_unit_price = Checked(_amount, required=False, help_text=f"Referencial. {AMOUNT}")
    notes = Checked(
        lambda v: _line(v, "notas", NOTE_MAX), max_length=NOTE_MAX, required=False, allow_blank=True
    )


class OpportunityFields(Closed, serializers.Serializer[Any]):
    """Lo que se envía de una oportunidad al crearla o cambiarla."""

    amount = Checked(_amount, required=False, help_text=f"Puede ser 0. {AMOUNT}")
    priority = Choice(choices=Opportunity.Priority.choices, required=False)
    source = Choice(choices=Opportunity.Source.choices, required=False)
    next_action = Checked(
        _action, max_length=ACTION_MAX, allow_null=True, required=False, help_text="Texto libre."
    )
    next_action_at = Checked(
        _instant, allow_null=True, required=False, help_text="ISO 8601 con zona horaria."
    )
    expected_close_date = Checked(
        _day, allow_null=True, required=False, help_text="`AAAA-MM-DD`, entre 2000 y 2100."
    )
    items = ItemInputSerializer(many=True, max_length=ITEMS_MAX, required=False, help_text=(
        f"Hasta {ITEMS_MAX}, una por variante; al cambiar, reemplazan todas las líneas."
    ))  # fmt: skip

    def validate_items(self, value: list[dict[str, Any]]) -> list[dict[str, Any]]:
        try:
            _items(value)  # la misma regla que el comando: variantes únicas y el tope del total
        except ValueError as error:
            raise serializers.ValidationError(sentence(error)) from None
        return value


class OpportunityCreateSerializer(OpportunityFields):
    contact_id = Id(help_text="Un contacto vivo que quien crea puede ver (`contacts.view`).")
    title = Checked(_title, max_length=TITLE_MAX, help_text="Una línea de texto libre.")
    pipeline_id = Id(
        required=False, help_text="Un pipeline vivo y activo; sin él, el predeterminado."
    )


@version_required
class OpportunityUpdateSerializer(OpportunityFields):
    title = Checked(_title, max_length=TITLE_MAX, required=False)
    probability = Whole(
        min_value=0, max_value=100, required=False, help_text="Mientras esté abierta."
    )
    version = Whole(min_value=1, max_value=2**31 - 1, help_text="La que se leyó; si no, 409.")


def _final(value: str) -> Any:
    amount = _decimal(value, "importe final")
    if amount <= 0:
        raise ValueError("importe final: mayor que 0")
    return amount


class OpportunityAssignSerializer(Closed, serializers.Serializer[Any]):
    assigned_user_id = Id(help_text="El responsable: un miembro activo de la organización.")
    version = Whole(min_value=1, max_value=2**31 - 1, help_text="La que se leyó; si no, 409.")


class OpportunityMoveSerializer(Closed, serializers.Serializer[Any]):
    stage_id = Id(
        help_text="Una etapa activa del pipeline de la oportunidad (abierta, al reabrir)."
    )
    version = Whole(min_value=1, max_value=2**31 - 1, help_text="La que se leyó; si no, 409.")


class OpportunityWinSerializer(Closed, serializers.Serializer[Any]):
    amount = Checked(
        _final, help_text=f"Importe final, mayor que 0; reemplaza el estimado. {AMOUNT}"
    )
    version = Whole(min_value=1, max_value=2**31 - 1, help_text="La que se leyó; si no, 409.")


class OpportunityLoseSerializer(Closed, serializers.Serializer[Any]):
    lost_reason_id = Id(help_text="Un motivo de pérdida activo de la organización.")
    note = Checked(
        lambda v: _line(v, "nota", NOTE_MAX),
        max_length=NOTE_MAX,
        required=False,
        allow_null=True,
        allow_blank=True,
        help_text="Texto libre, una línea; en blanco es ninguna.",
    )
    version = Whole(min_value=1, max_value=2**31 - 1, help_text="La que se leyó; si no, 409.")
