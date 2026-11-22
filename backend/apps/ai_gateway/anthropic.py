"""El adaptador de Anthropic (F8-05; ADR-034 §3): la Messages API por `core.http`, nunca con el
SDK; tools con `tool_choice: auto` y la respuesta final con `output_config.format`. Como mucho dos
reintentos, solo tras un 429 o un 5xx (sin uso) y dentro del plazo; nunca tras un tiempo agotado o
un corte (pudo facturar). `proceed()` corre en `core.http` antes de cada envío, ya conectado y
antes de escribir nada (F8-13d; la exclusión de la conversación o la persona, F8-13c): si no da
`True`, no sale nada. Solo se registran el estado y el `request-id`, nunca el cuerpo."""

import json
import re
import time
from collections.abc import Callable
from typing import Any

from django.conf import settings

from apps.ai_gateway import llm, probe, turns
from apps.ai_gateway.llm import Outcome, ProviderError
from core import http
from core.observability.logging import get_logger

logger = get_logger(__name__)
ATTEMPTS = 3  # la llamada y como mucho dos reintentos
INTERNAL = (400, 404, 409, 413)


def _count(usage: dict[str, Any], name: str, default: int | None = 0) -> int:
    """Un conteo del uso; los de entrada y salida son obligatorios (`default=None`)."""
    value = default if usage.get(name) is None else usage[name]
    if type(value) is not int or value < 0:
        raise ValueError(name)  # sin el valor
    return value


def _usage(usage: dict[str, Any]) -> llm.Usage:
    """El uso informado; una escritura en caché sin desglose cuenta al precio de 1 hora."""
    split = usage.get("cache_creation") or {}
    five, hour = (_count(split, f"ephemeral_{t}_input_tokens") for t in ("5m", "1h"))
    hour += max(_count(usage, "cache_creation_input_tokens") - five - hour, 0)
    return llm.Usage(_count(usage, "input_tokens", None), _count(usage, "output_tokens", None),
                     five, hour,
                     _count(usage, "cache_read_input_tokens"))  # fmt: skip


def _str(block: dict[str, Any], name: str) -> str:
    if type(block[name]) is not str:
        raise TypeError(name)  # sin el valor
    return str(block[name])


def _read(block: dict[str, Any]) -> llm.Block:
    """Un bloque de la respuesta en el formato neutro, con sus campos tal cual (F8-05b7). Otro tipo
    no se podría reenviar intacto: la respuesta no se usa y la reserva cuenta entera."""
    kind = block["type"]
    if kind == "text":
        return llm.Text(_str(block, "text"))
    if kind == "thinking":
        return llm.Thinking(_str(block, "thinking"), _str(block, "signature"))
    if kind == "redacted_thinking":
        return llm.RedactedThinking(_str(block, "data"))
    if kind == "tool_use" and type(block["input"]) is dict:
        return llm.ToolCall(_str(block, "id"), _str(block, "name"), block["input"])
    raise ValueError("block")  # sin el tipo


def _write(block: llm.Block) -> dict[str, Any]:
    """Un bloque neutro en la forma de la API, con los mismos textos, firmas y datos."""
    match block:
        case llm.Text(text):
            return {"type": "text", "text": text}
        case llm.Image(media_type, data):
            return {"type": "image", "source": {"type": "base64", "media_type": media_type,
                                                "data": data}}  # fmt: skip
        case llm.Thinking(thinking, signature):
            return {"type": "thinking", "thinking": thinking, "signature": signature}
        case llm.RedactedThinking(data):
            return {"type": "redacted_thinking", "data": data}
        case llm.ToolCall(id_, name, value):
            return {"type": "tool_use", "id": id_, "name": name, "input": value}
        case llm.ToolResult(tool_use_id, content, is_error):
            return {"type": "tool_result", "tool_use_id": tool_use_id, "is_error": is_error,
                    "content": [_write(inner) for inner in content]}  # fmt: skip
    raise TypeError("block")  # `turns.check` ya lo rechazó


