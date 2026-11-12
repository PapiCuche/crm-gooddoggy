"""F7-09a1a: la descarga acotada de `core.http` (`http.download`) y la consulta de medios del
simulador de Meta. Sin red: el simulador, un gestor falso o sockets locales sustituyen solo la capa
de conexión de urllib3; la política real (hosts de la descarga aparte de la allowlist, https, 443,
URL leída por dos analizadores, tope, plazo, sin decodificar, sin reintentos) es la real."""

import errno
import io
import logging
import logging.config
import socket
import threading
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
SECRET = "oh=SECRETO&oe=123"  # noqa: S105 — dato de prueba: la query de una URL firmada


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


def get(sink: Any = None, *, cap: int = 100, **kw: Any) -> int:
    return http.download(URL, sink or io.BytesIO(), max_bytes=cap, hosts=HOSTS, headers=AUTH, **kw)


@pytest.fixture(autouse=True)
def allowlist(settings: Any) -> None:
    settings.HTTP_ALLOWED_HOSTS = [transport.GRAPH_HOST]  # el host de media NO está en la allowlist
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
    assert graph.calls[1]["headers"]["accept-encoding"] == "identity"  # se pidió sin codificar


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
    assert kwargs["retries"].total == 0 and kwargs["timeout"].read_timeout <= 15
    want = {"authorization": AUTH["Authorization"], "accept-encoding": "identity"}
    assert kwargs["headers"] == want
    monkeypatch.undo()  # el `_manager` real (el único) solo conecta con su clase protegida:
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


def test_a_resolved_address_with_a_zone_id_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    answer = [(socket.AF_INET6, 1, 6, "", ("2606:4700::1111%lo0", 443, 0, 0))]
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: answer)
    with pytest.raises(http.BlockedDestination):
        http.resolve_public(MEDIA_HOST, 443)


