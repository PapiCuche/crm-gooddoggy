"""Una API de Anthropic falsa para las pruebas de `apps.ai_gateway` (F8-03b), sin red: contesta
`GET /v1/models` y `POST /v1/messages` con las respuestas y errores documentados; `install` cambia
solo la capa de red de `core.http` (como `tests.meta_sim.transport`), así que su política real
(https, allowlist, cabeceras) sigue aplicándose. Cada cuerpo de error lleva `BODY_MARK`: una prueba
comprueba que ningún cuerpo del proveedor sale del CRM. Ninguna clave es real."""

import io
import json
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
import urllib3
from urllib3.exceptions import ConnectTimeoutError, ProtocolError, ReadTimeoutError

from core import http

HOST = "api.anthropic.com"
TEST_KEY = "fake-anthropic-key-not-real-0001"  # ficticia
BODY_MARK = "provider-body-must-not-leave"
REQUEST_ID = "req_fake_0001"
# nombre -> (estado HTTP, tipo de error, mensaje, `error.details.error_code`)
ERRORS: dict[str, tuple[int, str, str, str | None]] = {
    "unauthorized": (401, "authentication_error", "invalid x-api-key", None),
    "forbidden": (403, "permission_error", "not allowed", None),
    "billing": (402, "billing_error", "credit balance too low", None),
    "spend_limit": (400, "invalid_request_error",
                    "You have reached your specified API usage limits.", None),
    "tier_limit": (429, "rate_limit_error", "spend limit", "enforced_spend_limit_reached"),
    "rate_limit": (429, "rate_limit_error", "rate limited", None),
    "server": (500, "api_error", "internal error", None),
    "overloaded": (529, "overloaded_error", "overloaded", None),
    "bad_request": (400, "invalid_request_error", "bad request", None),
    "not_found": (404, "not_found_error", "not found", None),
    "teapot": (418, "api_error", "unexpected", None),  # F8-05: un estado no documentado
}  # fmt: skip
OUTCOMES = ("ok", "garbage", "cut", "timeout", *ERRORS)


@dataclass
class FakeAnthropic:
    """`outcome` fija la respuesta de las próximas llamadas; otra clave que `key` es un 401."""

    key: str = TEST_KEY
    outcome: str = "ok"
    calls: list[dict[str, Any]] = field(default_factory=list)
    retries: list[Any] = field(default_factory=list)  # lo que pidió `core.http`, por llamada
    timeouts: list[Any] = field(default_factory=list)
    body: bytes | None = None  # el de la petición en curso
    request_id, retry_after = REQUEST_ID, "2"  # F8-05: las cabeceras de la respuesta
    script: list[str] = field(default_factory=list)  # F8-05: un resultado por llamada, antes
    usage: dict[str, Any] = field(default_factory=lambda: {"input_tokens": 9, "output_tokens": 9})
    content: list[dict[str, Any]] | None = None  # F8-05b7: los bloques de la respuesta, si no texto

    def handle(self, method: str, url: str, headers: dict[str, str]) -> tuple[int, bytes]:
        parts, body = urlsplit(url), self.body
        self.calls.append(
            {"method": method, "path": parts.path, "query": parse_qs(parts.query),
             "headers": dict(headers), "body": json.loads(body) if body else None}
        )  # fmt: skip
        outcome = self.script.pop(0) if self.script else self.outcome
        if outcome == "cut":
            raise ProtocolError("Connection aborted.")
        if outcome == "unreachable":  # F8-05: no llegó a conectar
            raise ConnectTimeoutError(None, url, "Connect timed out.")
        if outcome == "timeout":
            raise ReadTimeoutError(None, url, "Read timed out.")  # type: ignore[arg-type]
        name = "unauthorized" if headers.get("x-api-key") != self.key else outcome
        route = (method, parts.path)
        if route not in {("GET", "/v1/models"), ("POST", "/v1/messages")} and name == "ok":
            name = "not_found"
        if name == "ok" and route == ("POST", "/v1/messages"):
            text = [{"type": "text", "text": f"respuesta {BODY_MARK}"}]
            reply = {
                "content": text if self.content is None else self.content,
                "stop_reason": "end_turn",
                "usage": self.usage,
            }
            return 200, json.dumps({"id": "msg_fake", "type": "message", **reply}).encode()
        if name == "ok":
            data = [{"type": "model", "id": "claude-fake-model", "display_name": BODY_MARK}]
            return 200, json.dumps({"data": data, "has_more": True}).encode()
        if name == "garbage":
            return 200, f"<html>{BODY_MARK}</html>".encode()
        status, kind, message, code = ERRORS[name]
        error: dict[str, Any] = {"type": kind, "message": f"{message} {BODY_MARK}"}
        if code:
            error["details"] = {"error_code": code}
        return status, json.dumps({"type": "error", "error": error}).encode()


class Connected:
    sock = object()  # ya conectada: `before_send` corre sin abrir nada


class _Manager:
    def __init__(self, fake: FakeAnthropic) -> None:
        self.fake = fake

    def request(
        self, method: str, url: str, *, headers: dict[str, str] | None = None, **options: Any
    ) -> urllib3.BaseHTTPResponse:
        if urlsplit(url).hostname != HOST:  # nada fuera del simulador
            raise AssertionError(f"el simulador no atiende {urlsplit(url).hostname}")
        self.fake.retries.append(options.get("retries", "default"))  # el gestor real reintentaría
        self.fake.timeouts.append(options.get("timeout"))
        http._checked(Connected())  # type: ignore[arg-type]  # F8-13d: como la conexión real
        self.fake.body = options.get("body")
        status, body = self.fake.handle(method, url, dict(headers or {}))
        reply_headers = {"request-id": self.fake.request_id, "content-type": "application/json"}
        if status == 429:  # como la API real: un 429 dice cuándo volver
            reply_headers["retry-after"] = self.fake.retry_after
        return urllib3.HTTPResponse(
            body=io.BytesIO(body), headers=reply_headers, status=status, preload_content=False
        )


def install(monkeypatch: pytest.MonkeyPatch, fake: FakeAnthropic) -> FakeAnthropic:
    monkeypatch.setattr(http, "_manager", _Manager(fake))
    return fake
