"""HTTP saliente anti-SSRF (B9): allowlist https, IP pública validada al conectar y fijada, sin
redirects ni proxies. Único módulo con urllib3. Diseño: docs/architecture/storage-http.md."""

import contextvars
import io
import ipaddress
import re
import socket
import ssl
import threading
import time
from collections.abc import Iterable
from typing import IO, Any
from urllib.parse import urlsplit

import urllib3
from django.conf import settings
from urllib3.connection import HTTPSConnection
from urllib3.connectionpool import HTTPSConnectionPool

HTTPError = urllib3.exceptions.HTTPError  # lo que lanza una petición que no llegó a respuesta
MAX_RESPONSE = 1 << 20  # una respuesta mayor no es de un servicio de la allowlist: se corta
DOWNLOAD_SECONDS = 90.0  # plazo total de una descarga (ver `download`: qué cubre y qué no)
CHUNK = 64 * 1024
# Cabeceras que el llamador no puede poner: las del destino (sale de la URL) y las de enmarcado.
FORBIDDEN_HEADERS = frozenset({"host", "transfer-encoding", "content-length", "connection"})


class ResponseTooLarge(HTTPError):
    """La respuesta pasó `MAX_RESPONSE`: se deja de leer (la petición ya se envió)."""


class DownloadTooSlow(HTTPError):
    """Una descarga pasó su plazo total (un servidor que gotea para no acabar)."""


class UnsupportedEncoding(HTTPError):
    """La descarga trae un `Content-Encoding`: no se decodifica nunca (una bomba gzip)."""


class DownloadFailed(HTTPError):
    """La red falló durante una descarga. Sin el texto del error original: lleva la URL firmada."""


class BlockedDestination(Exception):  # noqa: N818 — resultado de política, no un fallo de red
    """Destino fuera de la allowlist o que resuelve a una IP no pública."""


# Prefijos que llevan una IPv4 incrustada (NAT64, compatibles, SIIT) o no enrutables en Internet.
_BLOCKED = [ipaddress.ip_network(n) for n in (
    "64:ff9b::/96", "64:ff9b:1::/48", "::/96", "::ffff:0:0:0/96", "fec0::/10", "192.88.99.0/24",
)]  # fmt: skip


def is_public(ip: str) -> bool:
    address = ipaddress.ip_address(ip.split("%", 1)[0])
    if isinstance(address, ipaddress.IPv6Address):
        embedded = address.ipv4_mapped or address.sixtofour or (address.teredo or (None, None))[1]
        if embedded is not None and not is_public(str(embedded)):
            return False
    if any(address in network for network in _BLOCKED if network.version == address.version):
        return False
    return address.is_global and not address.is_multicast


def resolve_public(host: str, port: int) -> str:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise BlockedDestination(f"No se pudo resolver {host}") from exc
    ips = {str(info[4][0]) for info in infos}
    if not ips or any("%" in ip for ip in ips) or not all(is_public(ip) for ip in ips):  # `%`: zona
        raise BlockedDestination(f"{host} resuelve a una IP no pública")
    return sorted(ips)[0]


class _Watchdog:
    """Corta los sockets de una descarga al vencer su plazo total: los tiempos de urllib3 son por
    lectura y un servidor que gotea (apretón TLS, cabeceras o cuerpo) los reiniciaría sin fin.
    `watch` guarda un **duplicado** del socket (`ssl` suelta el descriptor original al empezar el
    apretón; el duplicado sigue siendo el socket del núcleo y su número no se reutiliza). `stop`,
    antes de soltar ningún socket, cierra los duplicados y deja inerte al temporizador bajo el
    mismo cerrojo: nunca corta una conexión ajena. No cubre `getaddrinfo`."""

    def __init__(self, seconds: float) -> None:
        self.fired, self._stopped, self._sockets = False, False, []  # type: ignore[var-annotated]
        self._lock = threading.Lock()
        self._timer = threading.Timer(max(seconds, 0.0), self._fire)
        self._timer.daemon = True

    def start(self) -> None:
        self._timer.start()

    def stop(self) -> None:
        with self._lock:
            self._stopped = True
            self._timer.cancel()
            sockets, self._sockets = self._sockets, []
        for sock in sockets:
            sock.close()

    def watch(self, sock: socket.socket) -> None:
        with self._lock:
            if self._stopped:
                return
            self._sockets.append(socket.socket.dup(sock))
            if self.fired:
                self._shutdown()

    def _fire(self) -> None:
        with self._lock:
            if self._stopped:
                return
            self.fired = True
            self._shutdown()

    def _shutdown(self) -> None:
        for sock in self._sockets:
            try:  # el de la clase base: `SSLSocket.shutdown` cambia estado desde otro hilo
                socket.socket.shutdown(sock, socket.SHUT_RDWR)
            except OSError:
                pass  # ya cerrado


