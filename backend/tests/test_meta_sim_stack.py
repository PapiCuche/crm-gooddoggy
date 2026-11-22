"""F7b-06c (ADR-041): el simulador local de Meta del stack de desarrollo. `core.http` manda a él
solo las URL de Graph, y solo con `DEBUG` y `META_GRAPH_SIMULATOR_URL`; cualquier otro destino
sigue la política de siempre. El servidor (`tests/meta_sim/server.py`) se levanta de verdad en
loopback; el webhook firmado de `send_webhook` entra por la ruta pública. PostgreSQL real con
`crm_app` en las pruebas que conectan una cuenta o ingieren."""

import hashlib
import hmac
import io
import json
import re
import socket
import threading
import time
import urllib.request
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace
from typing import Any

import pytest
from urllib3.connectionpool import HTTPConnectionPool

from apps.conversations import graph, ingress
from core import http
from tests.meta_sim import GRAPH_VERSION, send_webhook
from tests.meta_sim import server as sim_server
from tests.meta_sim.graph import TEST_TOKEN
from tests.meta_sim.server import Simulator, serve
from tests.meta_sim.social import IG_ACCOUNT_ID, IGSID, PAGE_ID, PSID
from tests.meta_sim.webhooks import (
    PHONE_NUMBER_ID,
    SIGNATURE_HEADER,
    TEST_APP_SECRET,
    WABA_ID,
    deliver,
)
from tests.test_contacts_api import crm, real_stack
from tests.test_conversations_tables import world
from tests.test_http import resolving_to
from tests.test_meta_webhook import channel, post
from tests.test_social_accounts import connect
from tests.test_social_inbound import inbox, social_world

__all__ = ["channel", "crm", "real_stack", "social_world", "world"]  # las fixtures, para pytest
GRAPH = f"https://graph.facebook.com/{GRAPH_VERSION}"
AUTH = {"Authorization": f"Bearer {TEST_TOKEN}"}
OTHER_SECRET = "otrosecretodepruebanoreal0000"  # noqa: S105 — ficticio
LOOKALIKES = (
    "graph.facebook.com.evil.test", "x.graph.facebook.com", "x.facebook.com", "facebook.com",
)  # fmt: skip


@pytest.fixture
def sim() -> Iterator[SimpleNamespace]:
    simulator = Simulator()
    server = serve("127.0.0.1", 0, simulator)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield SimpleNamespace(graph=simulator, origin=f"http://127.0.0.1:{server.server_port}")
    server.shutdown()
    server.server_close()


@pytest.fixture
def local(sim: SimpleNamespace, settings: Any) -> SimpleNamespace:
    """El modo del stack local: `DEBUG` y el origen del simulador, como `config.settings.local`."""
    settings.DEBUG, settings.META_GRAPH_SIMULATOR_URL = True, sim.origin
    settings.META_ENABLED = True
    # Hosts parecidos al de Graph en la allowlist: solo el exacto va al simulador.
    settings.HTTP_ALLOWED_HOSTS = [*LOOKALIKES, "graph.facebook.com", "api.example.com"]
    return sim


def calls(sim: SimpleNamespace) -> int:
    return len(sim.graph.page.calls) + len(sim.graph.whatsapp.calls)


def test_in_local_mode_the_adapters_graph_calls_reach_the_simulator(local: SimpleNamespace) -> None:
    assert graph.page(TEST_TOKEN, PAGE_ID, IG_ACCOUNT_ID)["id"] == PAGE_ID
    graph.subscribe(TEST_TOKEN, PAGE_ID)
    assert local.graph.page.subscribed[PAGE_ID][0] == "messages"
    assert graph.profile(TEST_TOKEN, IGSID)["username"] == "cliente.sim"
    assert graph.phone_number(TEST_TOKEN, WABA_ID, PHONE_NUMBER_ID)["id"] == PHONE_NUMBER_ID
    body = {"recipient": {"id": PSID}, "messaging_type": "RESPONSE", "message": {"text": "Hola"}}
    sent = http.request("POST", f"{GRAPH}/{PAGE_ID}/messages", headers=AUTH, json=body)
    assert json.loads(sent.data)["message_id"] == "m_SIMOUT000001"
    assert local.graph.page.sent[0]["message"] == {"text": "Hola"}
    queried = local.graph.page.calls[0]  # la misma ruta y la misma consulta que la URL de Graph
    assert queried["path"] == f"/{GRAPH_VERSION}/{PAGE_ID}" and "fields" in queried["query"]
    with pytest.raises(graph.TokenRejected):  # el token se sigue comprobando
        graph.page("otro-token-que-no-es-el-del-simulador", PAGE_ID)


