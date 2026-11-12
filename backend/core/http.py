"""HTTP saliente anti-SSRF (B9): allowlist https, IP pública validada al conectar y fijada, sin
redirects ni proxies. Único módulo con urllib3. Diseño: docs/architecture/storage-http.md."""

import io
import ipaddress
import socket
import ssl
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
DOWNLOAD_SECONDS = 90.0  # plazo total de una descarga, además del tiempo de lectura de cada trozo
CHUNK = 64 * 1024


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
    if not ips or not all(is_public(ip) for ip in ips):
        raise BlockedDestination(f"{host} resuelve a una IP no pública")
    return sorted(ips)[0]


class _GuardedHTTPSConnection(HTTPSConnection):
    def _new_conn(self) -> socket.socket:
        name = self._dns_host  # en urllib3, `host` (SNI, certificado, Host:) deriva de _dns_host
        self._dns_host = resolve_public(name, self.port)
        try:
            return super()._new_conn()  # conecta a la IP validada
        finally:
            self._dns_host = name  # TLS y la cabecera Host siguen usando el nombre


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


def check_url(url: str) -> None:
    """https, puerto 443, sin credenciales y un host de la allowlist. La URL se lee con dos
    analizadores (el de Python y el de urllib3, que es el que conecta): si discrepan en el esquema,
    el host o el puerto, o trae controles, espacios o `\\`, se rechaza."""
    if not url.isascii() or any(c <= " " or c in "\\\x7f" for c in url):
        raise BlockedDestination("URL con caracteres no válidos")
    try:
        parts = urlsplit(url)
        port = parts.port
        seen = urllib3.util.parse_url(url)
    except ValueError:  # un puerto fuera de rango o no numérico, un corchete suelto
        raise BlockedDestination("URL no válida") from None
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or parts.username or parts.password:
        raise BlockedDestination("Solo https y sin credenciales en la URL")
    if host not in {h.lower() for h in settings.HTTP_ALLOWED_HOSTS}:
        raise BlockedDestination(f"Host fuera de la allowlist: {host or '(vacío)'}")
    if port not in (None, 443):
        raise BlockedDestination("Solo el puerto 443")
    if (seen.scheme, (seen.host or "").lower(), seen.port) != ("https", host, port):
        raise BlockedDestination("La URL se lee distinto según el analizador")


def request(
    method: str, url: str, *, headers: dict[str, str] | None = None, body: bytes | None = None,
    json: Any = None,
) -> urllib3.BaseHTTPResponse:  # fmt: skip
    """Sin `**kwargs`: no se pueden quitar timeouts, reintentos ni el bloqueo de redirects."""
    check_url(url)
    if any(name.lower() == "host" for name in headers or {}):
        raise BlockedDestination("La cabecera Host no se puede sustituir")  # TLS/Host = URL
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
    """GET en flujo hacia `sink` (F7-09a1). Pasa por el mismo `_manager` que `request` (allowlist,
    https, 443, IP pública fijada al conectar, sin redirecciones ni `Host`), y además: el host tiene
    que estar en `hosts` (una lista más estrecha que la allowlist); sin reintentos (un reintento
    volvería a bajar el cuerpo entero); tope de bytes (`ResponseTooLarge`, por `Content-Length` y,
    sin fiarse de él, por lo recibido: nunca se escribe más que el tope); plazo total
    (`DownloadTooSlow`, comprobado antes de cada trozo: los tiempos de urllib3 son por lectura); y
    sin decodificar (`UnsupportedEncoding` si trae `Content-Encoding`). Un fallo de red es
    `DownloadFailed`, sin el texto original (lleva la URL). Devuelve el estado; solo con 200 se
    escribe en `sink`."""
    check_url(url)
    if (urlsplit(url).hostname or "").lower() not in {h.lower() for h in hosts}:
        raise BlockedDestination("Host de descarga fuera de lo permitido")
    if any(name.lower() == "host" for name in headers or {}):
        raise BlockedDestination("La cabecera Host no se puede sustituir")
    deadline = time.monotonic() + seconds
    never = urllib3.Retry(total=0, redirect=False, raise_on_redirect=False)
    try:
        reply = _manager.request(
            "GET", url, headers={**(headers or {}), "Accept-Encoding": "identity"},
            redirect=False, preload_content=False, retries=never,
        )  # fmt: skip
    except HTTPError, OSError:
        raise DownloadFailed from None
    try:
        if reply.status != 200:
            return int(reply.status)
        if reply.headers.get("Content-Encoding", "identity").strip().lower() != "identity":
            raise UnsupportedEncoding
        declared = reply.headers.get("Content-Length", "")
        if declared.isdigit() and int(declared) > max_bytes:
            raise ResponseTooLarge
        total = 0
        for chunk in reply.stream(CHUNK, decode_content=False):
            total += len(chunk)
            if total > max_bytes:
                raise ResponseTooLarge
            sink.write(chunk)
            if time.monotonic() > deadline:
                raise DownloadTooSlow
        return 200
    except ResponseTooLarge, DownloadTooSlow, UnsupportedEncoding:
        raise
    except HTTPError, OSError:
        raise DownloadFailed from None
    finally:
        reply.close()  # sin devolver la conexión a la reserva: puede quedar a medias
