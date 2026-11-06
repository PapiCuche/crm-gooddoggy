"""Cómo se sirve `FakeGraph`.

1. `install(monkeypatch, graph)` sustituye solo la capa de red de `core.http` (el gestor de
   conexiones de urllib3): su política (https, puerto 443, allowlist, `Host`) sigue siendo la real,
   y la prueba fija `HTTP_ALLOWED_HOSTS` con `override_settings`.
2. `serve(graph)` abre un servidor HTTP en un puerto efímero de 127.0.0.1, en un hilo, para probar
   el simulador por un socket de verdad y para el entorno local (`python -m
   tests.meta_sim.transport`). `core.http` no llega a él a propósito (solo https e IP públicas);
   F7-05 conectará el adaptador con un transporte de pruebas explícito, sin debilitar `core.http`.
"""

import io
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

import pytest
import urllib3
from urllib3.exceptions import ProtocolError, ReadTimeoutError

from core import http
from tests.meta_sim.graph import Cut, FakeGraph, Timeout

GRAPH_HOST = "graph.facebook.com"


class _Manager:
    """El `PoolManager` falso: contesta `request` con el estado y el cuerpo de `FakeGraph`."""

    def __init__(self, graph: FakeGraph, hosts: tuple[str, ...]) -> None:
        self.graph, self.hosts, self.calls = graph, hosts, 0

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
        json: Any = None,
        redirect: bool = True,
        **_: Any,
    ) -> urllib3.BaseHTTPResponse:
        import json as jsonlib

        parts = urlsplit(url)
        if parts.hostname not in self.hosts:  # nada fuera del simulador
            raise AssertionError(f"el simulador no atiende {parts.hostname}")
        sent = jsonlib.dumps(json).encode() if json is not None else body
        self.calls += 1
        target = parts.path + (f"?{parts.query}" if parts.query else "")
        try:
            status, reply_headers, payload = self.graph.handle(
                method, target, dict(headers or {}), sent
            )
        except Cut:
            raise ProtocolError("Connection aborted.") from None
        except Timeout:
            raise ReadTimeoutError(None, url, "Read timed out.") from None  # type: ignore[arg-type]
        return urllib3.HTTPResponse(
            body=io.BytesIO(payload), headers=reply_headers, status=status, preload_content=True
        )


def install(monkeypatch: pytest.MonkeyPatch, graph: FakeGraph, *hosts: str) -> _Manager:
    manager = _Manager(graph, hosts or (GRAPH_HOST,))
    monkeypatch.setattr(http, "_manager", manager)
    return manager


class _Handler(BaseHTTPRequestHandler):
    graph: FakeGraph

    def _serve(self) -> None:
        size = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(size) if size else None
        try:
            status, headers, payload = self.graph.handle(
                self.command, self.path, dict(self.headers), body
            )
        except Cut:
            self.connection.close()
            return
        except Timeout:
            threading.Event().wait(5)
            return
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = do_POST = _serve  # noqa: N815

    def log_message(self, *args: Any) -> None:  # sin ruido en los tests
        pass


@contextmanager
def serve(graph: FakeGraph) -> Iterator[str]:
    """Sirve `graph` en `http://127.0.0.1:<puerto>` mientras dure el bloque."""
    handler = type("Handler", (_Handler,), {"graph": graph})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":  # pragma: no cover — entorno local: `python -m tests.meta_sim.transport`
    with serve(FakeGraph()) as base:
        print(f"Graph API falsa de Meta en {base} (Ctrl+C para salir)")  # noqa: T201
        threading.Event().wait()
