"""El veredicto del antivirus (ClamAV, ADR-008; F7-15): un flujo de bytes a `clamd` por INSTREAM,
en TCP, con tope de bytes y un plazo total. Solo `stream: OK` es limpio y solo `<firma> FOUND` es
infectado; cualquier otra cosa (sin `CLAMD_ADDRESS`, caído, plazo vencido, tope, `… ERROR` o una
respuesta que no se entiende) es `Unavailable`, nunca un veredicto. Nada de esto registra bytes."""

import re
import socket
import struct
import time
from dataclasses import dataclass
from typing import IO

from django.conf import settings

CLEAN, INFECTED = "CLEAN", "INFECTED"
CHUNK = 64 << 10  # bytes por trozo de INSTREAM
REPLY_MAX = 1024  # una respuesta de `clamd` es una línea corta
SIGNATURE = re.compile(r"stream: ([\x20-\x7e]{1,200}) FOUND")  # ASCII imprimible


class Unavailable(Exception):  # noqa: N818 — sin veredicto; el texto es una clase cerrada, sin bytes
    pass


@dataclass(frozen=True, slots=True)
class Verdict:
    status: str  # `CLEAN` o `INFECTED`
    signature: str | None = None  # el nombre de la firma de ClamAV (no es contenido del archivo)


def _address() -> tuple[str, int]:
    host, _, port = str(settings.CLAMD_ADDRESS).rpartition(":")
    if not host or not port.isdigit():
        raise Unavailable("sin CLAMD_ADDRESS")
    return host, int(port)


def _left(sock: socket.socket, deadline: float) -> None:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise Unavailable("plazo")
    sock.settimeout(remaining)


def _reply(sock: socket.socket, deadline: float) -> str:
    reply = b""
    while not reply.endswith(b"\0"):
        _left(sock, deadline)
        chunk = sock.recv(REPLY_MAX)
        if not chunk or len(reply) + len(chunk) > REPLY_MAX:
            raise Unavailable("respuesta")
        reply += chunk
    return reply[:-1].decode("ascii", "replace").strip()


def scan(stream: IO[bytes], *, max_bytes: int, seconds: float) -> Verdict:
    """Envía `stream` entero (como mucho `max_bytes`) en menos de `seconds` y lee el veredicto."""
    host, port = _address()
    deadline, sent = time.monotonic() + seconds, 0
    try:
        with socket.create_connection((host, port), timeout=seconds) as sock:
            _left(sock, deadline)
            sock.sendall(b"zINSTREAM\0")
            while chunk := stream.read(CHUNK):
                sent += len(chunk)
                if sent > max_bytes:  # sin veredicto parcial: el resto no se miraría
                    raise Unavailable("tope")
                _left(sock, deadline)
                sock.sendall(struct.pack(">I", len(chunk)) + chunk)
            _left(sock, deadline)
            sock.sendall(struct.pack(">I", 0))
            reply = _reply(sock, deadline)
    except OSError as error:  # caído, rechazado, plazo del socket o cortado por `clamd`
        raise Unavailable(type(error).__name__) from None
    if reply == "stream: OK":
        return Verdict(CLEAN)
    if found := SIGNATURE.fullmatch(reply):
        return Verdict(INFECTED, found.group(1))
    raise Unavailable("respuesta")
