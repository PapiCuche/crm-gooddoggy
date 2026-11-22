"""La interfaz de proveedor (F8-05; ADR-005 Parte B, ADR-034 §2): un formato neutro que cada
adaptador traduce a su API. Nada de aquí se registra: lleva el texto del cliente."""

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID


class Outcome(StrEnum):  # los siete primeros quedan en `ai_llm_calls.outcome`
    OK = "OK"
    CREDENTIALS_REJECTED = "CREDENTIALS_REJECTED"
    PROVIDER_BILLING = "PROVIDER_BILLING"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    PROVIDER_ERROR = "PROVIDER_ERROR"  # 400, 404, 409, 413: un fallo nuestro, no se reintenta
    # Salió y no hubo respuesta (tiempo agotado, corte, cuerpo ilegible): pudo cobrarla, la reserva
    # cuenta entera. Solo en el registro; quien llama recibe `PROVIDER_UNAVAILABLE`.
    PROVIDER_UNCERTAIN = "PROVIDER_UNCERTAIN"
    # Reserva huérfana (proceso caído) cerrada por el barrido: cuenta entera (F8-05b5).
    ORPHANED = "ORPHANED"
    AI_NOT_CONFIGURED = "AI_NOT_CONFIGURED"  # sin cuenta activa, sin clave legible o sin precio
    AI_BUDGET_NOT_SET = "AI_BUDGET_NOT_SET"  # sin tope: no se llama, igual que sin margen
    AI_BUDGET_EXHAUSTED = "AI_BUDGET_EXHAUSTED"  # sin margen: no se llama
    AI_TURN_INVALID = "AI_TURN_INVALID"  # un turno mal formado o sin cota: ni se reserva (F8-05b7)
    AI_DISABLED = "AI_DISABLED"  # `AI_ENABLED` o el interruptor de la organización (F8-06)
    # F8-08g2: el ajuste falló (la base) y la reserva queda abierta: el barrido la cierra como
    # gastada. Solo para quien llama, con el id de la reserva; nunca en el registro.
    INTERNAL_ERROR = "INTERNAL_ERROR"
    # F8-13c: `admit` lo negó o falló (nada más salió). Solo para quien llama, nunca en el registro.
    AI_WITHHELD = "AI_WITHHELD"


@dataclass(frozen=True)
class Text:
    text: str


@dataclass(frozen=True)
class Image:  # solo en base64: el proveedor no descarga nada
    media_type: str
    data: str


@dataclass(frozen=True)
class Thinking:  # se reenvía intacto: la firma lleva cifrado el razonamiento entero
    thinking: str
    signature: str


@dataclass(frozen=True)
class RedactedThinking:  # se reenvía intacto
    data: str


@dataclass(frozen=True)
class ToolCall:  # el `tool_use` que pide el modelo y que se le reenvía
    id: str
    name: str
    input: dict[str, Any]


@dataclass(frozen=True)
class ToolResult:  # la respuesta a un `ToolCall` del turno anterior
    tool_use_id: str
    content: tuple[Text | Image, ...] = ()
    is_error: bool = False


Block = Text | Image | Thinking | RedactedThinking | ToolCall | ToolResult


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    schema: dict[str, Any]  # JSON Schema estricto de la entrada


@dataclass(frozen=True)
class Turn:  # los bloques del modelo vuelven tal cual (F8-05b7); un texto es un bloque de texto
    role: str  # "user" | "assistant"
    content: str | tuple[Block, ...]


@dataclass(frozen=True)
class LLMRequest:
    purpose: str  # SUGGEST | SUMMARY
    system: str
    turns: tuple[Turn, ...]
    tools: tuple[Tool, ...] = ()
    output_schema: dict[str, Any] | None = None  # la respuesta final, con salida estructurada
    conversation_id: UUID | None = None


@dataclass(frozen=True)
class Usage:
    input_tokens: int  # sin caché
    output_tokens: int
    cache_write_5m: int = 0
    cache_write_1h: int = 0
    cache_read: int = 0


@dataclass(frozen=True)
class LLMResponse:
    text: str
    tool_calls: tuple[ToolCall, ...]
    stop: str
    usage: Usage
    request_id: str
    blocks: tuple[Block, ...] = ()  # los del modelo, en orden, para reenviarlos sin cambios
    reservation_id: UUID | None = None  # F8-08g: la de `ai_llm_calls`, que pone el gateway


class ProviderError(Exception):  # noqa: N818 — solo el código y si pudo facturar
    """`billed`: salió y no hubo respuesta de error (pudo cobrarla): la reserva cuenta entera."""

    def __init__(self, outcome: Outcome, *, billed: bool, request_id: str = "") -> None:
        super().__init__(outcome.value)
        self.outcome, self.billed, self.request_id = outcome, billed, request_id


class LLMProviderAdapter(Protocol):
    def encode(self, request: LLMRequest, params: dict[str, Any]) -> bytes: ...  # lo que se envía

    def send(  # o ProviderError; `proceed()` antes de cada reintento: si no, no reenvía (F8-13c)
        self, key: str, body: bytes, seconds: float, proceed: Callable[[], bool]
    ) -> LLMResponse: ...
