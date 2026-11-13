"""Lo que entra por las rutas de escritura de cotizaciones (F5-09e): serializadores cerrados y
estrictos (el tipo exacto de cada campo; una clave desconocida, también `organization_id`,
`status`, `number` o un total, es 400) con las reglas de los comandos (`commands._text`,
`_moment` e `_items`), para que un dato inválido sea un 400 en su campo. Las líneas solo llevan
variante y cantidad: el precio lo pone el `PricingService`, nunca el cliente."""

import datetime as dt
from typing import Any

from django.utils.dateparse import parse_datetime
from rest_framework import serializers

from apps.deals.api.inputs import Checked, sentence, version_required
from apps.quotes import commands, decisions, discounts
from apps.quotes.models import QUANTITY_MAX
from core.api.strict import Closed, Id, Whole
from core.money import parse_amount

INSTANT = "ISO 8601 con zona horaria, como 2026-11-20T23:59:00-05:00."
VERSION = "La que se leyó; si no, 409."
NOTE = f"Texto libre de hasta {commands.NOTE_MAX} caracteres."
AMOUNT = "Texto decimal sin signo ni exponente, hasta 12 cifras enteras y 2 decimales."


def _instant(value: str) -> dt.datetime:
    moment = parse_datetime(value)
    if moment is None:
        raise ValueError(f"fecha y hora {INSTANT[:-1]}")
    return commands._moment(moment)


def _note(value: str) -> str:
    return commands._text(value, "nota", commands.NOTE_MAX)


class QuoteLineInputSerializer(Closed, serializers.Serializer[Any]):
    """Una línea: la variante y la cantidad; el precio no se envía."""

    variant_id = Id(help_text="Una variante con precio vigente en la lista de la cotización.")
    quantity = Whole(min_value=1, max_value=QUANTITY_MAX)


class QuoteFields(Closed, serializers.Serializer[Any]):
    """Lo que se envía de una cotización al crearla o cambiarla."""

    valid_until = Checked(_instant, allow_null=True, required=False, help_text=INSTANT)
    notes_to_customer = Checked(_note, required=False, allow_blank=True, help_text=NOTE)
    internal_notes = Checked(_note, required=False, allow_blank=True, help_text=NOTE)
    items = serializers.ListField(
        child=QuoteLineInputSerializer(),
        min_length=1,
        max_length=commands.ITEMS_MAX,
        help_text="Una por variante; reemplazan a todas y se valoran con los precios de ahora.",
    )

    def validate_items(self, value: list[dict[str, Any]]) -> list[dict[str, Any]]:
        try:
            commands._items(value)
        except ValueError as error:
            raise serializers.ValidationError(sentence(error)) from None
        return value


class QuoteCreateSerializer(QuoteFields):
    contact_id = Id(required=False, help_text="Un contacto vivo que quien crea puede ver.")
    opportunity_id = Id(
        required=False,
        help_text="Una oportunidad viva que quien crea puede ver (de ese contacto, si se envía).",
    )
    price_list_id = Id(required=False, help_text="Sin ella, la lista predeterminada.")

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        if attrs.get("contact_id") is None and attrs.get("opportunity_id") is None:
            raise serializers.ValidationError("Un contacto o una oportunidad.")
        return attrs


@version_required
class QuoteUpdateSerializer(QuoteFields):
    version = Whole(min_value=1, max_value=2**31 - 1, help_text=VERSION)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fields["items"].required = False


class QuoteVersionSerializer(Closed, serializers.Serializer[Any]):
    """Solo la versión que se leyó; cualquier otro campo, 400."""

    version = Whole(min_value=1, max_value=2**31 - 1, help_text=VERSION)


def _reason(value: str) -> str:
    if not (why := commands._text(value, "motivo", discounts.REASON_MAX)):
        raise ValueError("motivo: obligatorio")
    return why


class QuotePriceSerializer(QuoteVersionSerializer):
    """El precio final escrito a mano, como texto (nunca un número JSON), y su motivo."""

    price = Checked(parse_amount, help_text=f"Final por unidad, con IGV. {AMOUNT}")
    reason = Checked(_reason, help_text=f"Obligatorio, hasta {discounts.REASON_MAX} caracteres.")


class QuoteApproveSerializer(QuoteVersionSerializer):
    comment = Checked(
        lambda v: commands._text(v, "comentario", decisions.COMMENT_MAX), required=False,
        allow_blank=True, help_text=f"Opcional, hasta {decisions.COMMENT_MAX} caracteres.",
    )  # fmt: skip


class QuoteRejectSerializer(QuoteVersionSerializer):
    reason = Checked(_reason, help_text=f"Obligatorio, hasta {discounts.REASON_MAX} caracteres.")


class QuoteCancelSerializer(QuoteVersionSerializer):
    reason = Checked(
        lambda v: commands._text(v, "motivo", commands.REASON_MAX), required=False,
        allow_blank=True, help_text=f"Opcional, hasta {commands.REASON_MAX} caracteres.",
    )  # fmt: skip
