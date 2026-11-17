"""El simulador de Meta como servidor HTTP del stack de desarrollo (F7b-06c, ADR-041): sirve
`FakePageGraph` (Messenger e Instagram) y `FakeGraph` (WhatsApp) por http, para que el backend con
los settings `local` y `META_GRAPH_SIMULATOR_URL` conecte cuentas y responda sin Meta. Vive en
`tests/`, que ninguna imagen copia (`backend/.dockerignore` y el `Dockerfile`); solo lo arranca el
compose de desarrollo con `--profile meta-sim`. Solo la biblioteca estándar.

Además de la Graph API falsa: `GET /_sim/state` (lo enviado y las páginas suscritas) y
`POST /_sim/then` con `{"page": [...], "whatsapp": [...]}` (los resultados de las próximas
llamadas, los de `then`). Uso: `python -m tests.meta_sim.server --port 8090`.
"""

import argparse
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

from tests.meta_sim.graph import TEST_TOKEN, Cut, FakeGraph, Response, Timeout
from tests.meta_sim.page_graph import FakePageGraph

HANG_SECONDS = 20.0  # un `timeout` del guion: más que la lectura de `core.http` (15 s)
BODY_MAX = 32 << 20  # más que un medio de WhatsApp; un cuerpo mayor es 413 sin leerlo
CALLS_MAX = 500  # el registro de llamadas de las Graph API falsas, solo las últimas


class Simulator:
    """Las dos Graph API falsas detrás de una sola, con un cerrojo: no son seguras entre hilos.
    Una ruta cuyo primer id es un número o un WABA de `FakeGraph` va a WhatsApp; el resto, a la
    página."""

    def __init__(self, token: str = TEST_TOKEN) -> None:
        self.page, self.whatsapp = FakePageGraph(token=token), FakeGraph(token=token)
        self.lock = threading.Lock()

    def handle(
        self, method: str, target: str, headers: dict[str, str], body: bytes | None
    ) -> Response:
        path = urlsplit(target).path
        with self.lock:
            if path.startswith("/_sim/"):
                return self._admin(method, path, body)
            first = path.strip("/").split("/")[1:2]
            numbers = self.whatsapp.numbers
            whatsapp = bool(first) and (first[0] in numbers or first[0] in numbers.values())
            try:
                return (self.whatsapp if whatsapp else self.page).handle(
                    method, target, headers, body
                )
            finally:  # un stack que corre días no crece sin fin (lo enviado sí se guarda)
                for log in (self.page.calls, self.whatsapp.calls, self.whatsapp.requests):
                    del log[:-CALLS_MAX]

    def _admin(self, method: str, path: str, body: bytes | None) -> Response:
        if (method, path) == ("GET", "/_sim/state"):
            return _json(200, {
                "subscribed": self.page.subscribed, "page_sent": self.page.sent,
                "whatsapp_sent": self.whatsapp.sent,
            })  # fmt: skip
        if (method, path) == ("POST", "/_sim/then"):
            try:
                wanted = json.loads(body or b"{}")
                self.page.then(*wanted.get("page", []))
                self.whatsapp.then(*wanted.get("whatsapp", []))
            except ValueError, AttributeError, TypeError:
                return _json(400, {"error": "resultado desconocido"})
            queued = {"page": list(self.page.script), "whatsapp": list(self.whatsapp.script)}
            return _json(200, queued)
        return _json(404, {"error": "no existe"})


def _json(status: int, payload: Any) -> Response:
    return status, {"Content-Type": "application/json"}, json.dumps(payload).encode()


def handler(simulator: Simulator) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _serve(self) -> None:
            declared = self.headers.get("Content-Length") or "0"
            valid = re.fullmatch(r"[0-9]{1,10}", declared) is not None
            if not valid or int(declared) > BODY_MAX:  # sin leer el cuerpo ni reusar la conexión
                self.close_connection = True
                return self._reply(*_json(413 if valid else 400, {"error": "Content-Length"}))
            size = int(declared)
            body = self.rfile.read(size) if size else None
            try:
                status, headers, payload = simulator.handle(
                    self.command, self.path, dict(self.headers.items()), body
                )
            except Cut:  # Meta corta la conexión sin responder
                self.close_connection = True
                return
            except Timeout:
                time.sleep(HANG_SECONDS)
                self.close_connection = True
                return
            self._reply(status, headers, payload)

        def _reply(self, status: int, headers: dict[str, str], payload: bytes) -> None:
            self.send_response(status)
            for name, value in {**headers, "Content-Length": str(len(payload))}.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(payload)

        do_GET = do_POST = do_DELETE = _serve

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            pass  # la ruta lleva ids; el estado se ve en `/_sim/state`

    return Handler


def serve(host: str, port: int, simulator: Simulator | None = None) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), handler(simulator or Simulator()))
    server.daemon_threads = True
    return server


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Simulador local de la Graph API de Meta")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    args = parser.parse_args()
    print(f"meta-sim en http://{args.host}:{args.port}", flush=True)  # noqa: T201
    serve(args.host, args.port).serve_forever()