# ---- La URL y las cabeceras ----


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
        "https://evil.example/blob/M1", "https://evil.example:8443/blob/M1",
        "https://[::1]/x", "https://[::ffff:7f00:1]/x", "https://2130706433/x",
        "https://0x7f000001/x", "https://0177.0.0.1/x", "https://127.1/x", "file:///etc/passwd",
        f"https://{transport.GRAPH_HOST}/x",  # en la allowlist, pero no un host de media
        f"https://{MEDIA_HOST}/é", "", "https:///x",
    ],
)  # fmt: skip
def test_a_url_outside_the_media_policy_never_reaches_the_connection_manager(
    url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, sink = serve(monkeypatch), io.BytesIO()
    with pytest.raises(http.BlockedDestination):
        http.download(url, sink, max_bytes=9, hosts=HOSTS, headers=AUTH)
    assert manager.calls == [] and sink.getvalue() == b""


def test_the_media_host_is_not_reachable_by_request_and_the_parsers_must_agree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = serve(monkeypatch)
    with pytest.raises(http.BlockedDestination):  # `request` lleva el token: solo la allowlist
        http.request("GET", URL, headers=AUTH)
    monkeypatch.setattr(urllib3.util, "parse_url", lambda u: urllib3.util.Url("https", host="x"))
    with pytest.raises(http.BlockedDestination):  # lo que lee urllib3 no es lo que se validó
        get()
    assert manager.calls == []


@pytest.mark.parametrize(
    "name", ["Host", "hOsT", "Transfer-Encoding", "CONTENT-LENGTH", "Connection"]
)
def test_the_caller_cannot_set_the_headers_that_frame_the_request(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = serve(monkeypatch)
    graph_url = f"https://{transport.GRAPH_HOST}/x"
    with pytest.raises(http.BlockedDestination):
        http.download(URL, io.BytesIO(), max_bytes=9, hosts=HOSTS, headers={name: "x"})
    with pytest.raises(http.BlockedDestination):
        http.request("GET", graph_url, headers={name: "x"})
    assert manager.calls == []


def test_the_encoding_header_is_normalized_so_the_caller_cannot_ask_for_gzip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = serve(monkeypatch, b"x")
    sent = {"ACCEPT-encoding": "gzip"}  # el nombre, en cualquier combinación
    http.download(URL, io.BytesIO(), max_bytes=9, hosts=HOSTS, headers=sent)
    assert manager.calls[0][1]["headers"] == {"accept-encoding": "identity"}


# ---- La respuesta: redirección, estado, codificación ----


def test_a_redirect_or_an_error_status_is_returned_and_nothing_is_written_or_followed(
    graph: FakeGraph,
) -> None:
    for status in (302, 403, 404, 410, 500):
        graph.media["M1"] = FakeMedia(b"x", blob=status, headers={"Location": "http://10.0.0.1/"})
        sink = io.BytesIO()
        assert get(sink) == status and sink.getvalue() == b""
    assert len(graph.calls) == 5  # una petición por intento: el `Location` nunca se pidió


class Unread(io.BytesIO):
    reads = 0

    def read1(self, size: int | None = -1) -> bytes:
        self.reads += 1
        return super().read1(-1 if size is None else size)

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


# ---- El tope ----


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
    serve(monkeypatch, b"x" * 10, headers={"Content-Length": "5000"})  # dice más: sin leer
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


@pytest.mark.parametrize("declared", ["²", "٣", "9" * 5000, "-1", "1e3", " 5", "+5", "", "5 "])
def test_a_hostile_content_length_is_ignored_and_the_byte_cap_still_applies(
    declared: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    serve(monkeypatch, b"hola", headers={"Content-Length": declared})
    sink = io.BytesIO()
    try:  # nunca un `ValueError` que se escape de `download`
        assert get(sink) == 200 and sink.getvalue() == b"hola"
    except http.DownloadFailed:  # urllib3 sí lee ` 5` o `+5` como 5 y la respuesta queda corta
        assert len(sink.getvalue()) <= 100
    serve(monkeypatch, b"x" * 500, headers={"Content-Length": declared})
    with pytest.raises((http.ResponseTooLarge, http.DownloadFailed)):
        get()


class Sink(io.BytesIO):
    """Un sink que escribe de a 3 bytes (un `write` parcial) o falla con disco lleno."""

    def __init__(self, fail: bool = False) -> None:
        super().__init__()
        self.fail = fail

    def write(self, data: Any) -> int:
        if self.fail:
            raise OSError(errno.ENOSPC, "disco lleno")
        return super().write(bytes(data[:3]))


def test_a_partial_write_loses_no_bytes_and_a_sink_error_is_not_a_network_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    serve(monkeypatch, b"0123456789")
    sink = Sink()
    assert get(sink) == 200 and sink.getvalue() == b"0123456789"
    serve(monkeypatch, b"0123456789")
    with pytest.raises(OSError, match="disco lleno") as caught:
        get(Sink(fail=True))
    assert type(caught.value) is OSError and caught.value.errno == errno.ENOSPC


# ---- El plazo total ----


class Drip(io.RawIOBase):
    """Un servidor que gotea: un byte por lectura, cada una dentro del tiempo de lectura."""

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        time.sleep(0.05)
        buffer[:1] = b"x"
        return 1


def test_a_body_that_drips_hits_the_total_deadline_without_waiting_for_a_whole_chunk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # `read(CHUNK)` esperaría los 40 bytes (2 s) antes de poder mirar el reloj; `read1` no.
    monkeypatch.setattr(http, "CHUNK", 40)
    serve(monkeypatch, io.BufferedReader(Drip(), 8))
    sink, started = io.BytesIO(), time.monotonic()
    with pytest.raises(http.DownloadTooSlow):
        get(sink, cap=10_000, seconds=0.2)
    assert time.monotonic() - started < 1.0 and 0 < len(sink.getvalue()) < 40


def test_the_watchdog_cuts_a_stalled_read_at_the_deadline_and_a_late_socket_at_once() -> None:
    mine, theirs = socket.socketpair()
    dog = http._Watchdog(0.1)
    dog.watch(mine)
    dog.start()
    started = time.monotonic()
    assert mine.recv(1) == b"" and dog.fired  # bloqueada hasta que el vigilante la cortó
    assert time.monotonic() - started < 2
    late, other = socket.socketpair()
    dog.watch(late)  # un socket que llega tras el plazo se corta en cuanto se registra
    assert late.recv(1) == b""
    for sock in (mine, theirs, late, other):
        sock.close()


def test_a_server_that_drips_the_headers_is_cut_by_the_watchdog_not_by_the_read_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mine, theirs = socket.socketpair()
    mine.settimeout(3)  # sin vigilante, esto fallaría como un fallo de red, no como plazo

    class Stalled:
        def request(self, *args: Any, **kwargs: Any) -> Any:
            http._watch(mine)  # lo que hace la conexión protegida al abrir su socket
            if not mine.recv(1):
                raise ProtocolError("cortada")
            raise AssertionError("no debía llegar nada")

    monkeypatch.setattr(http, "_manager", Stalled())
    started = time.monotonic()
    with pytest.raises(http.DownloadTooSlow):
        get(seconds=0.2)
    assert time.monotonic() - started < 2
    mine.close()
    theirs.close()


def test_the_protected_connection_registers_its_sockets_with_the_active_watchdog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[Any] = []

    class Dog:
        def watch(self, sock: Any) -> None:
            seen.append(sock)

    answer = [(socket.AF_INET, 1, 6, "", ("93.184.216.34", 443))]
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: answer)
    plain = socket.socket()
    monkeypatch.setattr("urllib3.connection.connection.create_connection", lambda *a, **k: plain)
    monkeypatch.setattr(
        urllib3.connection.HTTPSConnection, "connect", lambda self: setattr(self, "sock", "tls")
    )
    token = http._watchdog.set(Dog())  # type: ignore[arg-type]
    try:
        conn = http._GuardedHTTPSConnection(MEDIA_HOST, 443)
        conn._new_conn()
        conn.connect()
    finally:
        http._watchdog.reset(token)
        plain.close()
    assert seen == [plain, "tls"]  # el socket en claro (conexión) y el TLS (cabeceras y cuerpo)


# ---- Fallos de red: sin reintento y sin la URL, ni como causa ni como contexto ----


class Reset(io.BytesIO):
    def read1(self, size: int | None = -1) -> bytes:
        if self.tell():
            raise ProtocolError(f"Connection reset by peer {URL}?{SECRET}")
        return super().read1(2)


def chain(error: BaseException | None) -> str:
    """El texto de toda la cadena de una excepción (causa y contexto)."""
    seen = ""
    while error is not None:
        seen += f"{error!r} {error}"
        error = error.__cause__ or error.__context__
    return seen


def test_a_network_failure_is_one_request_and_carries_no_url_as_cause_or_context(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    manager = serve(monkeypatch, Reset(b"abcdefgh"))
    with pytest.raises(http.DownloadFailed) as caught:
        get()
    assert len(manager.calls) == 1  # el reintento volvería a bajar el cuerpo entero
    url = f"https://{MEDIA_HOST}/x?{SECRET}"
    raised: list[BaseException] = [caught.value]
    failures: list[Exception] = [MaxRetryError(None, url, ConnectionError(SECRET)), OSError(SECRET)]  # type: ignore[arg-type]
    for failure in failures:
        monkeypatch.setattr(http, "_manager", Manager(failure))
        with pytest.raises(http.DownloadFailed) as hidden:
            get()
        raised.append(hidden.value)
    for error in raised:  # ni como causa ni como contexto: lo que lee un rastreador de errores
        assert error.__cause__ is None and error.__context__ is None
        assert SECRET not in chain(error) and MEDIA_HOST not in chain(error)
    assert SECRET not in caplog.text and MEDIA_HOST not in caplog.text


# ---- El registro real de urllib3 ----


def test_urllib3_does_not_log_the_signed_query_with_the_real_request_path(
    monkeypatch: pytest.MonkeyPatch, settings: Any
) -> None:
    """Con el gestor real, urllib3 registra `GET <ruta>?<query>` en DEBUG: la configuración de
    `LOGGING` lo calla. Sin red: la conexión protegida usa un socket local en claro."""
    client, server = socket.socketpair()

    def answer() -> None:
        server.recv(65536)
        server.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 4\r\n\r\nhola")

    class Local(http._GuardedHTTPSConnection):
        is_verified = True

        def connect(self) -> None:
            self.sock = client

    monkeypatch.setattr(http._GuardedPool, "ConnectionCls", Local)
    manager = urllib3.PoolManager()
    manager.pool_classes_by_scheme = {"https": http._GuardedPool}
    monkeypatch.setattr(http, "_manager", manager)
    seen: list[str] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            seen.append(record.getMessage())

    handler, logger = Capture(), logging.getLogger("urllib3")
    old = logger.level
    logger.setLevel(logging.DEBUG)  # sin la entrada de `LOGGING`, urllib3 registraría la query
    only = {"urllib3": settings.LOGGING["loggers"]["urllib3"]}  # solo su entrada: no toca la raíz
    logging.config.dictConfig({"version": 1, "disable_existing_loggers": False, "loggers": only})
    logger.addHandler(handler)
    threading.Thread(target=answer, daemon=True).start()
    sink = io.BytesIO()
    try:
        assert http.download(f"{URL}?{SECRET}", sink, max_bytes=100, hosts=HOSTS) == 200
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old)
        client.close()
        server.close()
    assert sink.getvalue() == b"hola" and "SECRETO" not in " ".join(seen)
