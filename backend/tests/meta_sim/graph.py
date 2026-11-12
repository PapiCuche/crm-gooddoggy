"""Una Graph API falsa de Meta (WhatsApp Business): sin red y con los errores de ADR-029 §8.

`FakeGraph.handle(método, ruta, cabeceras, cuerpo)` devuelve `(estado, cabeceras, cuerpo)` o lanza
`Cut` o `Timeout`. Los códigos de error son los conocidos de Meta a la fecha del diseño: se
reconfirman con su documentación al implementar el adaptador (F7-05) y este módulo cambia con ellos.
"""

import json
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlsplit

from tests.meta_sim import GRAPH_VERSION
from tests.meta_sim.webhooks import DISPLAY_NUMBER, MEDIA_HOST, PHONE_NUMBER_ID, WABA_ID

TEST_TOKEN = "meta-sim-test-access-token-not-real"  # noqa: S105 — dato ficticio de test
# nombre -> (estado HTTP, código de Meta, subcódigo, título)
ERRORS: dict[str, tuple[int, int, int | None, str]] = {
    "token": (401, 190, None, "Invalid OAuth access token"),
    "rate_limit": (400, 130429, None, "Rate limit hit"),
    "pair_rate_limit": (400, 131056, None, "Pair rate limit hit"),
    "no_number": (400, 100, 33, "Unsupported post request. Object does not exist"),
    "window_closed": (400, 131047, None, "Re-engagement message"),
    "unreachable": (400, 131026, None, "Message undeliverable"),
    "quality": (400, 131048, None, "Spam rate limit hit"),
    "template": (404, 132001, None, "Template name does not exist in the translation"),
    "server": (500, 1, None, "An unknown error occurred"),
}


@dataclass
class FakeMedia:
    """Un medio de Meta: la consulta del id (`lookup`) y la descarga (`blob`) con su estado, el host
    de la URL que se devuelve y las cabeceras de la descarga."""

    content: bytes
    lookup: int = 200
    blob: int = 200
    host: str = MEDIA_HOST
    headers: dict[str, str] = field(default_factory=dict)


class Cut(Exception):  # noqa: N818 — la conexión se cortó antes de la respuesta
    pass


class Timeout(Exception):  # noqa: N818 — la respuesta no llegó a tiempo
    pass


Response = tuple[int, dict[str, str], bytes]
LOST = {
    "cut_after": Cut,
    "timeout_after": Timeout,
}  # Meta ya lo aceptó, pero la respuesta se pierde
OUTCOMES = ("ok", "slow", "cut", "timeout", *LOST, *ERRORS)


@dataclass
class FakeGraph:
    """Estado y comportamiento de la API falsa. `script` fija el resultado de las próximas
    llamadas de envío, una por llamada; sin script, el envío sale bien."""

    token: str = TEST_TOKEN
    numbers: dict[str, str] = field(default_factory=lambda: {PHONE_NUMBER_ID: WABA_ID})
    slow_seconds: float = 0.05
    script: deque[str] = field(default_factory=deque)
    sent: list[dict[str, Any]] = field(default_factory=list)  # lo que "se envió" a clientes
    requests: list[tuple[str, str]] = field(default_factory=list)  # (método, ruta) recibidos
    calls: list[dict[str, Any]] = field(default_factory=list)  # método, ruta, query y cabeceras
    quality: str = "GREEN"
    tier: str = "TIER_1K"
    media: dict[str, FakeMedia] = field(default_factory=dict)

    def then(self, *outcomes: str) -> FakeGraph:
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
        self.requests.append((method, parts.path))
        self.calls.append(
            {"method": method, "path": parts.path, "query": query, "headers": dict(headers)}
        )
        if {k.lower(): v for k, v in headers.items()}.get(
            "authorization"
        ) != f"Bearer {self.token}":
            return self._error("token")  # también la descarga de medios lleva el token
        if path[0] == "blob" and len(path) == 2 and path[1] in self.media:
            blob = self.media[path[1]]  # la descarga: `Location` de una redirección va en headers
            return blob.blob, {**blob.headers}, blob.content if blob.blob == 200 else b""
        if path[0] != GRAPH_VERSION:
            return self._error("no_number", f"Unsupported version {path[0]}")
        match (method, path[1:]):
            case ("POST", [number, "messages"]):
                return self._send(number, body)
            case ("GET", [media]) if media in self.media:  # la consulta: debe traer el número
                if query.get("phone_number_id", [""])[0] not in self.numbers:
                    return self._error("no_number")
                item = self.media[media]
                if item.lookup != 200:
                    reply = {"error": {"message": "Media unavailable", "code": 100}}
                    return item.lookup, {}, json.dumps(reply).encode()
                return self._ok({
                    "messaging_product": "whatsapp", "id": media, "mime_type": "image/jpeg",
                    "file_size": len(item.content), "sha256": "0" * 64,
                    "url": f"https://{item.host}/blob/{media}",
                })  # fmt: skip
            case ("GET", [number]) if number in self.numbers:
                return self._ok(self._number(number, query))
            case ("GET", [waba, "phone_numbers"]) if waba in self.numbers.values():
                found = [self._number(n, query) for n, w in self.numbers.items() if w == waba]
                return self._ok({"data": found})
        return self._error("no_number")

    def _number(self, number: str, query: dict[str, list[str]]) -> dict[str, Any]:
        return {
            "id": number, "display_phone_number": DISPLAY_NUMBER,
            "verified_name": "Negocio de prueba", "quality_rating": self.quality,
            "messaging_limit_tier": self.tier,
        }  # fmt: skip

    def _send(self, number: str, body: bytes | None) -> Response:
        if number not in self.numbers:
            return self._error("no_number")
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
            valid = (
                sent["messaging_product"] == "whatsapp" and sent["to"] and sent["type"] == "text"
            )
            valid = valid and isinstance(sent["text"]["body"], str)
        except ValueError, KeyError, TypeError:
            valid = False
        if not valid:
            return self._error("no_number", "Invalid parameter")
        self.sent.append({"phone_number_id": number, **sent})
        wamid = f"wamid.SIMOUT{len(self.sent):06d}"
        if outcome in LOST:
            raise LOST[outcome]
        return self._ok({
            "messaging_product": "whatsapp",
            "contacts": [{"input": sent["to"], "wa_id": sent["to"]}],
            "messages": [{"id": wamid}],
        })  # fmt: skip

    @staticmethod
    def _ok(payload: dict[str, Any]) -> Response:
        return 200, {"Content-Type": "application/json"}, json.dumps(payload).encode()

    @staticmethod
    def _error(name: str, message: str | None = None) -> Response:
        status, code, subcode, title = ERRORS[name]
        error: dict[str, Any] = {
            "message": message or title,
            "type": "OAuthException",
            "code": code,
            "fbtrace_id": "SIMTRACE0001",
        }
        if subcode is not None:
            error["error_subcode"] = subcode
        return status, {"Content-Type": "application/json"}, json.dumps({"error": error}).encode()