def test_the_simulator_scripts_outcomes_and_shows_what_it_received(local: SimpleNamespace) -> None:
    def admin(path: str, payload: Any = None) -> Any:
        data = None if payload is None else json.dumps(payload).encode()
        with urllib.request.urlopen(f"{local.origin}{path}", data, timeout=5) as reply:  # noqa: S310
            return json.loads(reply.read())

    assert admin("/_sim/then", {"page": ["window_closed"]}) == {"page": ["window_closed"],
                                                                "whatsapp": []}  # fmt: skip
    body = {"recipient": {"id": PSID}, "messaging_type": "RESPONSE", "message": {"text": "Hola"}}
    refused = http.request("POST", f"{GRAPH}/{PAGE_ID}/messages", headers=AUTH, json=body)
    assert refused.status == 400 and json.loads(refused.data)["error"]["code"] == 10
    graph.subscribe(TEST_TOKEN, PAGE_ID)
    assert admin("/_sim/state")["subscribed"] == {PAGE_ID: graph.SUBSCRIBED_FIELDS.split(",")}
    with pytest.raises(urllib.request.HTTPError):
        admin("/_sim/then", {"page": ["no-existe"]})


@pytest.mark.parametrize(
    "url",
    [
        "{origin}/v23.0/100000000000101",  # el simulador mismo: http y loopback
        "https://127.0.0.1:{port}/v23.0/x",
        "http://graph.facebook.com/v23.0/x",
        "https://graph.facebook.com:8443/v23.0/x",
        "https://user:pw@graph.facebook.com/v23.0/x",
        "https://lookaside.fbsbx.com/x",
    ],
)
def test_in_local_mode_every_other_destination_keeps_the_policy(
    local: SimpleNamespace, url: str
) -> None:
    port = local.origin.rsplit(":", 1)[1]
    with pytest.raises(http.BlockedDestination):
        http.request("GET", url.format(origin=local.origin, port=port), headers=AUTH)
    assert calls(local) == 0


