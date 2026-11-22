"""F8-13d (#1084): la exclusión se vuelve a comprobar justo antes de que salga el cuerpo, ya
conectado (TCP y TLS) y antes de escribir nada (`core.http`, `before_send`). Un proveedor falso de
verdad: un servidor TLS local (certificado de prueba, de confianza solo aquí) que cuenta los bytes
que recibe tras el apretón; `core.http` real (allowlist, IP fijada, gestor y conexión reales). La
sesión de PostgreSQL del copiloto se mata con `pg_terminate_backend` desde otra conexión al
conectar, en el apretón TLS o en la comprobación, con una exclusión que responde 200: no sale ni un
byte y la reserva se libera a 0. Hilos y conexiones reales; datos ficticios."""

import datetime as dt
import socket
import ssl
import threading
import time
from collections.abc import Callable, Iterator
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
import urllib3.connection
import urllib3.util.connection
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from django.db import DatabaseError, connection
from psycopg.conninfo import make_conninfo

from apps.ai_agents import copilot
from apps.ai_gateway import gateway, orphans
from apps.ai_gateway.llm import Outcome
from apps.conversations import attention
from core import http
from core.tenancy.scope import tenant_scope
from tests import ai_sim
from tests.test_ai_copilot_exclusion import LEDGER, RUNS, USAGE, excluded_at, mark, put
from tests.test_ai_copilot_runtime import PLAIN, desk
from tests.test_ai_copilot_suggest_api import DB
from tests.test_ai_gateway import world
from tests.test_ai_kill_switch import later
from tests.test_memberships import ctx
from tests.test_self_context import real_stack

__all__ = ["desk", "real_stack", "world"]  # los fixtures, para pytest
pytestmark = pytest.mark.usefixtures("tenant_db", "real_stack")
REAL_MANAGER = http._manager  # antes de que `desk` ponga el simulador
HOLDER = ("SELECT pid FROM pg_locks WHERE locktype = 'advisory' AND granted"
          " AND mode = 'ShareLock'")  # fmt: skip
ADVISORY = "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory'"


def _name(text: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, text)])


def _certificates(folder: Any) -> tuple[str, ssl.SSLContext]:
    """Una CA de prueba y el certificado de `api.anthropic.com` firmado por ella."""
    now = dt.datetime.now(dt.UTC)
    ca_key, key = ec.generate_private_key(ec.SECP256R1()), ec.generate_private_key(ec.SECP256R1())
    usage = x509.KeyUsage(True, False, False, False, False, True, True, False, False)
    ca = (x509.CertificateBuilder().subject_name(_name("test-ca")).issuer_name(_name("test-ca"))
          .public_key(ca_key.public_key()).serial_number(1).not_valid_before(now - dt.timedelta(1))
          .not_valid_after(now + dt.timedelta(1))
          .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
          .add_extension(usage, critical=True)
          .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), False)
          .sign(ca_key, hashes.SHA256()))  # fmt: skip
    leaf_usage = x509.KeyUsage(True, False, False, False, False, False, False, False, False)
    leaf = (x509.CertificateBuilder().subject_name(_name(ai_sim.HOST)).issuer_name(ca.subject)
            .public_key(key.public_key()).serial_number(2).not_valid_before(now - dt.timedelta(1))
            .not_valid_after(now + dt.timedelta(1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(ai_sim.HOST)]), False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(leaf_usage, critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
                           False)
            .sign(ca_key, hashes.SHA256()))  # fmt: skip
    pem = serialization.Encoding.PEM
    (folder / "ca.pem").write_bytes(ca.public_bytes(pem))
    (folder / "leaf.pem").write_bytes(leaf.public_bytes(pem))
    (folder / "key.pem").write_bytes(key.private_bytes(
        pem, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))  # fmt: skip
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.load_cert_chain(folder / "leaf.pem", folder / "key.pem")
    return str(folder / "ca.pem"), server