_watchdog: contextvars.ContextVar[_Watchdog | None] = contextvars.ContextVar(
    "http_watchdog", default=None
)


def _watch(sock: socket.socket) -> None:
    if (dog := _watchdog.get()) is not None:
        dog.watch(sock)


class _GuardedHTTPSConnection(HTTPSConnection):
    def _new_conn(self) -> socket.socket:
        name = self._dns_host  # en urllib3, `host` (SNI, certificado, Host:) deriva de _dns_host
        self._dns_host = resolve_public(name, self.port)
        try:
            sock = super()._new_conn()  # conecta a la IP validada
        finally:
            self._dns_host = name  # TLS y la cabecera Host siguen usando el nombre
        _watch(sock)  # antes del apretón TLS: es el socket del núcleo de toda la conexión
        return sock


class _GuardedPool(HTTPSConnectionPool):
    ConnectionCls = _GuardedHTTPSConnection


_manager = urllib3.PoolManager(
    timeout=urllib3.Timeout(connect=5.0, read=15.0),
    # `other=0`: urllib3 repite un POST ante un error TLS «de otro tipo» (`SSLError`) aunque la
    # petición ya se enviara y el fallo llegara al leer la respuesta: un envío duplicado.
    retries=urllib3.Retry(total=2, redirect=False, raise_on_redirect=False, other=0),
)
_manager.pool_classes_by_scheme = {"https": _GuardedPool}


def never_connected(error: BaseException) -> bool:
    """¿El fallo ocurrió antes de enviar la petición (no se pudo conectar, la IP no era pública,
    TLS falló)? Entonces el servidor no recibió nada y repetirla es seguro; cualquier otro fallo
    (tiempo de lectura agotado, conexión cortada) es ambiguo: la petición pudo llegar."""
    if isinstance(error, urllib3.exceptions.MaxRetryError):
        error = error.reason or error
    if isinstance(error, urllib3.exceptions.SSLError):
        # Un error TLS también puede llegar al leer la respuesta, con la petición ya enviada: solo
        # un certificado que no vale (apretón de manos) es seguro; cualquier otro es ambiguo.
        inner = error.args[0] if error.args else None
        return isinstance(inner, ssl.SSLCertVerificationError)
    connect = (
        BlockedDestination, urllib3.exceptions.ConnectTimeoutError,
        urllib3.exceptions.NewConnectionError,
    )  # fmt: skip
    return isinstance(error, connect)


def check_url(url: str, hosts: Iterable[str] | None = None) -> None:
    """https, puerto 443, sin credenciales y un host de `hosts` (por omisión, la allowlist). La URL
    se lee con dos analizadores (el de Python y el de urllib3, que es el que conecta): si discrepan
    en el esquema, el host o el puerto, o trae controles, espacios o `\\`, se rechaza."""
    if not url.isascii() or any(c <= " " or c in "\\\x7f" for c in url):
        raise BlockedDestination("URL con caracteres no válidos")
    try:
        parts = urlsplit(url)
        port = parts.port
        seen = urllib3.util.parse_url(url)
        bad = False
    except ValueError:  # un puerto fuera de rango o no numérico, un corchete suelto
        bad = True  # se lanza fuera del `except`: el error de urllib3 lleva la URL entera
    if bad:
        raise BlockedDestination("URL no válida") from None
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or parts.username or parts.password:
        raise BlockedDestination("Solo https y sin credenciales en la URL")
    if host not in {h.lower() for h in (settings.HTTP_ALLOWED_HOSTS if hosts is None else hosts)}:
        raise BlockedDestination(f"Host fuera de la allowlist: {host or '(vacío)'}")
    if port not in (None, 443):
        raise BlockedDestination("Solo el puerto 443")
    if (seen.scheme, (seen.host or "").lower(), seen.port) != ("https", host, port):
        raise BlockedDestination("La URL se lee distinto según el analizador")


def _check_headers(headers: dict[str, str] | None) -> dict[str, str]:
    """Las cabeceras del llamador con el nombre en minúsculas; sin `FORBIDDEN_HEADERS`."""
    named = {name.lower(): value for name, value in (headers or {}).items()}
    if FORBIDDEN_HEADERS & named.keys():
        raise BlockedDestination("Cabecera que el llamador no puede poner")  # TLS/Host = URL
    if any(c < " " and c != "\t" or c == "\x7f" for value in named.values() for c in value):
        raise BlockedDestination("Valor de cabecera con controles")  # sin el valor: es el token
    return named


