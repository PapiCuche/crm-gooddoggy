"""Política de envío (ADR-010): la única fuente que decide si una respuesta sale por un canal.

`evaluate` es una función pura sobre los hechos (conversación, cuenta, contacto bloqueado, hora):
no lee la base de datos ni el texto del cliente. La usan `record_outbound` (al encolar), el
despachador (la **re-evalúa justo antes de enviar**) y la ruta de consulta de la pantalla. La
ventana se recalcula siempre desde `last_inbound_at` con las `ChannelCapabilities` vigentes.
Orden: nota interna, conversación cerrada (`RESOLVED` admite respuestas, D-F6-11), canal no
conectado, contacto bloqueado y ventana. Plantillas y consentimientos: F7-08 y F7-10.
"""

import datetime as dt
from dataclasses import dataclass

from apps.conversations.models import Direction, Status

UNAVAILABLE = ("DISCONNECTED", "ERROR", "PENDING")  # estados de `ChannelAccount` sin envío
FREE_FORM, BLOCKED = "FREE_FORM", "BLOCKED"  # `mode`; TEMPLATE_REQUIRED llega con F7-10
REASONS = (  # el conjunto cerrado de motivos de una decisión que niega
    "INTERNAL_NOTE_NOT_SENDABLE", "CONVERSATION_CLOSED", "CHANNEL_UNAVAILABLE", "CONTACT_BLOCKED",
    "WINDOW_CLOSED",
)  # fmt: skip


class ConversationClosed(Exception):
    """La conversación está CLOSED: no admite más respuestas (un mensaje entrante abre otra)."""


class ChannelUnavailable(Exception):
    """El canal de la conversación no está conectado (`DISCONNECTED`, `ERROR` o `PENDING`)."""


class ContactBlocked(Exception):
    """El contacto está bloqueado: no recibe mensajes."""


class WindowClosed(Exception):
    """Pasó la ventana de atención del canal desde el último mensaje del cliente."""


DENIALS: dict[str, type[Exception]] = {
    "CONVERSATION_CLOSED": ConversationClosed,
    "CHANNEL_UNAVAILABLE": ChannelUnavailable,
    "CONTACT_BLOCKED": ContactBlocked,
    "WINDOW_CLOSED": WindowClosed,
}
CODES = {cls: code for code, cls in DENIALS.items()}


@dataclass(frozen=True, slots=True)
class ChannelCapabilities:
    """Lo que cambia de un canal a otro para decidir (ADR-010 §1). `window_hours`: cuántas horas
    después del último mensaje del cliente se escribe en formato libre; `None`, sin límite."""

    window_hours: int | None


CAPABILITIES: dict[str, ChannelCapabilities] = {
    "WHATSAPP": ChannelCapabilities(window_hours=24),  # la ventana de atención de Meta
    "SANDBOX": ChannelCapabilities(window_hours=None),  # el canal de pruebas no la tiene
}


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    """`allowed` y, si no, el `reason` (uno de `REASONS`); nunca texto del cliente ni del
    proveedor. `window_expires_at`: cuándo cierra (o cerró) la ventana, `None` si no tiene."""

    allowed: bool
    reason: str | None
    window_expires_at: dt.datetime | None
    window_hours: int | None
    evaluated_at: dt.datetime

    @property
    def mode(self) -> str:
        return FREE_FORM if self.allowed else BLOCKED

    def as_json(self) -> dict[str, object]:
        """La instantánea que se guarda en el mensaje (`messages.policy_decision`)."""
        expires = self.window_expires_at
        return {
            "allowed": self.allowed,
            "mode": self.mode,
            "reason": self.reason,
            "window_expires_at": None if expires is None else expires.isoformat(),
            "evaluated_at": self.evaluated_at.isoformat(),
        }


def evaluate(
    *,
    conversation_status: str,
    platform: str,
    account_status: str,
    account_deleted: bool,
    contact_blocked: bool,
    last_inbound_at: dt.datetime | None,
    now: dt.datetime,
    direction: str = Direction.OUTBOUND,
) -> PolicyDecision:
    """La decisión para escribir ahora en la conversación. La ventana está abierta mientras
    `now < last_inbound_at + window_hours`: a la hora exacta ya está cerrada."""
    caps = CAPABILITIES.get(platform)  # `None`: la plataforma no tiene adaptador
    hours = None if caps is None else caps.window_hours
    expires = (
        None
        if hours is None or last_inbound_at is None
        else last_inbound_at + dt.timedelta(hours=hours)
    )
    reason: str | None = None
    if direction == Direction.INTERNAL:
        reason = "INTERNAL_NOTE_NOT_SENDABLE"
    elif conversation_status == Status.CLOSED:
        reason = "CONVERSATION_CLOSED"
    elif caps is None or account_deleted or account_status in UNAVAILABLE:
        reason = "CHANNEL_UNAVAILABLE"  # una cuenta borrada o sin adaptador no recibe envíos
    elif contact_blocked:
        reason = "CONTACT_BLOCKED"
    elif hours is not None and (expires is None or now >= expires):
        reason = "WINDOW_CLOSED"  # sin mensaje del cliente, la ventana nunca se abrió
    return PolicyDecision(reason is None, reason, expires, hours, now)


def require_allowed(decision: PolicyDecision) -> None:
    """Lanza la excepción del motivo si la decisión niega; una nota (`INTERNAL_NOTE_NOT_SENDABLE`)
    nunca pasa por aquí y da `ValueError`."""
    if decision.reason is not None:
        raise DENIALS.get(decision.reason, ValueError)(decision.reason)