def test_in_local_mode_an_allowed_host_still_needs_a_public_ip(
    local: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", resolving_to("127.0.0.1"))
    with pytest.raises(http.NonPublicAddress):
        http.request("GET", f"https://api.example.com/{GRAPH_VERSION}/{PAGE_ID}", headers=AUTH)
    assert calls(local) == 0


@pytest.mark.parametrize("host", LOOKALIKES)
def test_in_local_mode_only_the_exact_graph_host_goes_to_the_simulator(
    local: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    """En la allowlist y con un nombre parecido (prefijo o sufijo del de Graph): sale por la
    política normal, así que con DNS a loopback se bloquea, y el simulador no recibe nada."""
    monkeypatch.setattr(socket, "getaddrinfo", resolving_to("127.0.0.1"))
    with pytest.raises(http.NonPublicAddress):
        http.request("GET", f"https://{host}/{GRAPH_VERSION}/{PAGE_ID}", headers=AUTH)
    assert calls(local) == 0


def test_in_local_mode_a_download_keeps_the_policy_even_from_the_graph_host(
    local: SimpleNamespace, settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-041 no toca `download`: ni con el host de Graph entre los de medios va al simulador."""
    settings.META_MEDIA_HOSTS = ("lookaside.fbsbx.com", "graph.facebook.com")
    monkeypatch.setattr(socket, "getaddrinfo", resolving_to("127.0.0.1"))
    sink = io.BytesIO()
    with pytest.raises(http.NonPublicAddress):
        http.download(
            f"{GRAPH}/{PAGE_ID}", sink, max_bytes=100, hosts=["graph.facebook.com"], headers=AUTH
        )
    assert calls(local) == 0 and sink.getvalue() == b""


def test_the_simulator_pool_speaks_only_http_with_the_same_limits() -> None:
    for manager in (http._manager, http._simulator):
        assert manager.connection_pool_kw["timeout"] is http._LIMITS["timeout"]
        assert manager.connection_pool_kw["retries"] is http._LIMITS["retries"]
    assert http._simulator.pool_classes_by_scheme == {"http": http._SimulatorPool}  # F8-13d
    assert http._SimulatorPool.__mro__[1] is HTTPConnectionPool  # solo http, con `before_send`


def test_in_local_mode_graph_still_has_to_be_in_the_allowlist(
    local: SimpleNamespace, settings: Any
) -> None:
    settings.HTTP_ALLOWED_HOSTS = ["api.example.com"]
    with pytest.raises(http.BlockedDestination):
        http.request("GET", f"{GRAPH}/{PAGE_ID}", headers=AUTH)
    assert calls(local) == 0


@pytest.mark.parametrize(("debug", "configured"), [(False, True), (True, False)])
def test_outside_local_mode_graph_goes_to_meta_under_the_policy(
    sim: SimpleNamespace, settings: Any, monkeypatch: pytest.MonkeyPatch, debug: bool,
    configured: bool,
) -> None:  # fmt: skip
    """Sin `DEBUG` (production) o sin el ajuste, Graph sale a su host con IP pública: aquí resuelve
    a loopback, así que se bloquea, y el simulador no recibe nada."""
    settings.DEBUG, settings.HTTP_ALLOWED_HOSTS = debug, ["graph.facebook.com"]
    settings.META_GRAPH_SIMULATOR_URL = sim.origin if configured else ""
    monkeypatch.setattr(socket, "getaddrinfo", resolving_to("127.0.0.1"))
    with pytest.raises(http.NonPublicAddress):
        http.request("GET", f"{GRAPH}/{PAGE_ID}", headers=AUTH)
    assert calls(sim) == 0


@pytest.mark.usefixtures("tenant_db")
@pytest.mark.parametrize("platform", ["MESSENGER", "INSTAGRAM"])
def test_in_local_mode_connecting_an_account_against_the_simulator_works(
    crm: SimpleNamespace, local: SimpleNamespace, platform: str
) -> None:
    account, _ = connect(crm, platform)
    assert (account.status, account.status_detail) == ("PENDING", "WEBHOOK_NOT_SEEN")
    assert account.external_account_id == (IG_ACCOUNT_ID if platform == "INSTAGRAM" else PAGE_ID)
    assert PAGE_ID in local.graph.page.subscribed


class Capture(BaseHTTPRequestHandler):
    seen: list[tuple[dict[str, str], bytes]] = []

    def do_POST(self) -> None:  # noqa: N802
        size = int(self.headers["Content-Length"])
        Capture.seen.append((dict(self.headers.items()), self.rfile.read(size)))
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args: Any) -> None:
        pass


@pytest.mark.parametrize("kind", ["page", "instagram", "whatsapp"])
def test_the_sender_posts_a_body_signed_with_the_given_secret(kind: str) -> None:
    server = HTTPServer(("127.0.0.1", 0), Capture)
    threading.Thread(target=server.handle_request, daemon=True).start()
    Capture.seen.clear()
    url = f"http://127.0.0.1:{server.server_port}/webhooks/meta/"
    assert send_webhook.send(url, kind, "Hola", OTHER_SECRET) == 200
    server.server_close()
    ((headers, body),) = Capture.seen
    mac = hmac.new(OTHER_SECRET.encode(), body, hashlib.sha256).hexdigest()  # el dado
    assert headers[SIGNATURE_HEADER] == f"sha256={mac}"
    expected = "whatsapp_business_account" if kind == "whatsapp" else kind
    assert json.loads(body)["object"] == expected
    sent_at = re.search(rb'"timestamp":"?([0-9]{10})', body)  # la hora actual: abre la ventana
    assert sent_at is not None and abs(int(sent_at.group(1)) - time.time()) < 60
    with pytest.raises(ValueError):
        send_webhook.send("file:///etc/passwd", kind, "Hola", send_webhook.DEV_APP_SECRET)


@pytest.mark.usefixtures("tenant_db")
@pytest.mark.parametrize("kind", ["page", "instagram"])
def test_a_webhook_from_the_sender_is_ingested_into_the_inbox(
    social_world: SimpleNamespace, settings: Any, kind: str
) -> None:
    settings.META_APP_SECRETS = [send_webhook.DEV_APP_SECRET]
    first, second = (send_webhook.delivery(kind, "Hola") for _ in range(2))
    assert first.body != second.body  # un `mid` nuevo cada vez: el segundo no es un repetido
    for found in (first, second):
        assert post(deliver(found.body, secret=send_webhook.DEV_APP_SECRET)).status_code == 200
    ingress.process()
    assert inbox(social_world.a) == [("TEXT", "Hola"), ("TEXT", "Hola")]
    unsigned = send_webhook.delivery(kind, "Hola")  # firmado con otro secreto: 401, nada nuevo
    assert SIGNATURE_HEADER not in unsigned.headers
    assert post(deliver(unsigned.body, secret=TEST_APP_SECRET)).status_code == 401


def test_the_simulator_refuses_a_bad_or_oversized_body_and_keeps_a_bounded_log(
    local: SimpleNamespace,
) -> None:
    def raw(head: str) -> bytes:
        with socket.create_connection(
            ("127.0.0.1", int(local.origin.rsplit(":", 1)[1])), 5
        ) as conn:
            conn.sendall(f"POST /_sim/then HTTP/1.1\r\nHost: x\r\n{head}\r\n\r\n".encode())
            reply = b""  # hasta que el simulador cierre: si no la cierra, vence el plazo
            while chunk := conn.recv(4096):
                reply += chunk
            return reply

    assert raw("Content-Length: nada").startswith(b"HTTP/1.1 400")
    assert raw("Content-Length: -1").startswith(b"HTTP/1.1 400")
    assert raw(f"Content-Length: {sim_server.BODY_MAX + 1}").startswith(b"HTTP/1.1 413")
    chunked = raw("Transfer-Encoding: chunked")  # un cuerpo sin longitud: 411 y se cierra
    assert chunked.startswith(b"HTTP/1.1 411") and b"Connection: close" in chunked
    for _ in range(sim_server.CALLS_MAX + 5):
        local.graph.handle("GET", f"/{GRAPH_VERSION}/{PAGE_ID}", AUTH, None)
        local.graph.handle("GET", f"/{GRAPH_VERSION}/{PHONE_NUMBER_ID}", AUTH, None)
    whatsapp = local.graph.whatsapp
    assert len(local.graph.page.calls) == len(whatsapp.calls) == sim_server.CALLS_MAX
    assert len(whatsapp.requests) == sim_server.CALLS_MAX