class Provider:
    """El servidor TLS: por conexión, los bytes recibidos tras el apretón (`received`); contesta
    con lo que diga el `FakeAnthropic` de `desk` y cierra la conexión."""

    def __init__(self, tls: ssl.SSLContext, fake: Any) -> None:
        self.tls, self.fake, self.received = tls, fake, list[bytes]()
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                raw, _ = self.listener.accept()
            except OSError:
                return
            raw.settimeout(10)
            try:
                sock = self.tls.wrap_socket(raw, server_side=True)
            except OSError:
                self.received.append(b"<sin apreton>")
                raw.close()
                continue
            with sock:
                self._answer(sock)

    def settled(self, count: int) -> list[bytes]:
        """Lo recibido, cuando el servidor ya registró `count` conexiones (o a los 10 s)."""
        until = time.monotonic() + 10
        while len(self.received) < count and time.monotonic() < until:
            threading.Event().wait(0.01)  # `desk` sustituye `time.sleep`
        return self.received

    @staticmethod
    def _recv(sock: ssl.SSLSocket) -> bytes:
        try:
            return sock.recv(65536)
        except OSError:  # cerrada sin `close_notify`: lo mismo que el fin
            return b""

    def _answer(self, sock: ssl.SSLSocket) -> None:
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = self._recv(sock)
            if not chunk:
                self.received.append(data)  # cerrada sin una petición completa
                return
            data += chunk
        head, _, body = data.partition(b"\r\n\r\n")
        lines = head.decode().split("\r\n")
        headers = {
            k.strip().lower(): v.strip() for k, _, v in (h.partition(":") for h in lines[1:])
        }
        while len(body) < int(headers.get("content-length", "0")):
            body += self._recv(sock)
        self.received.append(head + b"\r\n\r\n" + body)
        self.fake.body = body
        status, reply = self.fake.handle("POST", f"https://{ai_sim.HOST}/v1/messages", headers)
        extra = f"retry-after: {self.fake.retry_after}\r\n" if status == 429 else ""
        sock.sendall(f"HTTP/1.1 {status} X\r\ncontent-type: application/json\r\nrequest-id: r1\r\n"
                     f"{extra}content-length: {len(reply)}\r\nconnection: close\r\n\r\n".encode()
                     + reply)  # fmt: skip


@pytest.fixture(scope="module")
def certificates(tmp_path_factory: pytest.TempPathFactory) -> tuple[str, ssl.SSLContext]:
    return _certificates(tmp_path_factory.mktemp("tls"))


