"""El adaptador de Anthropic (F8-05; ADR-034 §3): la Messages API por `core.http`, nunca con el
SDK; tools con `tool_choice: auto` y la respuesta final con `output_config.format`. Como mucho dos
reintentos, solo tras un 429 o un 5xx (sin uso) y dentro del plazo; nunca tras un tiempo agotado o
un corte (pudo facturar). Solo se registran el estado y el `request-id`, nunca el cuerpo."""

import json
import time
from typing import Any

from django.conf import settings

from apps.ai_gateway import llm, probe
from apps.ai_gateway.llm import Outcome, ProviderError
from core import http
from core.observability.logging import get_logger

logger = get_logger(__name__)
ATTEMPTS = 3  # la llamada y como mucho dos reintentos
INTERNAL = (400, 404, 409, 413)


def _turn(turn: llm.Turn) -> dict[str, Any]:
    if turn.raw:
        return {"role": turn.role, "content": list(turn.raw)}
    content: list[dict[str, Any]] = [{"type": "text", "text": turn.text}] if turn.text else []
    content += [{"type": "tool_use", "id": c.id, "name": c.name, "input": c.input}
                for c in turn.tool_calls]  # fmt: skip
    content += [{"type": "tool_result", "tool_use_id": r.id, "content": r.content,
                 "is_error": r.is_error} for r in turn.tool_results]  # fmt: skip
    return {"role": turn.role, "content": content}


def _count(usage: dict[str, Any], name: str) -> int:
    value = usage.get(name) or 0
    if type(value) is not int or value < 0:
        raise ValueError(name)  # sin el valor
    return value


def _usage(usage: dict[str, Any]) -> llm.Usage:
    """El uso informado; una escritura en caché sin desglose cuenta al precio de 1 hora."""
    split = usage.get("cache_creation") or {}
    five, hour = (_count(split, f"ephemeral_{t}_input_tokens") for t in ("5m", "1h"))
    hour += max(_count(usage, "cache_creation_input_tokens") - five - hour, 0)
    return llm.Usage(_count(usage, "input_tokens"), _count(usage, "output_tokens"), five, hour,
                     _count(usage, "cache_read_input_tokens"))  # fmt: skip


def _parse(raw: bytes, request_id: str) -> llm.LLMResponse:
    try:
        data = json.loads(raw)
        blocks = data["content"]
        calls = tuple(llm.ToolCall(b["id"], b["name"], b["input"]) for b in blocks
                      if b["type"] == "tool_use")  # fmt: skip
        text = "".join(b["text"] for b in blocks if b["type"] == "text")
        usage, stop = _usage(data["usage"]), str(data["stop_reason"])
    except ValueError, KeyError, TypeError, AttributeError, RecursionError:
        raise ProviderError(Outcome.PROVIDER_UNAVAILABLE, billed=True) from None  # pudo cobrarla
    return llm.LLMResponse(text, calls, stop, usage, request_id, tuple(blocks))


class AnthropicAdapter:
    def encode(self, request: llm.LLMRequest, params: dict[str, Any]) -> bytes:
        output: dict[str, Any] = {"effort": params["effort"]}
        body: dict[str, Any] = {
            "model": params["model"], "max_tokens": params["max_tokens"],
            "system": request.system, "messages": [_turn(t) for t in request.turns],
            "output_config": output,
        }  # fmt: skip
        if request.tools:
            body["tools"] = [{"name": t.name, "description": t.description, "strict": True,
                              "input_schema": t.schema} for t in request.tools]  # fmt: skip
            body["tool_choice"] = {"type": "auto"}  # los modelos actuales rechazan forzarla
        if request.output_schema is not None:
            output["format"] = {"type": "json_schema", "schema": request.output_schema}
        return json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()

    def send(self, key: str, body: bytes, seconds: float) -> llm.LLMResponse:
        url = f"https://{settings.AI_PROVIDER_HOSTS['ANTHROPIC']}/v1/messages"
        headers = {"x-api-key": key, "anthropic-version": probe.API_VERSION,
                   "content-type": "application/json"}  # fmt: skip
        end = time.monotonic() + seconds
        with http.deadline(seconds):
            for attempt in range(ATTEMPTS):
                try:
                    reply = http.request("POST", url, headers=headers, body=body,
                                         read_seconds=end - time.monotonic())  # fmt: skip
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
                wait = float(after) if after.isdigit() and len(after) < 6 else 1.0
                again = outcome == Outcome.PROVIDER_UNAVAILABLE and attempt < ATTEMPTS - 1
                again = again and (reply.status == 429 or reply.status >= 500)
                if not again or time.monotonic() + wait >= end:
                    raise ProviderError(outcome, billed=False)
                time.sleep(wait)
        raise AssertionError("inalcanzable")  # pragma: no cover
