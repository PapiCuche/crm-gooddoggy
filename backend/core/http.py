"""HTTP saliente anti-SSRF (B9): allowlist https, IP pública validada al conectar y fijada, sin
redirects ni proxies. Único módulo con urllib3. Diseño: docs/architecture/storage-http.md."""

import ipaddress
import socket
from typing import Any
from urllib.parse import urlsplit

import urllib3
from django.conf import settings
from urllib3.connection import HTTPSConnection
from urllib3.connectionpool import HTTPSConnectionPool


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
    retries=urllib3.Retry(total=2, redirect=False, raise_on_redirect=False),
)
_manager.pool_classes_by_scheme = {"https": _GuardedPool}


def check_url(url: str) -> None:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or parts.username or parts.password:
        raise BlockedDestination("Solo https y sin credenciales en la URL")
    if host not in {h.lower() for h in settings.HTTP_ALLOWED_HOSTS}:
        raise BlockedDestination(f"Host fuera de la allowlist: {host or '(vacío)'}")
    if parts.port not in (None, 443):
        raise BlockedDestination("Solo el puerto 443")


def request(
    method: str, url: str, *, headers: dict[str, str] | None = None, body: bytes | None = None,
    json: Any = None,
) -> urllib3.BaseHTTPResponse:  # fmt: skip
    """Sin `**kwargs`: no se pueden quitar timeouts, reintentos ni el bloqueo de redirects."""
    check_url(url)
    if any(name.lower() == "host" for name in headers or {}):
        raise BlockedDestination("La cabecera Host no se puede sustituir")  # TLS/Host = URL
    return _manager.request(method, url, headers=headers, body=body, json=json, redirect=False)
