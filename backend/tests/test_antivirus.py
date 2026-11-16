"""F7-15: `core.antivirus` contra un `clamd` falso (un servidor TCP que entiende INSTREAM) y, con
`CLAMD_TEST_ADDRESS`, contra un `clamd` real (el paso de CI `backend checks`). Sin base de datos."""

import io
import os
import socket
import struct
import threading
from collections.abc import Callable, Iterator
from typing import Any

import pytest

from core import antivirus

# El archivo de prueba EICAR, armado en dos trozos: el repositorio no guarda la cadena entera.
EICAR = b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$" + b"EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"
REAL = os.environ.get("CLAMD_TEST_ADDRESS", "")
# Pasa los magic bytes de un PDF (F7-15b) y lleva EICAR en un objeto `stream`, que clamd extrae y
# escanea aparte: así se prueba un envío infectado desde el compositor (local-stack.md §6).
EICAR_PDF = (
    b"%PDF-1.4\n1 0 obj\n<< /Length 68 >>\nstream\n" + EICAR + b"\nendstream\nendobj\n%%EOF\n"
)


def _instream(conn: socket.socket) -> bytes:
    """Lo que manda el cliente: la orden y los trozos hasta el de longitud cero."""
    data = conn.makefile("rb")
    assert data.read(10) == b"zINSTREAM\0"
    body = b""
    while size := struct.unpack(">I", data.read(4))[0]:
        body += data.read(size)
    return body


def found(body: bytes) -> bytes:
    return b"stream: Eicar-Test-Signature FOUND\0" if EICAR in body else b"stream: OK\0"


class FakeClamd:
    """Un `clamd` en 127.0.0.1 que contesta con `answer(cuerpo)`; `None` no contesta nada."""

    def __init__(self, answer: Callable[[bytes], bytes | None] = found) -> None:
        self.answer, self.bodies = answer, list[bytes]()
        self.server = socket.create_server(("127.0.0.1", 0))
        self.address = f"127.0.0.1:{self.server.getsockname()[1]}"
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            with conn:
                try:
                    body = _instream(conn)
                except AssertionError, OSError, struct.error:
                    continue
                self.bodies.append(body)
                reply = self.answer(body)
                if reply is not None:
                    conn.sendall(reply)
                else:
                    conn.recv(1)  # espera a que el cliente se canse

    def close(self) -> None:
        self.server.close()


@pytest.fixture
def clamd(settings: Any) -> Iterator[FakeClamd]:
    fake = FakeClamd()
    settings.CLAMD_ADDRESS = fake.address
    yield fake
    fake.close()


def scan(content: bytes, **over: Any) -> antivirus.Verdict:
    return antivirus.scan(io.BytesIO(content), **({"max_bytes": 1 << 20, "seconds": 5.0} | over))


def test_eicar_is_infected_and_a_clean_file_is_clean_streamed_in_chunks(clamd: FakeClamd) -> None:
    assert scan(EICAR) == antivirus.Verdict("INFECTED", "Eicar-Test-Signature")
    big = bytes(antivirus.CHUNK * 3 + 7)  # varios trozos de INSTREAM
    assert scan(big) == antivirus.Verdict("CLEAN")
    assert clamd.bodies == [EICAR, big]


@pytest.mark.parametrize(
    ("reply", "case"),
    [
        (b"stream: OK", "sin el NUL final"),
        (b"INSTREAM size limit exceeded. ERROR\0", "error de clamd"),
        (b"stream: Something ERROR\0", "error de clamd"),
        (b"stream: OK, maybe\0", "respuesta desconocida"),
        (b"x" * 2000, "respuesta sin fin"),
        (None, "no contesta"),
    ],
)
def test_anything_but_ok_or_found_is_never_a_verdict(
    settings: Any, reply: bytes | None, case: str
) -> None:
    fake = FakeClamd(lambda body: reply)
    settings.CLAMD_ADDRESS = fake.address
    with pytest.raises(antivirus.Unavailable):
        scan(b"hola", seconds=1.0)
    fake.close()


def test_without_clamd_or_its_address_there_is_no_verdict(settings: Any) -> None:
    with socket.create_server(("127.0.0.1", 0)) as free:
        port = free.getsockname()[1]
    for address in ("", "clamd", f"127.0.0.1:{port}"):  # sin dirección, sin puerto, caído
        settings.CLAMD_ADDRESS = address
        with pytest.raises(antivirus.Unavailable):
            scan(b"hola")


def test_the_size_cap_stops_before_sending_more_and_gives_no_verdict(clamd: FakeClamd) -> None:
    with pytest.raises(antivirus.Unavailable, match="tope"):
        scan(bytes(antivirus.CHUNK * 2), max_bytes=antivirus.CHUNK)
    assert clamd.bodies == []  # sin el trozo final, `clamd` no contesta nada


@pytest.mark.skipif(not REAL, reason="sin CLAMD_TEST_ADDRESS (clamd real: backend checks en CI)")
def test_a_real_clamd_finds_eicar_and_passes_a_clean_file(settings: Any) -> None:
    settings.CLAMD_ADDRESS = REAL
    verdict = scan(EICAR, seconds=30.0)
    assert verdict.status == "INFECTED" and verdict.signature and "Eicar" in verdict.signature
    assert scan(b"\xff\xd8\xff\xe0" + bytes(4096), seconds=30.0) == antivirus.Verdict("CLEAN")
    assert scan(EICAR_PDF, seconds=30.0).status == "INFECTED"  # el envío infectado de la guía
