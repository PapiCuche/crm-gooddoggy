"""La interfaz de proveedor (F8-05; ADR-005 Parte B, ADR-034 §2): un formato neutro que cada
adaptador traduce a su API. Nada de aquí se registra: lleva el texto del cliente."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID


class Outcome(StrEnum):  # los seis primeros quedan en `ai_llm_calls.outcome`
    OK = "OK"
    CREDENTIALS_REJECTED = "CREDENTIALS_REJECTED"
    PROVIDER_BILLING = "PROVIDER_BILLING"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    PROVIDER_ERROR = "PROVIDER_ERROR"  # 400, 404, 409, 413: un fallo nuestro, no se reintenta
    # Salió y no hubo respuesta (tiempo agotado, corte, cuerpo ilegible): pudo cobrarla, la reserva
    # cuenta entera. Solo en el registro; quien llama recibe `PROVIDER_UNAVAILABLE`.
    PROVIDER_UNCERTAIN = "PROVIDER_UNCERTAIN"
    AI_NOT_CONFIGURED = "AI_NOT_CONFIGURED"  # sin cuenta activa, sin clave legible o sin precio
    AI_BUDGET_NOT_SET = "AI_BUDGET_NOT_SET"  # sin tope: no se llama, igual que sin margen
    AI_BUDGET_EXHAUSTED = "AI_BUDGET_EXHAUSTED"  # sin margen: no se llama


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    schema: dict[str, Any]  # JSON Schema estricto de la entrada


@dataclass(frozen=True)
class Turn:  # devolver resultados de tools y reenviar los bloques del modelo: #932
    role: str  # "user" | "assistant"
    text: str


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


class ProviderError(Exception):  # noqa: N818 — solo el código y si pudo facturar
    """`billed`: salió y no hubo respuesta de error (pudo cobrarla): la reserva cuenta entera."""

    def __init__(self, outcome: Outcome, *, billed: bool, request_id: str = "") -> None:
        super().__init__(outcome.value)
        self.outcome, self.billed, self.request_id = outcome, billed, request_id


class LLMProviderAdapter(Protocol):
    def encode(self, request: LLMRequest, params: dict[str, Any]) -> bytes: ...  # lo que se envía

    def send(self, key: str, body: bytes, seconds: float) -> LLMResponse: ...  # o ProviderError
