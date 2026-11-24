"""La Graph API falsa de una página de Facebook: Messenger e Instagram con Facebook Login (ADR-035
§2, §3, §6 y §9). Sirve `GET /<PAGE_ID>`, `GET`, `POST` y `DELETE /<PAGE_ID>/subscribed_apps` y
`POST /<PAGE_ID>/messages` [S17] [S20], y el perfil `GET /<IGSID>?fields=name,username` de la User
Profile API [S9] (F7b-04c), con la misma `GRAPH_VERSION` que WhatsApp (§6, D-F7-08).

Los códigos y subcódigos son los de ADR-035 §6 [S26] y se reconfirman en F7b-05. Las fuentes no
dan el estado HTTP de cada uno: el token sale con 401 y con 400 (la API de páginas suele mandar
`OAuthException` con 400), así que el adaptador clasifica por `code` y `error_subcode`, nunca por
el estado. El último campo de `ERRORS` es el código propio que §6 exige para cada caso: un `10` o
un `200` sin el subcódigo de §6 es `PROVIDER_REJECTED`, no `WINDOW_CLOSED` ni `MESSAGES_ACCESS_OFF`.
"""

import json
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlsplit

from tests.meta_sim import GRAPH_VERSION, social
from tests.meta_sim.graph import LOST, TEST_TOKEN, Cut, Response, Timeout
from tests.meta_sim.social import APP_ID, IG_ACCOUNT_ID, IGSID, PAGE_ID
from tests.meta_sim.webhooks import TEST_APP_SECRET

APP_TOKEN = f"{APP_ID}|{TEST_APP_SECRET}"  # el token de la app: `<id>|<secret>` (debug_token)

# nombre -> (estado HTTP, código de Meta, subcódigo, título, código propio de ADR-035 §6)
ERRORS: dict[str, tuple[int, int, int | None, str, str]] = {
    "window_closed": (400, 10, 2018278, "This message is sent outside of allowed window.",
                      "WINDOW_CLOSED"),
    "window_closed_2534022": (400, 10, 2534022, "This message is sent outside of allowed window.",
                              "WINDOW_CLOSED"),
    "unreachable": (400, 551, None, "This person isn't receiving messages from you right now.",
                    "RECIPIENT_UNREACHABLE"),
    "token": (401, 190, None, "Invalid OAuth access token.", "TOKEN_INVALID"),
    "token_400": (400, 190, None, "Invalid OAuth access token.", "TOKEN_INVALID"),
    "rate_limit": (400, 613, None, "Calls to this api have exceeded the rate limit.",
                   "RATE_LIMITED"),
    "throttle_pages": (400, 80001, None, "There have been too many calls to this Page account.",
                       "RATE_LIMITED"),  # F7b-06i: los límites de uso del negocio
    "throttle_instagram": (400, 80002, None, "There have been too many calls to this Instagram "
                           "account.", "RATE_LIMITED"),
    "throttle_messenger": (400, 80006, None, "There have been too many messages sent to this "
                           "Messenger account.", "RATE_LIMITED"),
    "messages_access_off": (400, 200, 2534041, "The account owner has disabled access to "
                            "Instagram Direct Messaging.", "MESSAGES_ACCESS_OFF"),
    "permission_10": (400, 10, None, "Application does not have permission for this action",
                      "PROVIDER_REJECTED"),
    "permission_200": (400, 200, None, "Permissions error", "PROVIDER_REJECTED"),
    "invalid": (400, 100, None, "Invalid parameter", "PROVIDER_REJECTED"),
    "server": (500, 1, None, "An unknown error occurred", "PROVIDER_ERROR"),
}  # fmt: skip
MESSAGING_TYPES = ("RESPONSE", "UPDATE", "MESSAGE_TAG")  # [S20]
TAGS = ("HUMAN_AGENT",)  # las demás dan `100` desde el 27-04-2026 [S20]
# Un 200 que no dice con qué id lo aceptó (sin `message_id`, o uno que no es un id): Meta lo envió.
NO_MID = {"no_mid": None, "bad_mid": "m_ con espacio"}
OUTCOMES = ("ok", "slow", "cut", "timeout", *LOST, *NO_MID, *ERRORS)
PROFILE_FIELDS = ("name", "username")  # los que lee D-F7b-06 de los que da [S9]


