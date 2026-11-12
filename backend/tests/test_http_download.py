"""F7-09a1: la descarga acotada de `core.http` (`http.download`) y la consulta de medios del
simulador de Meta. Sin red: el simulador o un gestor falso sustituyen solo la capa de conexión de
urllib3 (`_manager`); la política real (allowlist, https, 443, URL leída por dos analizadores,
tope, plazo, sin decodificar, sin reintentos) es la de `core.http`."""

import io
import logging
import socket
import time
from typing import Any

import pytest
import urllib3
from urllib3.exceptions import MaxRetryError, ProtocolError

from core import http
from tests.meta_sim import GRAPH_VERSION, transport
from tests.meta_sim.graph import TEST_TOKEN, FakeGraph, FakeMedia
from tests.meta_sim.webhooks import MEDIA_HOST, PHONE_NUMBER_ID

URL = f"https://{MEDIA_HOST}/blob/M1"
AUTH = {"Authorization": f"Bearer {TEST_TOKEN}"}
HOSTS = (MEDIA_HOST,)


class Manager:
    """El `_manager` falso: devuelve una respuesta (o lanza) y guarda cómo se le pidió."""

    def __init__(self, reply: Any) -> None:
        self.reply, self.calls = reply, []  # type: ignore[var-annotated]

    def request(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append((args, kwargs))
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def serve(
    monkeypatch: pytest.MonkeyPatch,
    body: Any = b"",
    status: int = 200,
    headers: dict[str, str] | None = None,
) -> Manager:
    stream = io.BytesIO(body) if isinstance(body, bytes) else body
    reply = urllib3.HTTPResponse(body=stream, status=status, headers=headers, preload_content=False)
    manager = Manager(reply)
    monkeypatch.setattr(http, "_manager", manager)
    return manager


def get(sink: io.BytesIO | None = None, *, cap: int = 100, **kw: Any) -> int:
    return http.download(URL, sink or io.BytesIO(), max_bytes=cap, hosts=HOSTS, headers=AUTH, **kw)


@pytest.fixture(autouse=True)
def allowlist(settings: Any) -> None:
    settings.HTTP_ALLOWED_HOSTS = [transport.GRAPH_HOST, MEDIA_HOST]
    settings.META_GRAPH_VERSION = GRAPH_VERSION


@pytest.fixture
def graph(monkeypatch: pytest.MonkeyPatch) -> FakeGraph:
    fake = FakeGraph()
    fake.media["M1"] = FakeMedia(b"hola")
    transport.install(monkeypatch, fake)
    return fake


# ---- La consulta y la descarga contra el simulador ----


def test_the_simulator_serves_the_lookup_and_the_blob_and_both_need_the_token(
    graph: FakeGraph,
) -> None:
    lookup = f"https://{transport.GRAPH_HOST}/{GRAPH_VERSION}/M1?phone_number_id={PHONE_NUMBER_ID}"
    found = http.request("GET", lookup, headers=AUTH)
    assert found.status == 200 and found.json()["url"] == URL
    sink = io.BytesIO()
    assert get(sink) == 200 and sink.getvalue() == b"hola"
    assert http.request("GET", lookup).status == 401  # sin token
    assert http.request("GET", lookup.replace(PHONE_NUMBER_ID, "999"), headers=AUTH).status == 400
    graph.media["M1"].lookup = 404
    assert http.request("GET", lookup, headers=AUTH).status == 404
    assert graph.calls[1]["headers"]["Accept-Encoding"] == "identity"  # y se pidió sin codificar


def test_a_download_goes_through_the_shared_manager_without_retries_or_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = serve(monkeypatch, b"hola")
    assert get() == 200
    (((method, url), kwargs),) = manager.calls
    assert (method, url, kwargs["redirect"], kwargs["preload_content"]) == (
        "GET",
        URL,
        False,
        False,
    )
    assert kwargs["retries"].total == 0 and kwargs["headers"]["Accept-Encoding"] == "identity"
    # el `_manager` de `core.http` (el real) es el único, y solo conecta con su clase protegida:
    monkeypatch.undo()
    assert http._manager.pool_classes_by_scheme == {"https": http._GuardedPool}
    assert http._GuardedPool.ConnectionCls is http._GuardedHTTPSConnection


@pytest.mark.parametrize("ip", ["127.0.0.1", "::ffff:127.0.0.1", "169.254.169.254", "10.1.2.3"])
def test_a_media_host_that_resolves_to_a_private_address_fails_before_any_socket(
    ip: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    monkeypatch.setattr(socket, "getaddrinfo", lambda h, p, *a, **k: [(family, 1, 6, "", (ip, p))])
    monkeypatch.setattr("urllib3.connection.connection.create_connection", pytest.fail)
    with pytest.raises(http.BlockedDestination):
        get()


# ---- La URL ----


@pytest.mark.parametrize(
    "url",
    [
        f"http://{MEDIA_HOST}/x", f"https://{MEDIA_HOST}:8443/x", f"https://{MEDIA_HOST}:0/x",
        f"https://{MEDIA_HOST}:65536/x", f"https://{MEDIA_HOST}:99999/x",
        f"https://{MEDIA_HOST}:abc/x", f"https://{MEDIA_HOST}: 443/x",
        f"https://user:pw@{MEDIA_HOST}/x", f"https://evil.example\\@{MEDIA_HOST}/x",
        f"https://{MEDIA_HOST}\\.evil.example/x", f"https://{MEDIA_HOST}./x",
        f"https://{MEDIA_HOST}\t/x", f"https://{MEDIA_HOST}\r\nHost: evil/x",
        f"https://{MEDIA_HOST}%2eevil.example/x", "https://lookaside%2efbsbx.com/x",
        f"https://{MEDIA_HOST}.evil.example/x", f"https://evil.example/{MEDIA_HOST}",
        "https://[::1]/x", "https://[::ffff:7f00:1]/x", "https://2130706433/x",
        "https://0x7f000001/x", "https://0177.0.0.1/x", "https://127.1/x", "file:///etc/passwd",
        f"https://{transport.GRAPH_HOST}/x",  # en la allowlist, pero no un host de media
        f"https://{MEDIA_HOST}/é", "", "https:///x",
    ],
)  # fmt: skip
def test_a_url_outside_the_media_policy_never_reaches_the_connection_manager(
    url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = serve(monkeypatch)
    with pytest.raises(http.BlockedDestination):
        http.download(url, io.BytesIO(), max_bytes=9, hosts=HOSTS, headers=AUTH)
    assert manager.calls == []


def test_the_host_header_cannot_be_replaced_and_the_two_parsers_must_agree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = serve(monkeypatch)
    with pytest.raises(http.BlockedDestination):
        http.download(URL, io.BytesIO(), max_bytes=9, hosts=HOSTS, headers={"hOsT": "evil"})
    monkeypatch.setattr(urllib3.util, "parse_url", lambda u: urllib3.util.Url("https", host="x"))
    with pytest.raises(http.BlockedDestination):  # lo que lee urllib3 no es lo que se validó
        get()
    assert manager.calls == []


# ---- La respuesta: redirección, estado, codificación ----


def test_a_redirect_or_an_error_status_is_returned_and_nothing_is_written_or_followed(
    monkeypatch: pytest.MonkeyPatch, graph: FakeGraph
) -> None:
    for status in (302, 403, 404, 410, 500):
        graph.media["M1"] = FakeMedia(b"x", blob=status, headers={"Location": "http://10.0.0.1/"})
        sink = io.BytesIO()
        assert get(sink) == status and sink.getvalue() == b""
    assert len(graph.calls) == 5  # una petición por intento: el `Location` nunca se pidió


class Unread(io.BytesIO):
    reads = 0

    def read(self, size: int | None = -1) -> bytes:
        self.reads += 1
        return super().read(size)


def test_a_content_encoding_is_refused_before_a_single_byte_is_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = Unread(b"\x1f\x8b" + b"\x00" * 64)  # una bomba gzip cabe en pocos bytes
    serve(monkeypatch, body, headers={"Content-Encoding": "gzip"})
    sink = io.BytesIO()
    with pytest.raises(http.UnsupportedEncoding):
        get(sink)
    assert sink.getvalue() == b"" and not body.reads
    serve(monkeypatch, b"hola", headers={"Content-Encoding": "identity"})
    assert get() == 200  # `identity` sí


# ---- El tope y el plazo ----


@pytest.mark.parametrize("size", [100, 101, 5000])
def test_the_cap_is_exact_and_the_sink_never_gets_more_than_it(
    size: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    serve(monkeypatch, b"x" * size)  # sin `Content-Length`: lo único que vale es lo recibido
    sink = io.BytesIO()
    if size <= 100:
        assert get(sink) == 200 and len(sink.getvalue()) == size
    else:
        with pytest.raises(http.ResponseTooLarge):
            get(sink)
        assert sink.tell() <= 100


def test_a_content_length_that_lies_either_way_changes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    serve(
        monkeypatch, b"x" * 10, headers={"Content-Length": "5000"}
    )  # dice más: se rechaza sin leer
    with pytest.raises(http.ResponseTooLarge):
        get()
    serve(
        monkeypatch, b"x" * 5000, headers={"Content-Length": "10"}
    )  # dice menos: manda lo recibido
    sink = io.BytesIO()
    try:
        get(sink)
    except http.ResponseTooLarge, http.DownloadFailed:
        pass
    assert len(sink.getvalue()) <= 100


class Drip(io.RawIOBase):
    """Un servidor que gotea: un byte por lectura, cada una dentro del tiempo de lectura."""

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        time.sleep(0.02)
        buffer[:1] = b"x"
        return 1


def test_a_server_that_drips_hits_the_total_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    serve(monkeypatch, Drip())
    sink = io.BytesIO()
    started = time.monotonic()
    with pytest.raises(http.DownloadTooSlow):
        get(sink, cap=10_000, seconds=0.1)
    assert time.monotonic() - started < 5 and 0 < len(sink.getvalue()) < 10_000


# ---- Fallos de red: sin reintento y sin la URL ----


class Reset(io.BytesIO):
    def read(self, size: int | None = -1) -> bytes:
        if self.tell():
            raise ProtocolError("Connection reset by peer")
        return super().read(2)


def test_a_reset_in_the_middle_is_one_request_and_the_error_carries_no_url(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    manager = serve(monkeypatch, Reset(b"abcdefgh"))
    with pytest.raises(http.DownloadFailed) as caught:
        get()
    assert len(manager.calls) == 1  # el reintento volvería a bajar el cuerpo entero
    secret = "token=SECRETO"  # noqa: S105 — dato de prueba
    failing = MaxRetryError(None, f"https://{MEDIA_HOST}/x?{secret}", ConnectionError(secret))  # type: ignore[arg-type]
    for error in (failing, OSError(secret), ProtocolError(secret)):
        monkeypatch.setattr(http, "_manager", Manager(error))
        with pytest.raises(http.DownloadFailed) as hidden:
            get()
        shown = str(hidden.value) + repr(hidden.value.__cause__) + caplog.text
        assert secret not in shown and MEDIA_HOST not in shown and hidden.value.__cause__ is None
    assert caught.value.__suppress_context__
