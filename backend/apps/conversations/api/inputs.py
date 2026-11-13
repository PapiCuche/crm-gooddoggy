"""Lo que entra por el simulador Sandbox (F6-02): estricto y con las reglas de los comandos, para
que un dato inválido sea un 400 en su campo. Sin `at`: el orden es el del servidor (03 §I.7)."""

import re
from collections.abc import Callable
from typing import Any

from rest_framework import serializers

from apps.conversations import commands, outbound_media
from apps.conversations.accounts import NUMBER_REFUSED
from apps.conversations.models import BODY_MAX, META_ID, TOKEN_FORMAT
from core.api.strict import Choice, Closed, Id, Text, Whole


class Checked(Text):
    """Un texto que además cumple la regla del comando (su `ValueError` es un 400 de este campo,
    sin repetir lo recibido)."""

    def __init__(self, clean: Callable[[Any], Any], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.clean = clean

    def to_internal_value(self, data: Any) -> Any:
        try:
            return self.clean(super().to_internal_value(data))
        except ValueError as error:
            text = f"{error}."
            raise serializers.ValidationError(text[:1].upper() + text[1:]) from None


class SandboxInboundSerializer(Closed, serializers.Serializer[Any]):
    contact_id = Id(help_text="Un contacto vivo de la organización que quien llama puede ver.")
    external_message_id = Checked(
        commands._external,
        max_length=commands.EXTERNAL_MAX,
        help_text="La clave de idempotencia: el mismo id es el mismo mensaje.",
    )
    text = Checked(
        commands._body,
        trim_whitespace=False,
        max_length=BODY_MAX,
        help_text=f"Hasta {BODY_MAX} caracteres, con algo visible.",
    )


class SendMessageSerializer(Closed, serializers.Serializer[Any]):
    client_message_id = Id(help_text="La clave de idempotencia: el mismo id es el mismo mensaje.")
    body = Checked(
        commands._body,
        trim_whitespace=False,
        max_length=BODY_MAX,
        help_text=f"Hasta {BODY_MAX} caracteres, con algo visible.",
    )


def _caption(value: Any) -> str:
    text = commands._body(value)
    if outbound_media.units(text) > outbound_media.CAPTION_MAX:  # como cuenta WhatsApp
        raise ValueError(f"pie de foto: hasta {outbound_media.CAPTION_MAX} unidades UTF-16")
    return text


class SendMediaSerializer(Closed, serializers.Serializer[Any]):
    client_message_id = Id(help_text="La clave de idempotencia: el mismo id es el mismo mensaje.")
    caption = Checked(
        _caption, required=False, trim_whitespace=False,
        help_text=f"El pie de foto, hasta {outbound_media.CAPTION_MAX} unidades UTF-16 (un emoji "
        "puede contar 2); sin él, el chat dice «Imagen» o «Documento».",
    )  # fmt: skip


class SendMediaFormSerializer(SendMediaSerializer):  # solo para el contrato
    file = serializers.FileField(
        help_text="Una imagen JPEG o PNG (hasta 5 MiB) o un documento PDF (hasta 10 MiB). El tipo "
        "sale del contenido, nunca del nombre ni del tipo declarado."
    )


class NoteSerializer(Closed, serializers.Serializer[Any]):
    client_message_id = Id(help_text="La clave de idempotencia: el mismo id es la misma nota.")
    body = Checked(
        commands._body,
        trim_whitespace=False,
        max_length=BODY_MAX,
        help_text=f"Hasta {BODY_MAX} caracteres, con algo visible. Nunca sale por un canal.",
    )


class AssignSerializer(Closed, serializers.Serializer[Any]):
    action = Choice(
        choices=["TAKE", "TRANSFER", "RELEASE"],
        help_text="`TAKE`: la toma quien llama. `TRANSFER`: pasa a `user_id`. "
        "`RELEASE`: sin responsable.",
    )
    user_id = Id(required=False, help_text="Solo con `TRANSFER`: la persona que la recibe.")
    assignment_version = Whole(
        required=False,
        min_value=1,
        max_value=2**31 - 1,
        help_text="La que se leyó; si cambió, 409. Sin ella no se comprueba.",
    )

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        transfer = attrs["action"] == "TRANSFER"
        if transfer and "user_id" not in attrs:
            raise serializers.ValidationError({"user_id": ["Obligatorio con TRANSFER."]})
        if not transfer and "user_id" in attrs:
            raise serializers.ValidationError({"user_id": ["Solo con TRANSFER."]})
        return attrs


class Pattern(Text):
    """Un texto que cumple una expresión; si no, siempre el mismo mensaje fijo, sin repetir lo
    recibido (para el número, el mismo de un número que ya existe: sin oráculo)."""

    def __init__(self, pattern: str, message: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.pattern, self.message = re.compile(pattern), message

    def to_internal_value(self, data: Any) -> Any:
        value: str = super().to_internal_value(data)
        if not self.pattern.fullmatch(value):
            raise serializers.ValidationError(self.message)
        return value


class TokenSerializer(Closed, serializers.Serializer[Any]):
    access_token = Pattern(
        TOKEN_FORMAT, "Token no válido.", trim_whitespace=False,
        help_text="El token de acceso del usuario del sistema de Meta. Solo se guarda cifrado.",
    )  # fmt: skip


class ConnectSerializer(TokenSerializer):
    name = Text(max_length=100, help_text="El nombre que le das a la cuenta.")
    phone_number_id = Pattern(
        META_ID,
        NUMBER_REFUSED,
        help_text="El identificador del número en Meta (`phone_number_id`).",
    )
    waba_id = Pattern(META_ID, "Identificador no válido.", help_text="El WABA al que pertenece.")