@dataclass
class FakePageGraph:
    """`pages` va de cada página a la cuenta de Instagram vinculada (o `None`). `script` fija el
    resultado de los próximos envíos y lecturas de perfil, uno por llamada; sin script, salen bien.
    `profiles` va de cada IGSID a lo que su perfil devuelve (cualquier valor, para probar el
    saneado); un IGSID que no está da el `100` «no existe»."""

    token: str = TEST_TOKEN
    pages: dict[str, str | None] = field(default_factory=lambda: {PAGE_ID: IG_ACCOUNT_ID})
    slow_seconds: float = 0.05
    script: deque[str] = field(default_factory=deque)
    sent: list[dict[str, Any]] = field(default_factory=list)  # lo que "se envió" a clientes
    subscribed: dict[str, list[str]] = field(default_factory=dict)  # página -> campos
    calls: list[dict[str, Any]] = field(default_factory=list)  # método, ruta y query
    no_role: set[str] = field(default_factory=set)  # páginas sin rol: no leen sus apps (F7b-06e)
    # F7b-12: lo que `debug_token` dice del token (segundos Unix; `0`, nunca), `None` = un `100`.
    debug: dict[str, Any] | None = field(
        default_factory=lambda: {"expires_at": 0, "data_access_expires_at": 0}
    )
    profiles: dict[str, dict[str, Any]] = field(
        default_factory=lambda: {IGSID: {"name": "Cliente Simulado", "username": "cliente.sim"}}
    )

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
        if (method, path[1:]) == ("GET", ["debug_token"]):  # con el token de la app [F7b-12]
            if auth != f"Bearer {APP_TOKEN}" or self.debug is None:
                return self._error("token" if auth != f"Bearer {APP_TOKEN}" else "invalid")
            valid = query.get("input_token") == [self.token]
            return self._ok({"data": {"app_id": APP_ID, "type": "PAGE", "is_valid": valid,
                                      "profile_id": PAGE_ID, **self.debug}})  # fmt: skip
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
            case ("GET", [page, "subscribed_apps"]) if page in self.pages:
                if page in self.no_role:
                    return self._error("permission_200")
                mine = {"id": APP_ID, "subscribed_fields": self.subscribed.get(page)}
                return self._ok({"data": [mine] if page in self.subscribed else []})
            case ("POST", [page, "subscribed_apps"]) if page in self.pages:
                wanted = ",".join(query.get("subscribed_fields", []) or _form(body)).split(",")
                if wanted == [""]:
                    return self._error("invalid")
                self.subscribed[page] = wanted
                return self._ok({"success": True})
            case ("DELETE", [page, "subscribed_apps"]) if page in self.pages:  # F7b-06g
                self.subscribed.pop(page, None)  # la página deja de tener la app suscrita
                return self._ok({"success": True})
            case ("POST", [page, "messages"]) if page in self.pages:
                return self._send(page, body)
            case ("GET", [user]) if user in self.profiles:
                return self._profile(user, ",".join(query.get("fields", [])).split(","))
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
            meta = sent["message"].get("metadata")  # hasta 1000 caracteres [S19]
            valid = valid and (meta is None or isinstance(meta, str) and 1 <= len(meta) <= 1000)
        except ValueError, KeyError, TypeError:
            valid = False
        if not valid:
            return self._error("invalid")
        self.sent.append({"page_id": page, **sent})
        mid = f"m_SIMOUT{len(self.sent):06d}"
        if outcome in LOST:
            raise LOST[outcome]  # Meta lo envió, pero la respuesta se pierde (no deduplica, §6)
        if outcome in NO_MID:
            reply = {"recipient_id": sent["recipient"]["id"], "message_id": NO_MID[outcome]}
            return self._ok({k: v for k, v in reply.items() if v is not None})
        return self._ok({"recipient_id": sent["recipient"]["id"], "message_id": mid})

    def echo(
        self, channel: social.Channel, n: int = 1, app_id: str | None = APP_ID
    ) -> dict[str, Any]:
        """El eco del envío `n` (desde 1): su `mid` y la misma `metadata` que se envió [S19]
        (SIN VERIFICAR 11: que vuelva igual). `app_id=None`, el eco de Instagram sin él."""
        sent = self.sent[n - 1]["message"]
        return social.echo(
            channel, sent["text"], mid=f"m_SIMOUT{n:06d}", app_id=app_id,
            metadata=sent.get("metadata"),
        )  # fmt: skip

    def _profile(self, user: str, fields: list[str]) -> Response:
        outcome = self.script.popleft() if self.script else "ok"
        if outcome in ("cut", *LOST):
            raise Cut
        if outcome == "timeout":
            raise Timeout
        if outcome in ERRORS:
            return self._error(outcome)
        found = self.profiles[user]
        return self._ok({k: found[k] for k in PROFILE_FIELDS if k in fields and k in found})

    @staticmethod
    def _ok(payload: dict[str, Any]) -> Response:
        return 200, {"Content-Type": "application/json"}, json.dumps(payload).encode()

    @staticmethod
    def _error(name: str, message: str | None = None) -> Response:
        status, code, subcode, title, _ = ERRORS[name]
        error: dict[str, Any] = {
            "message": message or title, "type": "OAuthException", "code": code,
            "fbtrace_id": "SIMTRACE0002",
        }  # fmt: skip
        if subcode is not None:
            error["error_subcode"] = subcode
        return status, {"Content-Type": "application/json"}, json.dumps({"error": error}).encode()


def _form(body: bytes | None) -> list[str]:
    """`subscribed_fields` también como parámetro del cuerpo: JSON o formulario."""
    try:
        found = json.loads(body or b"{}").get("subscribed_fields", [])
    except ValueError, AttributeError:
        found = parse_qs((body or b"").decode(errors="replace")).get("subscribed_fields", [])
    return [found] if isinstance(found, str) else [str(f) for f in found]
