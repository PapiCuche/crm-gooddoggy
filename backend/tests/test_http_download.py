"""F7-09a1a: la descarga acotada de `core.http` (`http.download`) y la consulta de medios del
simulador de Meta. Sin red: el simulador, un gestor falso o sockets locales sustituyen solo la capa
de conexión de urllib3; la política real (hosts de la descarga aparte de la allowlist, https, 443,
URL leída por dos analizadores, tope, plazo, sin decodificar, sin reintentos) es la real."""

import contextlib
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
    sent = {**AUTH, "ACCEPT-encoding": "gzip"}  # el nombre en cualquier combinación: nunca gzip
    assert http.download(URL, io.BytesIO(), max_bytes=100, hosts=HOSTS, headers=sent) == 200
    (((method, url), kwargs),) = manager.calls
    assert (method, url) == ("GET", URL) and not kwargs["redirect"] | kwargs["preload_content"]
    assert kwargs["retries"].total == 0 and kwargs["timeout"].read_timeout <= 15
    want = {"authorization": AUTH["Authorization"], "accept-encoding": "identity"}
    assert kwargs["headers"] == want
    monkeypatch.undo()  # el `_manager` real (el único) solo conecta con su clase protegida:
    assert http._manager.pool_classes_by_scheme == {"https": http._GuardedPool}
    assert http._GuardedPool.ConnectionCls is http._GuardedHTTPSConnection


def test_the_hosts_can_be_any_iterable_even_a_generator(monkeypatch: pytest.MonkeyPatch) -> None:
    serve(monkeypatch, b"hola")
    hosts = (h for h in HOSTS)  # se leen una vez y se comprueban como tupla
    assert http.download(URL, io.BytesIO(), max_bytes=9, hosts=hosts, headers=AUTH) == 200


