"""Lo que entra por el simulador Sandbox (F6-02): estricto y con las reglas de los comandos, para
que un dato inválido sea un 400 en su campo. Sin `at`: el orden es el del servidor (03 §I.7)."""

from collections.abc import Callable
from typing import Any

from rest_framework import serializers

from apps.conversations import commands
from apps.conversations.models import BODY_MAX
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