def _parse(raw: bytes, request_id: str) -> llm.LLMResponse:
    try:
        data = json.loads(raw)
        blocks = tuple(_read(block) for block in data["content"])
        calls = tuple(b for b in blocks if type(b) is llm.ToolCall)
        text = "".join(b.text for b in blocks if type(b) is llm.Text)
        usage, stop = _usage(data["usage"]), str(data["stop_reason"])
        if calls:  # F8-07d: se reenviará tal cual; si no pasaría `turns.check`, es ilegible
            turns.reply(blocks)
    except ValueError, KeyError, TypeError, AttributeError, RecursionError, turns.Rejected:
        raise ProviderError(Outcome.PROVIDER_UNAVAILABLE, billed=True,  # pudo cobrarla
                            request_id=request_id) from None  # fmt: skip
    return llm.LLMResponse(text, calls, stop, usage, request_id, blocks)


class AnthropicAdapter:
    def encode(self, request: llm.LLMRequest, params: dict[str, Any]) -> bytes:
        output: dict[str, Any] = {"effort": params["effort"]}
        body: dict[str, Any] = {
            "model": params["model"], "max_tokens": params["max_tokens"], "output_config": output,
            "system": request.system,
            "messages": [{"role": t.role, "content": t.content if isinstance(t.content, str)
                          else [_write(b) for b in t.content]} for t in request.turns],
        }  # fmt: skip
        if request.tools:
            body["tools"] = [{"name": t.name, "description": t.description, "strict": True,
                              "input_schema": t.schema} for t in request.tools]  # fmt: skip
            body["tool_choice"] = {"type": "auto"}  # los modelos actuales rechazan forzarla
        if request.output_schema is not None:
            output["format"] = {"type": "json_schema", "schema": request.output_schema}
        return json.dumps(body, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()

    def send(
        self, key: str, body: bytes, seconds: float, proceed: Callable[[], bool]
    ) -> llm.LLMResponse:
        url = f"https://{settings.AI_PROVIDER_HOSTS['ANTHROPIC']}/v1/messages"
        headers = {"x-api-key": key, "anthropic-version": probe.API_VERSION,
                   "content-type": "application/json"}  # fmt: skip
        end, rid = time.monotonic() + seconds, ""
        with http.deadline(seconds):
            for attempt in range(ATTEMPTS):
                try:
                    reply = http.request("POST", url, headers=headers, body=body,
                                         read_seconds=end - time.monotonic(),
                                         before_send=proceed)  # fmt: skip
                except http.Withheld:  # conectado, sin escribir nada: no pudo facturar
                    raise ProviderError(Outcome.AI_WITHHELD, billed=False, request_id=rid) from None
                except (http.BlockedDestination, http.HTTPError, OSError) as error:
                    billed = not http.never_connected(error)  # salió: pudo cobrarla
                    logger.warning("ai.provider_call_failed", provider="ANTHROPIC", billed=billed)
                    raise ProviderError(Outcome.PROVIDER_UNAVAILABLE, billed=billed) from None
                rid = reply.headers.get("request-id", "")
                rid = rid[:64] if rid.isprintable() else ""
                logger.info("ai.provider_call", provider="ANTHROPIC", status=reply.status,
                            attempt=attempt, request_id=rid)  # fmt: skip
                if reply.status == 200:
                    return _parse(reply.data, rid)
                outcome = Outcome(probe.classify(reply.status, reply.data).value)
                if outcome == Outcome.PROVIDER_UNAVAILABLE and reply.status in INTERNAL:
                    outcome = Outcome.PROVIDER_ERROR  # un fallo nuestro: no se reintenta
                after = reply.headers.get("retry-after", "")
                wait = float(after) if re.fullmatch(r"[0-9]{1,5}", after) else 1.0
                again = outcome == Outcome.PROVIDER_UNAVAILABLE and attempt < ATTEMPTS - 1
                again = again and (reply.status == 429 or reply.status >= 500)
                if not again or time.monotonic() + wait >= end:
                    raise ProviderError(outcome, billed=False, request_id=rid)
                time.sleep(wait)  # el reenvío pasa otra vez por `proceed()` (F8-13c)
        raise AssertionError("inalcanzable")  # pragma: no cover