def test_a_socket_the_watchdog_cannot_watch_is_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_watch` falla en `_new_conn` (aquí `dup`): el socket del cliente se cierra, no se filtra."""
    client, server = socket.socketpair()
    public = [(socket.AF_INET, 1, 6, "", ("93.184.216.34", 443))]
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: public)
    monkeypatch.setattr("urllib3.connection.connection.create_connection", lambda *a, **k: client)
    manager = urllib3.PoolManager()
    manager.pool_classes_by_scheme = {"https": http._GuardedPool}
    monkeypatch.setattr(http, "_manager", manager)

    def broken(sock: socket.socket) -> socket.socket:
        raise OSError(errno.EMFILE, "sin descriptores")

    try:
        with monkeypatch.context() as patched, pytest.raises(http.DownloadFailed):
            patched.setattr(socket.socket, "dup", broken)
            get(seconds=5)
        assert client.fileno() == -1
    finally:
        client.close()
        server.close()


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


def test_the_hosts_of_a_download_must_be_media_hosts_because_the_token_goes_to_all_of_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = serve(monkeypatch)
    for hosts in ((transport.GRAPH_HOST,), (MEDIA_HOST, "evil.example"), ["api.example.com"]):
        with pytest.raises(ValueError, match="META_MEDIA_HOSTS"):
            http.download(URL, io.BytesIO(), max_bytes=9, hosts=hosts, headers=AUTH)
    assert manager.calls == []


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
    for value in ("Bearer abc\r\nHost: evil", "Bearer abc\x00", "Bearer abc\x7f"):  # y sin eco
        with pytest.raises(http.BlockedDestination) as caught:
            http.download(URL, io.BytesIO(), max_bytes=9, hosts=HOSTS, headers={"X": value})
        assert "abc" not in chain(caught.value)
    assert manager.calls == []


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


# Lo que `download` hace con cada `Content-Length` sobre un cuerpo de 4 bytes (el tope es 100): más
# que el tope se rechaza sin leer; lo que no es un número (`²`, `-1`, 5000 dígitos) se ignora.
LENGTHS = {
    "²": 200, "9" * 5000: 200, "-1": 200, "1e3": 200, "": 200, "٣": http.DownloadFailed,
    " 5": http.DownloadFailed, "+5": http.DownloadFailed, "5 ": http.DownloadFailed,
    "10": http.DownloadFailed, "5000": http.ResponseTooLarge, "4": 200,
}  # fmt: skip


@pytest.mark.parametrize("declared", list(LENGTHS))
def test_a_content_length_that_is_hostile_or_lies_gives_the_exact_outcome_and_never_a_valueerror(
    declared: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    serve(monkeypatch, b"hola", headers={"Content-Length": declared})
    sink, want = io.BytesIO(), LENGTHS[declared]
    if want == 200:
        assert get(sink) == 200 and sink.getvalue() == b"hola"
    else:
        with pytest.raises(want):  # type: ignore[call-overload]
            get(sink)
        assert len(sink.getvalue()) == (0 if want is http.ResponseTooLarge else 4)
    serve(monkeypatch, b"x" * 500, headers={"Content-Length": declared})
    sink = io.BytesIO()  # y con más bytes que el tope, mande lo que mande la cabecera
    with pytest.raises(http.ResponseTooLarge):
        get(sink)
    assert len(sink.getvalue()) <= 100


class Sink(io.BytesIO):
    """Un sink que escribe de a 3 bytes (un `write` parcial), falla con disco lleno o no avanza."""

    def __init__(self, mode: str = "partial") -> None:
        super().__init__()
        self.how = mode

    def write(self, data: Any) -> int:
        if self.how == "full":
            raise OSError(errno.ENOSPC, "disco lleno")
        if self.how in ("none", "zero"):
            return {"none": None, "zero": 0}[self.how]  # type: ignore[return-value]
        return super().write(bytes(data[:3]))


def test_a_partial_write_loses_no_bytes_and_a_sink_that_fails_or_stalls_is_an_oserror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    serve(monkeypatch, b"0123456789")
    sink = Sink()
    assert get(sink) == 200 and sink.getvalue() == b"0123456789"
    for mode in ("full", "none", "zero"):  # sin giro infinito y sin confundirlo con la red
        serve(monkeypatch, b"0123456789")
        with pytest.raises(OSError) as caught:
            get(Sink(mode))
        assert type(caught.value) is OSError
        assert (caught.value.errno == errno.ENOSPC) == (mode == "full")


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
    assert time.monotonic() - started < 2.0 and 0 < len(sink.getvalue()) < 40


class Cut(io.BytesIO):
    """Un cuerpo sin `Content-Length` cortado por el vigilante: la lectura da fin de datos."""

    def read1(self, size: int | None = -1) -> bytes:
        if not self.tell():
            return super().read1(2)
        time.sleep(0.4)  # el plazo (0.2 s) vence aquí, y el socket cortado da `b""`
        return b""


def test_a_body_cut_short_by_the_watchdog_is_not_a_complete_200(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    serve(monkeypatch, Cut(b"abcdef"))
    sink = io.BytesIO()
    with pytest.raises(http.DownloadTooSlow):
        get(sink, seconds=0.2)
    assert sink.getvalue() == b"ab"  # (y el llamador lo descarta)


def test_the_watchdog_cuts_a_stalled_read_at_the_deadline_and_a_late_socket_at_once() -> None:
    mine, theirs = socket.socketpair()
    late, other = socket.socketpair()
    for sock in (mine, theirs, late, other):
        sock.settimeout(5)  # un fallo no cuelga la suite
    dog = http._Watchdog(0.1)
    dog.watch(mine)
    dog.start()
    started = time.monotonic()
    assert mine.recv(1) == b"" and dog.fired  # bloqueada hasta que el vigilante la cortó
    assert time.monotonic() - started < 2
    dog.watch(late)  # un socket que llega tras el plazo se corta en cuanto se registra
    assert late.recv(1) == b""
    dog.stop()
    for sock in (mine, theirs, late, other):
        sock.close()


def test_after_stop_the_timer_is_inert_and_the_duplicates_are_closed() -> None:
    """Una conexión ya soltada (su descriptor puede ser de otro hilo) no se corta."""
    mine, theirs = socket.socketpair()
    mine.settimeout(5)
    dog = http._Watchdog(60)
    dog.watch(mine)
    (copy,) = dog._sockets
    assert copy is not mine and copy.fileno() != mine.fileno()  # un duplicado, no el original
    dog.stop()
    assert copy.fileno() == -1  # cerrado bajo el cerrojo
    dog._fire()  # el temporizador, justo en ese instante
    theirs.sendall(b"x")
    assert mine.recv(1) == b"x" and not dog.fired  # la conexión sigue viva
    dog.watch(mine)  # y no registra nada nuevo
    assert dog._sockets == []
    mine.close()
    theirs.close()


def test_a_tls_handshake_that_drips_is_cut_by_the_watchdog_with_the_real_ssl_wrap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Un registro TLS de 16384 bytes que gotea un byte cada 50 ms: la lectura no vence."""
    client, server = socket.socketpair()
    client.settimeout(3)
    server.settimeout(5)
    done, until = threading.Event(), time.monotonic() + 3  # sin vigilante, falla en 3 s: no cuelga

    def drip() -> None:
        server.sendall(b"\x16\x03\x03\x40\x00")
        while not done.is_set() and time.monotonic() < until:
            try:
                server.sendall(b"x")
            except OSError:
                return
            time.sleep(0.05)
        with contextlib.suppress(OSError):  # fin del goteo: el apretón termina con un error
            server.shutdown(socket.SHUT_RDWR)

    answer = [(socket.AF_INET, 1, 6, "", ("93.184.216.34", 443))]
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: answer)
    monkeypatch.setattr("urllib3.connection.connection.create_connection", lambda *a, **k: client)
    threading.Thread(target=drip, daemon=True).start()
    dog = http._Watchdog(0.3)
    token = http._watchdog.set(dog)
    dog.start()
    started = time.monotonic()
    try:
        with pytest.raises((OSError, urllib3.exceptions.HTTPError)):
            http._GuardedHTTPSConnection(MEDIA_HOST, 443).connect()
        assert dog.fired and time.monotonic() - started < 2.5
    finally:
        done.set()
        dog.stop()
        http._watchdog.reset(token)
        client.close()
        server.close()


class Reset(io.BytesIO):
    def read1(self, size: int | None = -1) -> bytes:
        if self.tell():
            raise ProtocolError(f"Connection reset by peer {URL}?{SECRET}")
        return super().read1(2)


def chain(error: BaseException | None) -> str:
    seen = ""
    while error is not None:
        seen += f"{error!r} {error}"
        error = error.__cause__ or error.__context__
    return seen


def test_a_failure_carries_no_url_as_cause_or_context_and_the_download_is_one_request(
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
    # una URL que urlsplit acepta y urllib3 rechaza (`LocationParseError` lleva la URL entera)
    with pytest.raises(http.BlockedDestination) as refused:
        http.download(f"https://{MEDIA_HOST}%/x?{SECRET}", io.BytesIO(), max_bytes=9, hosts=HOSTS)
    raised.append(refused.value)
    for error in raised:  # ni como causa ni como contexto: lo que lee un rastreador de errores
        assert error.__cause__ is None and error.__context__ is None
        assert SECRET not in chain(error) and MEDIA_HOST not in chain(error)
    assert SECRET not in caplog.text and MEDIA_HOST not in caplog.text


def test_with_the_real_manager_a_media_connection_is_never_pooled_nor_logs_the_signed_query(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, settings: Any
) -> None:
    """Gestor, reserva y `_new_conn` reales (sin TLS). urllib3 devuelve la conexión a la reserva al
    leer el último byte: la de una descarga se cierra y la siguiente abre y vigila otra."""
    pairs = [socket.socketpair() for _ in range(2)]
    clients, watched, watch = iter(pairs), [], http._Watchdog.watch
    for sock in (sock for pair in pairs for sock in pair):
        sock.settimeout(5)

    def answer(server: socket.socket) -> None:
        server.recv(65536)
        server.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 4\r\n\r\nhola")

    def spy(dog: http._Watchdog, sock: socket.socket) -> None:
        watched.append(sock)
        watch(dog, sock)

    class Plain(http._GuardedHTTPSConnection):
        is_verified = True

        def connect(self) -> None:
            self.sock = self._new_conn()  # IP validada y vigilante reales; sin el apretón TLS

    public = [(socket.AF_INET, 1, 6, "", ("93.184.216.34", 443))]
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: public)
    monkeypatch.setattr(
        "urllib3.connection.connection.create_connection", lambda *a, **k: next(clients)[0]
    )
    monkeypatch.setattr(http._Watchdog, "watch", spy)
    monkeypatch.setattr(http._GuardedPool, "ConnectionCls", Plain)
    manager = urllib3.PoolManager()
    manager.pool_classes_by_scheme = {"https": http._GuardedPool}
    monkeypatch.setattr(http, "_manager", manager)
    handlers = logging.getLogger("urllib3").handlers[:]  # `dictConfig` los quita; el nivel, caplog
    caplog.set_level(logging.DEBUG, logger="urllib3")  # sin la entrada de `LOGGING`, se vería
    only = {"urllib3": settings.LOGGING["loggers"]["urllib3"]}  # solo su entrada: no toca la raíz
    logging.config.dictConfig({"version": 1, "disable_existing_loggers": False, "loggers": only})
    try:
        for (client, server), seconds in zip(pairs, (5, 60), strict=True):
            threading.Thread(target=answer, args=(server,), daemon=True).start()
            sink, pool = io.BytesIO(), manager.connection_from_url(URL)
            got = http.download(f"{URL}?{SECRET}", sink, max_bytes=9, hosts=HOSTS, seconds=seconds)
            wait = client.gettimeout() or 0  # lo que queda, y nunca más de 15 s por `recv`
            assert got == 200 and sink.getvalue() == b"hola"
            assert 0 < wait < 5 if seconds == 5 else 14 < wait <= 15
            assert client.fileno() == -1 and not any(pool.pool.queue)  # type: ignore[union-attr]
    finally:
        for sock in (sock for pair in pairs for sock in pair):
            sock.close()
        logging.getLogger("urllib3").handlers[:] = handlers  # el resto de la suite, como estaba
    assert watched == [client for client, _ in pairs] and "SECRETO" not in caplog.text
