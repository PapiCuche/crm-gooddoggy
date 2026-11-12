"""Cómo se sirve `FakeGraph`: `install(monkeypatch, graph)` sustituye solo la capa de red de
`core.http` (el gestor de conexiones de urllib3), así que su política real (https, puerto 443,
allowlist, `Host`) sigue aplicándose; la prueba fija `HTTP_ALLOWED_HOSTS` con `override_settings`.
Un servidor local para el entorno de desarrollo se añade con el adaptador de salida (F7-05) o con
la activación (F7-12): `core.http` no llega a un servidor local a propósito (solo https e IP
públicas).
"""

import io
from typing import Any
from urllib.parse import urlsplit

import pytest
import urllib3
from urllib3.exceptions import ProtocolError, ReadTimeoutError

from core import http
from tests.meta_sim.graph import Cut, FakeGraph, Timeout
from tests.meta_sim.webhooks import MEDIA_HOST

GRAPH_HOST = "graph.facebook.com"


class _Manager:
    """El `PoolManager` falso: contesta `request` con el estado y el cuerpo de `FakeGraph`."""

    def __init__(self, graph: FakeGraph, hosts: tuple[str, ...]) -> None:
        self.graph, self.hosts = graph, hosts

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
        json: Any = None,
        redirect: bool = True,
        preload_content: bool = True,
        **_: Any,
    ) -> urllib3.BaseHTTPResponse:
        import json as jsonlib

        parts = urlsplit(url)
        if parts.hostname not in self.hosts:  # nada fuera del simulador
            raise AssertionError(f"el simulador no atiende {parts.hostname}")
        sent = jsonlib.dumps(json).encode() if json is not None else body
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
            body=io.BytesIO(payload),
            headers=reply_headers,
            status=status,
            preload_content=preload_content,
        )


def install(monkeypatch: pytest.MonkeyPatch, graph: FakeGraph, *hosts: str) -> _Manager:
    manager = _Manager(graph, hosts or (GRAPH_HOST, MEDIA_HOST))
    monkeypatch.setattr(http, "_manager", manager)
    return manager