@pytest.fixture
def provider(
    desk: SimpleNamespace, certificates: tuple[str, ssl.SSLContext],
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Provider]:  # fmt: skip
    """`core.http` real: la IP pública fijada se sustituye por el servidor local al conectar y su
    CA es de confianza solo en el gestor de esta prueba."""
    ca, tls = certificates
    server = Provider(tls, desk.fake)
    public = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
    resolve = socket.getaddrinfo  # la base (psycopg resuelve en Python) sigue con el real

    def resolved(host: Any, *args: Any, **kwargs: Any) -> Any:
        return public if host == ai_sim.HOST else resolve(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", resolved)

    def local(address: Any, *args: Any, **kwargs: Any) -> socket.socket:
        assert address == ("93.184.216.34", 443)  # la IP validada y fijada
        sock = socket.socket()  # sin `getaddrinfo`: está sustituido
        sock.settimeout(5)
        sock.connect(("127.0.0.1", server.port))
        return sock

    monkeypatch.setattr("urllib3.connection.connection.create_connection", local)
    monkeypatch.setattr(http, "_manager", REAL_MANAGER)
    monkeypatch.setitem(REAL_MANAGER.connection_pool_kw, "ca_certs", ca)
    REAL_MANAGER.clear()
    yield server
    REAL_MANAGER.clear()
    server.listener.close()


def ahead_of(owner: Any, name: str, before: Callable[[], None]) -> None:
    """`before()` y después el original (sin `monkeypatch`: se deshace en la prueba)."""
    real = getattr(owner, name)

    def wrapped(*args: Any, **kwargs: Any) -> Any:
        before()
        return real(*args, **kwargs)

    setattr(owner, name, wrapped)


def suggest(desk: SimpleNamespace) -> str:
    try:
        copilot.suggest(ctx(desk.a, desk.ana), desk.convo)
        return "OK"
    except copilot.CopilotError as stop:
        return stop.code


def test_an_included_call_goes_through_tls_and_its_body_leaves_once(
    desk: SimpleNamespace, provider: Provider, migrator: DB
) -> None:
    desk.fake.replies = [PLAIN]
    assert suggest(desk) == "OK"
    [sent] = provider.settled(1)
    assert sent.startswith(b"POST /v1/messages HTTP/1.1") and b'"messages"' in sent
    assert migrator.execute(LEDGER).fetchone()[0] == "OK"  # type: ignore[index]
    assert migrator.execute(ADVISORY).fetchone() == (0,)


@pytest.fixture
def other() -> Iterator[DB]:
    """Otra conexión como `crm_app` (puede terminar las sesiones de su rol; el migrador no)."""
    db = connection.settings_dict
    info = make_conninfo(dbname=db["NAME"], user=db["USER"], password=db["PASSWORD"],
                         host=db["HOST"], port=db["PORT"] or None)  # fmt: skip
    with psycopg.connect(info, autocommit=True) as conn:
        yield conn


def killer(
    desk: SimpleNamespace, other: DB, answered: list[Any], skip: int = 0
) -> Callable[[], None]:
    """Mata la sesión del copiloto (la que tiene el candado) desde otra conexión y, en otro hilo,
    excluye la conversación: el `PUT` real responde 200 antes de seguir. Una vez, tras `skip`."""
    calls = [None] * skip

    def kill() -> None:
        if calls:
            calls.pop()
            return
        if answered:
            return
        [(pid,)] = other.execute(HOLDER).fetchall()
        other.execute("SELECT pg_terminate_backend(%s, 5000)", [pid])
        later(lambda: answered.append(put(desk))).join(20)

    return kill


WHERE = {  # dónde muere la sesión: al conectar, entre TCP y TLS, o en la propia comprobación
    "connect": (urllib3.util.connection, "create_connection", 0),  # el que usa urllib3
    "tls": (urllib3.connection, "_ssl_wrap_socket_and_match_hostname", 0),
    "check": (copilot, "_refused", 1),  # la 1.ª es la de la reserva
}


@pytest.mark.parametrize("where", WHERE)
def test_a_session_killed_before_the_body_leaves_sends_nothing_after_the_exclusion(
    desk: SimpleNamespace, provider: Provider, migrator: DB, other: DB,
    monkeypatch: pytest.MonkeyPatch, where: str,
) -> None:  # fmt: skip
    """La exclusión confirma con 200 (la sesión del copiloto ya no tiene el candado): ni un byte
    sale después; la reserva, a 0; el barrido de huérfanas no la vuelve a cerrar."""
    answered: list[Any] = []
    owner, name, skip = WHERE[where]
    monkeypatch.setattr(owner, name, getattr(owner, name))  # deshecho al terminar
    ahead_of(owner, name, killer(desk, other, answered, skip))
    desk.fake.replies = [PLAIN]
    code = suggest(desk)
    [answer] = answered
    assert answer.status_code == 200 and excluded_at(migrator, desk) is not None
    assert provider.settled(1) == [b""]  # conectada y con TLS, pero cerrada sin escribir nada
    assert code == "INTERNAL_ERROR"  # la comprobación no se pudo hacer: falla cerrado
    assert migrator.execute(RUNS).fetchall() == [("INTERNAL_ERROR", 1)]
    assert migrator.execute(LEDGER).fetchall() == [("PROVIDER_UNAVAILABLE", 0)]
    assert migrator.execute(USAGE).fetchone() == (0, 0)
    [(held,)] = migrator.execute("SELECT id FROM ai_budget_reservations").fetchall()
    assert gateway.settle(ctx(desk.a), held, Outcome.ORPHANED, None) is False  # ya ajustada
    assert orphans.close() == {"closed": 0, "skipped": 0, "failed": 0}
    assert migrator.execute(LEDGER).fetchall() == [("PROVIDER_UNAVAILABLE", 0)]
    assert migrator.execute(ADVISORY).fetchone() == (0,)


def test_a_mark_set_during_the_tls_handshake_is_seen_by_the_check_and_nothing_is_written(
    desk: SimpleNamespace, provider: Provider, migrator: DB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """La sesión sigue viva; la marca llega por otra vía (sin el candado): la comprobación da
    `False` (sin error) y no sale nada."""
    owner, name, _ = WHERE["tls"]
    monkeypatch.setattr(owner, name, getattr(owner, name))
    done: list[None] = []

    def excluded() -> None:
        def both() -> None:
            mark(desk)
            done.append(None)

        later(both).join(10)

    ahead_of(owner, name, excluded)
    desk.fake.replies = [PLAIN]
    assert suggest(desk) == "AI_EXCLUDED" and done == [None]
    assert provider.settled(1) == [b""]
    assert migrator.execute(LEDGER).fetchall() == [("PROVIDER_UNAVAILABLE", 0)]
    assert migrator.execute(RUNS).fetchall() == [("AI_EXCLUDED", 1)]


def test_a_lost_lock_on_a_reopened_connection_withholds_and_is_not_reported_as_excluded(
    desk: SimpleNamespace, provider: Provider, migrator: DB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """La conexión de la sesión se cierra durante el apretón TLS; la comprobación vuelve a conectar
    (otro pid, sin el candado) con la marca vacía: no sale nada y el run es `INTERNAL_ERROR`, no un
    `AI_EXCLUDED` que diría algo falso."""
    owner, name, _ = WHERE["tls"]
    monkeypatch.setattr(owner, name, getattr(owner, name))
    ahead_of(owner, name, connection.close)
    desk.fake.replies = [PLAIN]
    assert suggest(desk) == "INTERNAL_ERROR"
    assert provider.settled(1) == [b""] and excluded_at(migrator, desk) is None
    assert migrator.execute(RUNS).fetchall() == [("INTERNAL_ERROR", 1)]
    assert migrator.execute(LEDGER).fetchall() == [("PROVIDER_UNAVAILABLE", 0)]
    assert migrator.execute(ADVISORY).fetchone() == (0,)


def test_a_check_the_database_does_not_answer_in_time_withholds_the_send(
    desk: SimpleNamespace, provider: Provider, migrator: DB, monkeypatch: pytest.MonkeyPatch,
    settings: Any,
) -> None:  # fmt: skip
    """La comprobación de antes del envío tiene su `statement_timeout`: una base que tarda no la
    retiene; se cancela, no sale nada y la reserva se libera."""
    settings.AI_SEND_CHECK_SECONDS = 0.3
    refused, calls = copilot._refused, list[None]()

    def slow(*args: Any) -> Any:
        calls.append(None)
        if len(calls) == 2:  # la 1.ª es la de la reserva
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_sleep(5)")
        return refused(*args)

    monkeypatch.setattr(copilot, "_refused", slow)
    desk.fake.replies = [PLAIN]
    started = time.monotonic()
    assert suggest(desk) == "INTERNAL_ERROR" and time.monotonic() - started < 4
    assert provider.settled(1) == [b""]
    assert migrator.execute(LEDGER).fetchall() == [("PROVIDER_UNAVAILABLE", 0)]


BOUND = {  # F8-13e: lo que pasaba con el tope por sentencia de F8-13d y ya no pasa
    "many": (0.5, 3, 0.3, 0),  # tres sentencias de 0,3 s, cada una bajo el tope; juntas, no
    "tiny": (0.0004, 1, 1.5, 0),  # menos de 1 ms daba `0ms`: sin tope
    "late": (0.3, 0, 0, 0.5),  # un `True` que llega fuera del plazo
}


@pytest.mark.parametrize("case", BOUND)
def test_the_whole_check_has_one_bound_and_a_late_yes_does_not_send(
    desk: SimpleNamespace, provider: Provider, migrator: DB, monkeypatch: pytest.MonkeyPatch,
    settings: Any, case: str,
) -> None:  # fmt: skip
    seconds, statements, each, after = BOUND[case]
    settings.AI_SEND_CHECK_SECONDS = seconds
    refused, calls = copilot._refused, list[None]()

    def slow(*args: Any) -> Any:
        calls.append(None)
        if len(calls) != 2:  # la 1.ª es la de la reserva
            return refused(*args)
        with connection.cursor() as cursor:
            for _ in range(statements):
                cursor.execute("SELECT pg_sleep(%s)", [each])
        answer = refused(*args)
        threading.Event().wait(after)  # `desk` sustituye `time.sleep`
        return answer

    monkeypatch.setattr(copilot, "_refused", slow)
    desk.fake.replies = [PLAIN]
    started = time.monotonic()
    assert suggest(desk) == "INTERNAL_ERROR" and time.monotonic() - started < 2.5
    assert provider.settled(1) == [b""] and connection.execute_wrappers == []
    assert migrator.execute(LEDGER).fetchall() == [("PROVIDER_UNAVAILABLE", 0)]


def test_a_retry_on_a_new_connection_is_checked_again_and_withheld(
    desk: SimpleNamespace, provider: Provider, migrator: DB, other: DB,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    """Un 429; en su espera muere la sesión y la exclusión responde 200: el reenvío, por una
    conexión nueva, se comprueba otra vez y no escribe nada."""
    answered: list[Any] = []
    monkeypatch.setattr(time, "sleep", lambda _: killer(desk, other, answered)())
    desk.fake.script, desk.fake.replies = ["rate_limit"], [PLAIN]
    assert suggest(desk) == "INTERNAL_ERROR"
    assert [a.status_code for a in answered] == [200]
    first, second = provider.settled(2)
    assert first.startswith(b"POST /v1/messages") and second == b""
    assert migrator.execute(LEDGER).fetchall() == [("PROVIDER_UNAVAILABLE", 0)]


def test_after_the_session_dies_the_check_never_passes_even_on_a_new_connection(
    desk: SimpleNamespace, other: DB
) -> None:
    """La comprobación corre en la conexión que tiene el candado; si Django vuelve a conectar, la
    sesión nueva (otro pid) no lo tiene: `False`, nunca «reconecta y pasa»."""
    mine = ctx(desk.a, desk.ana)
    with attention.ai_call(mine, desk.convo, 5):
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_backend_pid()")
            [(before,)] = cursor.fetchall()
        other.execute("SELECT pg_terminate_backend(%s, 5000)", [before])
        seen: list[bool | str] = []
        for _ in range(3):
            try:
                with tenant_scope(mine):
                    seen.append(attention.ai_included(mine, desk.convo))
            except DatabaseError:
                seen.append("error")
            except attention.CallLockLost:
                seen.append("lost")
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_backend_pid()")
            [(after,)] = cursor.fetchall()
    assert True not in seen and "lost" in seen and after != before


@pytest.mark.parametrize("answer", [False, None, 1, "yes", RuntimeError("x"), DatabaseError("x")])
def test_core_http_writes_nothing_unless_the_check_says_true(
    provider: Provider, answer: Any
) -> None:
    """`before_send`: otra cosa que `True`, o un error (sin su mensaje), es `Withheld`; urllib3
    no la reintenta y la conexión ya estaba abierta (TLS) cuando corrió."""
    calls: list[bool] = []

    def check() -> Any:
        calls.append(True)
        if isinstance(answer, Exception):
            raise answer
        return answer

    url = f"https://{ai_sim.HOST}/v1/messages"
    with pytest.raises(http.Withheld) as caught:
        http.request("POST", url, body=b"{}", before_send=check)
    assert caught.value.__cause__ is None and str(caught.value) == ""
    assert calls == [True] and provider.settled(1) == [b""]
    assert http.never_connected(caught.value)


def test_core_http_runs_the_check_after_the_handshake_and_keeps_its_policy(
    provider: Provider, desk: SimpleNamespace
) -> None:
    seen: list[Any] = []

    def check() -> bool:
        seen.append(len(provider.received))
        return True

    desk.fake.replies = [PLAIN]
    url = f"https://{ai_sim.HOST}/v1/messages"
    reply = http.request("POST", url, headers={"x-api-key": ai_sim.TEST_KEY}, body=b"{}",
                         before_send=check)  # fmt: skip
    assert reply.status == 200 and seen == [0] and len(provider.settled(1)) == 1
    for bad in ("https://evil.example/v1", f"http://{ai_sim.HOST}/v1"):
        with pytest.raises(http.BlockedDestination):  # la política, antes de todo
            http.request("POST", bad, body=b"{}", before_send=lambda: True)
    assert len(provider.received) == 1 and http._before_send.get() is None


def test_the_local_simulator_path_also_runs_the_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """ADR-041: el gestor del simulador (http) tampoco escribe sin la comprobación."""
    client, server = socket.socketpair()
    monkeypatch.setattr(http, "_simulated", lambda url: "http://graph.local/v1")
    monkeypatch.setattr("urllib3.connection.connection.create_connection", lambda *a, **k: client)
    http._simulator.clear()
    with pytest.raises(http.Withheld):
        http.request("GET", f"https://{ai_sim.HOST}/v1/models", before_send=lambda: False)
    server.settimeout(2)
    assert server.recv(100) == b""
    server.close()
    http._simulator.clear()
