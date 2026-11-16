"""La Graph API falsa de una página de Facebook: Messenger e Instagram con Facebook Login (ADR-035
§2, §3, §6 y §9). Sirve `GET /<PAGE_ID>`, `POST /<PAGE_ID>/subscribed_apps` y
`POST /<PAGE_ID>/messages` [S17] [S20], con la misma `GRAPH_VERSION` que WhatsApp (§6, D-F7-08).

Los códigos y subcódigos son los de ADR-035 §6 [S26] y se reconfirman en F7b-05. Las fuentes no
dan el estado HTTP de cada uno: se sigue la convención de `graph.py` (401 el token, 400 el resto,
500 el servidor) y el adaptador clasifica por `code` y `error_subcode`, nunca por el estado.
"""

import json
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlsplit

from tests.meta_sim import GRAPH_VERSION
from tests.meta_sim.graph import LOST, TEST_TOKEN, Cut, Response, Timeout
from tests.meta_sim.social import IG_ACCOUNT_ID, PAGE_ID

# nombre -> (estado HTTP, código de Meta, subcódigo, título)
ERRORS: dict[str, tuple[int, int, int | None, str]] = {
    "window_closed": (400, 10, 2018278, "This message is sent outside of allowed window."),
    "window_closed_ig": (400, 10, 2534022, "This message is sent outside of allowed window."),
    "unreachable": (400, 551, None, "This person isn't receiving messages from you right now."),
    "token": (401, 190, None, "Invalid OAuth access token."),
    "rate_limit": (400, 613, None, "Calls to this api have exceeded the rate limit."),
    "messages_access_off": (400, 200, 2534041, "The account owner has disabled access to "
                            "Instagram Direct Messaging."),
    "invalid": (400, 100, None, "Invalid parameter"),
    "server": (500, 1, None, "An unknown error occurred"),
}  # fmt: skip
MESSAGING_TYPES = ("RESPONSE", "UPDATE", "MESSAGE_TAG")  # [S20]
TAGS = ("HUMAN_AGENT",)  # las demás dan `100` desde el 27-04-2026 [S20]
OUTCOMES = ("ok", "slow", "cut", "timeout", *LOST, *ERRORS)


@dataclass
class FakePageGraph:
    """`pages` va de cada página a la cuenta de Instagram vinculada (o `None`). `script` fija el
    resultado de los próximos envíos, uno por llamada; sin script, el envío sale bien."""

    token: str = TEST_TOKEN
    pages: dict[str, str | None] = field(default_factory=lambda: {PAGE_ID: IG_ACCOUNT_ID})
    slow_seconds: float = 0.05
    script: deque[str] = field(default_factory=deque)
    sent: list[dict[str, Any]] = field(default_factory=list)  # lo que "se envió" a clientes
    subscribed: dict[str, list[str]] = field(default_factory=dict)  # página -> campos
    calls: list[dict[str, Any]] = field(default_factory=list)  # método, ruta y query

    def then(self, *outcomes: str) -> FakePageGraph:
        unknown = set(outcomes) - set(OUTCOMES)
        if unknown:
            raise ValueError(f"resultado desconocido: {sorted(unknown)}")
        self.script.extend(outcomes)
        return self

    def handle(
        self, method: str, target: str, headers: dict[str, str], body: bytes | None
    ) -> Response:
        parts = urlsplit(target)
        path, query = parts.path.strip("/").split("/"), parse_qs(parts.query)
        self.calls.append({"method": method, "path": parts.path, "query": query})
        auth = {k.lower(): v for k, v in headers.items()}.get("authorization")
        if auth != f"Bearer {self.token}":
            return self._error("token")
        if path[0] != GRAPH_VERSION:
            return self._error("invalid", f"Unsupported version {path[0]}")
        match (method, path[1:]):
            case ("GET", [page]) if page in self.pages:  # sin `fields`, solo `id` y `name`
                found: dict[str, Any] = {"id": page, "name": "Página de prueba"}
                fields = ",".join(query.get("fields", [])).split(",")
                if self.pages[page] is not None and "instagram_business_account" in fields:
                    found["instagram_business_account"] = {"id": self.pages[page]}
                return self._ok(found)
            case ("POST", [page, "subscribed_apps"]) if page in self.pages:
                wanted = ",".join(query.get("subscribed_fields", [])).split(",")
                if wanted == [""]:
                    return self._error("invalid")
                self.subscribed[page] = wanted
                return self._ok({"success": True})
            case ("POST", [page, "messages"]) if page in self.pages:
                return self._send(page, body)
        return self._error("invalid", "Unsupported request. Object does not exist")

    def _send(self, page: str, body: bytes | None) -> Response:
        outcome = self.script.popleft() if self.script else "ok"
        if outcome == "cut":
            raise Cut
        if outcome == "timeout":
            raise Timeout
        if outcome == "slow":
            time.sleep(self.slow_seconds)
        elif outcome in ERRORS:
            return self._error(outcome)
        try:
            sent = json.loads(body or b"")
            kind, tag = sent["messaging_type"], sent.get("tag")
            valid = isinstance(sent["recipient"]["id"], str) and bool(sent["recipient"]["id"])
            valid = valid and isinstance(sent["message"]["text"], str) and kind in MESSAGING_TYPES
            valid = valid and (kind != "MESSAGE_TAG" or tag in TAGS)
        except ValueError, KeyError, TypeError:
            valid = False
        if not valid:
            return self._error("invalid")
        self.sent.append({"page_id": page, **sent})
        mid = f"m_SIMOUT{len(self.sent):06d}"
        if outcome in LOST:
            raise LOST[outcome]  # Meta lo envió, pero la respuesta se pierde (no deduplica, §6)
        return self._ok({"recipient_id": sent["recipient"]["id"], "message_id": mid})

    @staticmethod
    def _ok(payload: dict[str, Any]) -> Response:
        return 200, {"Content-Type": "application/json"}, json.dumps(payload).encode()

    @staticmethod
    def _error(name: str, message: str | None = None) -> Response:
        status, code, subcode, title = ERRORS[name]
        error: dict[str, Any] = {
            "message": message or title, "type": "OAuthException", "code": code,
            "fbtrace_id": "SIMTRACE0002",
        }  # fmt: skip
        if subcode is not None:
            error["error_subcode"] = subcode
        return status, {"Content-Type": "application/json"}, json.dumps({"error": error}).encode()