def request(
    method: str, url: str, *, headers: dict[str, str] | None = None, body: bytes | None = None,
    json: Any = None,
) -> urllib3.BaseHTTPResponse:  # fmt: skip
    """Sin `**kwargs`: no se pueden quitar timeouts, reintentos ni el bloqueo de redirects."""
    check_url(url)
    _check_headers(headers)  # los nombres originales se envían tal cual: ya no hay prohibidos
    reply = _manager.request(
        method, url, headers=headers, body=body, json=json, redirect=False, preload_content=False
    )
    data = reply.read(MAX_RESPONSE + 1)  # nunca más de `MAX_RESPONSE` + 1 bytes en memoria
    reply.release_conn()
    if len(data) > MAX_RESPONSE:
        raise ResponseTooLarge
    # `data` ya viene decodificado: sin estas cabeceras urllib3 lo decodificaría (o lo mediría) otra
    # vez y una respuesta con `Content-Encoding: gzip` fallaría con `DecodeError`.
    skip = {"content-encoding", "content-length", "transfer-encoding"}
    headers = {k: v for k, v in reply.headers.items() if k.lower() not in skip}
    return urllib3.HTTPResponse(
        body=io.BytesIO(data), headers=headers, status=reply.status, preload_content=True
    )


def download(
    url: str, sink: IO[bytes], *, max_bytes: int, hosts: Iterable[str],
    headers: dict[str, str] | None = None, seconds: float = DOWNLOAD_SECONDS,
) -> int:  # fmt: skip
    """GET en flujo hacia `sink` (F7-09a1a): el mismo `_manager` que `request` (https, 443, IP
    pública fijada, sin redirecciones), con `hosts` en lugar de la allowlist. `hosts` tiene que ser
    un subconjunto de `META_MEDIA_HOSTS` (se envía `headers`, el token, a todos: `ValueError`).
    Sin reintentos ni decodificación (un `Content-Encoding` es `UnsupportedEncoding`). Al `sink`
    nunca llega más de `max_bytes` (`ResponseTooLarge`). Plazo (`DownloadTooSlow`): antes de cada
    lectura (`read1`) y con un vigilante que corta el socket, también en el apretón TLS y las
    cabeceras; no cubre `getaddrinfo`. Un fallo de red es `DownloadFailed`, sin el texto original
    (la URL firmada) ni como causa ni como contexto; un fallo del `sink` es un `OSError` tal cual.
    Solo con 200 se escribe en `sink`; ante **cualquier** excepción, el llamador lo descarta."""
    allowed = {h.lower() for h in settings.META_MEDIA_HOSTS}
    if not {h.lower() for h in hosts} <= allowed:
        raise ValueError("Los hosts de una descarga tienen que ser de META_MEDIA_HOSTS")
    check_url(url, hosts)
    sent = _check_headers(headers) | {"accept-encoding": "identity"}
    dog = _Watchdog(seconds)
    token = _watchdog.set(dog)
    try:
        dog.start()
        return _fetch(url, sink, max_bytes, sent, seconds, dog)
    finally:
        dog.stop()  # idempotente; `_fetch` ya lo llamó antes de soltar el socket
        _watchdog.reset(token)


def _fetch(
    url: str, sink: IO[bytes], max_bytes: int, headers: dict[str, str], seconds: float,
    dog: _Watchdog,
) -> int:  # fmt: skip
    deadline = time.monotonic() + seconds
    lapse = urllib3.Timeout(connect=min(5.0, seconds), read=min(15.0, seconds))
    never = urllib3.Retry(total=0, redirect=False, raise_on_redirect=False)
    reply, failed = None, False
    try:
        reply = _manager.request(
            "GET", url, headers=headers, redirect=False, preload_content=False, retries=never,
            timeout=lapse,
        )  # fmt: skip
    except HTTPError, OSError:
        failed = True
    if reply is None:  # fuera del `except`: sin la excepción de urllib3 (lleva la URL) de contexto
        raise (DownloadTooSlow if dog.fired else DownloadFailed) from None
    try:
        if reply.status != 200:
            return int(reply.status)
        if reply.headers.get("Content-Encoding", "identity").strip().lower() != "identity":
            raise UnsupportedEncoding
        declared = reply.headers.get("Content-Length", "")
        if re.fullmatch(r"[0-9]{1,18}", declared) and int(declared) > max_bytes:
            raise ResponseTooLarge
        total = 0
        while True:
            if time.monotonic() > deadline or dog.fired:
                raise DownloadTooSlow
            try:
                chunk = reply.read1(CHUNK, decode_content=False)
            except HTTPError, OSError:
                failed = True
                break
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise ResponseTooLarge
            view = memoryview(chunk)
            while view:  # un `write` parcial no pierde bytes; un error del sink sale tal cual
                written = sink.write(view)
                if not written:  # `None` o 0: sin avance no hay fin (nunca un giro infinito)
                    raise OSError("el destino no acepta más bytes")
                view = view[written:]
        # un corte del vigilante llega como fin de cuerpo (en uno sin `Content-Length`): no es 200
        if failed or dog.fired:
            raise (DownloadTooSlow if dog.fired else DownloadFailed) from None
        return 200
    finally:
        dog.stop()  # antes de soltar el socket: el temporizador no corta una conexión ajena
        reply.close()  # sin devolver la conexión a la reserva: puede quedar a medias
