"""Adaptadores de canal (F6-04b, ADR-010): lo único que cambia de un canal a otro.

`ChannelAdapter.send` recibe el mensaje que el despachador ya validó (conversación abierta, cuenta
conectada, estado `QUEUED`) y devuelve el resultado; no escribe en la base de datos. Un fallo
definitivo es un `SendResult` con un motivo corto y fijo (nunca el texto del mensaje ni el error
del proveedor); un fallo pasajero lanza `TemporaryChannelError` y el despachador reintenta. La
Fase 7 sustituye `SandboxAdapter` por proveedores reales sin tocar el despachador.
"""

from dataclasses import dataclass
from typing import Protocol

from apps.conversations.models import Message

REASONS = ("CONVERSATION_CLOSED", "CHANNEL_UNAVAILABLE", "ADAPTER_REJECTED", "RETRIES_EXHAUSTED")


class TemporaryChannelError(Exception):
    """El canal no respondió ahora; vale reintentar (con espera creciente)."""


@dataclass(frozen=True, slots=True)
class SendResult:
    """`ok` con el id que el canal le dio al mensaje, o no `ok` con un motivo de `REASONS`.
    `delivered`: el canal ya lo entregó (Sandbox); los reales lo avisan después, con un webhook."""

    ok: bool
    external_id: str | None = None
    delivered: bool = False
    reason: str | None = None


class ChannelAdapter(Protocol):
    """Contrato (ADR-010): el despachador entrega al menos una vez, así que `send` puede llamarse
    más de una vez para el mismo mensaje (el barredor, un reintento, o un fallo tras enviar y antes
    de confirmar en la base) y **debe ser idempotente**: la clave es `message.pk` (o su
    `client_message_id`). Un `TemporaryChannelError` no lleva el texto del mensaje ni el cuerpo de
    la respuesta del proveedor: lo registran Celery y los registros."""

    def send(self, message: Message) -> SendResult: ...


class SandboxAdapter:
    """Siempre acepta y entrega, con un id externo sintético y estable: reenviar el mismo mensaje
    da el mismo id."""

    def send(self, message: Message) -> SendResult:
        return SendResult(True, f"sandbox-{message.pk}", delivered=True)


class UnknownPlatform(LookupError):
    """La plataforma de la cuenta no tiene adaptador (hasta la Fase 7, solo `SANDBOX`)."""


ADAPTERS: dict[str, ChannelAdapter] = {"SANDBOX": SandboxAdapter()}


def adapter_for(platform: str) -> ChannelAdapter:
    try:
        return ADAPTERS[platform]
    except KeyError:
        raise UnknownPlatform(platform) from None
