"""Adaptadores de canal (F6-04b, F7-05b; ADR-010, ADR-029 §6): lo único que cambia de un canal a
otro.

`ChannelAdapter.send` recibe un `Claim` (el mensaje reclamado, `SENDING`, con lo que hace falta,
leído antes): **no toca la base de datos y se llama fuera de toda transacción**. Devuelve un
`SendResult` (un fallo definitivo lleva un motivo corto y fijo, nunca el texto del proveedor) o
lanza `TemporaryChannelError` (seguro que no llegó: se devuelve el reclamo y se reintenta) o
`AmbiguousChannelError` (pudo llegar: no se reenvía a ciegas, Meta no deduplica).
"""

from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID

from apps.conversations.models import FAILURE_CODES, Credential

REASONS = FAILURE_CODES  # los motivos de un `FAILED` (los de la política, el despacho y Meta)


class TemporaryChannelError(Exception):
    """Seguro que el canal no recibió el mensaje (no se llegó a él, o «demasiado rápido»): se
    reintenta con espera creciente."""


class AmbiguousChannelError(Exception):
    """La petición pudo llegar y no hay respuesta que lo diga (tiempo agotado, corte, 5xx):
    reenviar podría duplicar el mensaje, así que se queda `SENDING`."""


@dataclass(frozen=True, slots=True)
class Claim:
    """Lo que el adaptador necesita de un mensaje reclamado. `credential` va cifrada: el token solo
    se abre durante la llamada."""

    message_id: UUID
    account_id: UUID
    platform: str
    phone_number_id: str  # el `external_account_id` de la cuenta
    recipient: str  # el teléfono del contacto en E.164; vacío si no tiene
    body: str = field(repr=False)
    credential: Credential | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class SendResult:
    """`ok` con el id que el canal le dio al mensaje, o no `ok` con un motivo de `REASONS`.
    `delivered`: el canal ya lo entregó (Sandbox); los reales lo avisan después, con un webhook."""

    ok: bool
    external_id: str | None = None
    delivered: bool = False
    reason: str | None = None


class ChannelAdapter(Protocol):
    """Contrato (ADR-010, ADR-029 §6): una llamada por reclamo, que un reclamo devuelto puede
    repetir: `send` **debe ser idempotente** con la clave `claim.message_id` donde el canal lo
    permita. Ningún error lleva el texto del mensaje ni la respuesta del proveedor."""

    def send(self, claim: Claim) -> SendResult: ...


class SandboxAdapter:
    """Siempre acepta y entrega, con un id externo sintético y estable: reenviar el mismo mensaje
    da el mismo id."""

    def send(self, claim: Claim) -> SendResult:
        return SendResult(True, f"sandbox-{claim.message_id}", delivered=True)


class UnknownPlatform(LookupError):
    """La plataforma de la cuenta no tiene adaptador (`SANDBOX` y `WHATSAPP`)."""


ADAPTERS: dict[str, ChannelAdapter] = {"SANDBOX": SandboxAdapter()}


def adapter_for(platform: str) -> ChannelAdapter:
    if platform == "WHATSAPP":  # se importa aquí: `whatsapp` usa los tipos de este módulo
        from apps.conversations.whatsapp import WhatsAppAdapter

        return ADAPTERS.setdefault(platform, WhatsAppAdapter())
    try:
        return ADAPTERS[platform]
    except KeyError:
        raise UnknownPlatform(platform) from None
