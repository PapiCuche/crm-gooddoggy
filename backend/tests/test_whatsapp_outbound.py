"""F7-05b: el envío por WhatsApp contra la Graph API falsa (`tests/meta_sim`): reclamo en `SENDING`,
llamada sin transacción, resultados ambiguos que no se reenvían, token y tenant. PostgreSQL real con
el rol `crm_app`; el simulador sustituye solo la capa de red de `core.http`."""

import dataclasses
import datetime as dt
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import Any, cast

import pytest
import urllib3
from django.core.management import call_command
from django.db import connection
from django.utils import timezone

from apps.contacts.models import Contact
from apps.conversations import accounts, channels, commands, credentials, limiter, tasks, whatsapp
from apps.conversations.channels import AmbiguousChannelError, TemporaryChannelError
from apps.conversations.models import TOKEN_PURPOSE, ChannelAccount, Conversation, Credential
from core import http, sealed
from core.ids import new_id
from core.tenancy.scope import tenant_scope
from tests import test_conversations_tables as tables
from tests.meta_sim import GRAPH_VERSION, transport
from tests.meta_sim.graph import TEST_TOKEN, FakeGraph
from tests.meta_sim.webhooks import PHONE_NUMBER_ID, WABA_ID
from tests.test_conversation_commands import audit, outbound, world, writer
from tests.test_conversations_dispatch import Retrying, deliveries, state
from tests.test_memberships import ctx
from tests.test_pipelines_tables import as_migrator
from tests.test_whatsapp_account import credential as tables_credential

__all__ = ["world", "writer"]  # los fixtures, para pytest
pytestmark = pytest.mark.usefixtures("tenant_db")
PHONE = "+51900000111"


class FakeRedis:
    """Lo único que usa el limitador: contar, leer el plazo y fijarlo (`pipeline` de una
    transacción)."""

    def __init__(self) -> None:
        self.counts: dict[str, int] = {}
        self.windows: dict[str, int] = {}

    def incr(self, key: str) -> int:
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]

    def ttl(self, key: str) -> int:
        return self.windows.get(key, -1)

    def expire(self, key: str, window: int) -> None:
        self.windows[key] = window

    def pipeline(self, transaction: bool = True) -> FakePipe:
        return FakePipe(self)


class FakePipe:
    def __init__(self, server: FakeRedis) -> None:
        self.server, self.calls = server, []  # type: ignore[var-annotated]

    def __enter__(self) -> FakePipe:
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def incr(self, key: str) -> None:
        self.calls.append(lambda: self.server.incr(key))

    def ttl(self, key: str) -> None:
        self.calls.append(lambda: self.server.ttl(key))

    def execute(self) -> list[int]:
        return [call() for call in self.calls]


@pytest.fixture(autouse=True)
def redis(monkeypatch: pytest.MonkeyPatch) -> FakeRedis:
    fake = FakeRedis()
    monkeypatch.setattr(limiter, "client", lambda: fake)
    return fake


@pytest.fixture
def graph(monkeypatch: pytest.MonkeyPatch, settings: Any) -> FakeGraph:
    settings.META_ENABLED, settings.META_GRAPH_VERSION = True, GRAPH_VERSION
    settings.HTTP_ALLOWED_HOSTS = [transport.GRAPH_HOST]
    fake = FakeGraph()
    transport.install(monkeypatch, fake)
    return fake


def talk(world: SimpleNamespace) -> Conversation:
    """Una conversación de WhatsApp conectada, a la que el cliente escribió hace una hora."""
    mine = new_id()
    with tenant_scope(ctx(world.a)) as tenant:
        stored = credentials.store(tenant, mine, TEST_TOKEN)
        made = ChannelAccount.objects.create(
            id=mine, platform="WHATSAPP", name="WA", status="CONNECTED", credential_id=stored.pk,
            external_account_id=PHONE_NUMBER_ID, external_parent_id=WABA_ID,
            last_healthcheck_at=timezone.now(),
        )  # fmt: skip
        Contact.objects.filter(pk=world.contact_a.pk).update(primary_phone_e164=PHONE)
    return tables.conversation(  # type: ignore[no-any-return]
        world.a, world.contact_a, made, last_inbound_at=timezone.now() - dt.timedelta(hours=1)
    )


def send(world: SimpleNamespace, convo: Conversation) -> Any:
    message, _ = outbound(world, convo, body="Hola, ¿en qué te ayudo?")
    tasks.deliver(Retrying(0), world.a, message.pk)
    return message, state(world, message)


def test_a_reply_inside_the_window_goes_out_with_its_token_and_callback_data(
    world: SimpleNamespace, graph: FakeGraph, migrator: Any
) -> None:
    message, saved = send(world, talk(world))
    assert (saved.delivery_status, saved.external_message_id) == ("SENT", "wamid.SIMOUT000001")
    sent, call = graph.sent[0], graph.calls[0]
    assert (sent["to"], sent["type"]) == ("51900000111", "text")
    assert sent["biz_opaque_callback_data"] == str(message.pk)
    assert call["path"] == f"/{GRAPH_VERSION}/{PHONE_NUMBER_ID}/messages"
    assert call["headers"]["Authorization"] == f"Bearer {TEST_TOKEN}"  # en la cabecera
    moves = [c for c, _ in deliveries(migrator)]
    assert moves == [["QUEUED", "SENDING"], ["SENDING", "SENT"]] and saved.sending_at is not None
    assert TEST_TOKEN not in str(audit(migrator)) + call["path"]  # ni token ni texto


@pytest.mark.parametrize("outcome", ["cut", "timeout", "cut_after", "timeout_after", "server"])
def test_an_ambiguous_result_is_never_resent_and_the_message_stays_sending(
    world: SimpleNamespace, graph: FakeGraph, outcome: str
) -> None:
    graph.then(outcome)
    message, saved = send(world, talk(world))
    assert saved.delivery_status == "SENDING"
    tasks.deliver(Retrying(0), world.a, message.pk)  # otro despachador, el barredor o un reintento
    assert state(world, message).delivery_status == "SENDING"
    assert len(graph.requests) == 1  # Meta recibió una sola petición: no se reenvió a ciegas
    assert len(graph.sent) == (1 if outcome.endswith("_after") else 0)  # lo aceptado, solo una vez


def test_a_refused_token_puts_the_account_in_error_and_stops_the_queue(
    world: SimpleNamespace, graph: FakeGraph, migrator: Any
) -> None:
    convo = talk(world)
    first, second = outbound(world, convo)[0], outbound(world, convo)[0]
    graph.then("token")
    tasks.deliver(Retrying(0), world.a, first.pk)
    with tenant_scope(ctx(world.a)):
        found = ChannelAccount.objects.get(pk=convo.channel_account_id)
    assert (state(world, first).delivery_status, found.status) == ("FAILED", "ERROR")
    assert found.status_detail == "TOKEN_INVALID"
    tasks.deliver(Retrying(0), world.a, second.pk)  # lo que seguía en cola falla por la política
    assert state(world, second).delivery_status == "FAILED" and len(graph.requests) == 1
    assert [a for a, *_ in audit(migrator) if a.endswith("token_invalid")] == [
        "channel_account.token_invalid"
    ]
    with tenant_scope(ctx(world.a)) as tenant:  # solo una vez, y solo una cuenta viva
        assert not accounts.mark_token_invalid(tenant, channel_account_id=found.pk)


def test_nothing_is_sent_to_a_host_off_the_allowlist_or_with_a_credential_of_another_account(
    world: SimpleNamespace, graph: FakeGraph, settings: Any
) -> None:
    convo, hosts = talk(world), settings.HTTP_ALLOWED_HOSTS
    settings.HTTP_ALLOWED_HOSTS = []  # fuera de la allowlist: ni se conecta, y se reintenta
    message, _ = outbound(world, convo)
    with pytest.raises(RuntimeError, match="retry"):
        tasks.deliver(Retrying(0), world.a, message.pk)
    assert graph.requests == [] and state(world, message).delivery_status == "QUEUED"
    settings.HTTP_ALLOWED_HOSTS = hosts
    with tenant_scope(ctx(world.a)):  # una credencial viva que es de otra cuenta
        other = tables_credential(world.a)
        ChannelAccount.objects.filter(pk=convo.channel_account_id).update(credential_id=other)
    assert send(world, convo)[1].delivery_status == "FAILED" and graph.requests == []


def test_the_token_is_opened_only_during_the_call_and_never_stored_in_the_claim(
    world: SimpleNamespace, graph: FakeGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[str] = []
    real = credentials.open_token

    def spy(credential: Any) -> str:
        opened.append("x")
        return real(credential)

    monkeypatch.setattr(credentials, "open_token", spy)
    convo = talk(world)
    message, _ = outbound(world, convo)
    with tenant_scope(ctx(world.a)) as tenant:
        claim = commands.claim_outbound(tenant, message_id=message.pk)
    assert claim is not None and opened == []  # reclamar no abre el token
    assert TEST_TOKEN not in repr(claim) and "Hola" not in repr(claim)  # ni el texto
    assert channels.adapter_for("WHATSAPP").send(claim).ok and opened == ["x"]


def test_the_limiter_counts_per_account_and_window_and_stops_a_burst_before_meta(
    world: SimpleNamespace, graph: FakeGraph, settings: Any, redis: FakeRedis
) -> None:
    settings.CHANNEL_SEND_LIMIT = (1, 3)  # un envío cada 3 s por cuenta
    convo = talk(world)
    assert send(world, convo)[1].delivery_status == "SENT"
    message, _ = outbound(world, convo)
    task = Retrying(0)
    with pytest.raises(RuntimeError, match="retry"):
        tasks.deliver(task, world.a, message.pk)  # sin cupo: no sale y se devuelve
    assert state(world, message).delivery_status == "QUEUED" and len(graph.sent) == 1
    assert (
        list(redis.windows.values()) == [3]
        and f"{convo.channel_account_id}" in list(redis.counts)[0]
    )
    assert limiter.allow(new_id())  # otra cuenta tiene su propio cupo


def test_the_limiter_window_is_fixed_when_the_key_is_created_and_never_extended(
    settings: Any, redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings.CHANNEL_SEND_LIMIT = (5, 3)
    expired: list[str] = []
    real = redis.expire

    def spy(key: str, window: int) -> None:
        expired.append(key)
        real(key, window)

    monkeypatch.setattr(redis, "expire", spy)
    account = new_id()
    assert [limiter.allow(account) for _ in range(4)] == [True] * 4
    assert expired == [f"chn:send:{account}"]  # una vez: otra más alargaría la ventana sin fin


def test_a_limiter_key_that_lost_its_expiry_gets_one_again(settings: Any, redis: FakeRedis) -> None:
    settings.CHANNEL_SEND_LIMIT = (2, 5)
    account = new_id()
    redis.counts[f"chn:send:{account}"] = 7  # una clave sin plazo (perdió su `EXPIRE`)
    assert not limiter.allow(account)  # sigue contando...
    assert redis.windows[f"chn:send:{account}"] == 5  # ...y recupera su plazo: no bloquea siempre


def test_without_redis_a_send_fails_closed_towards_the_retry_and_logs_no_url(
    world: SimpleNamespace, graph: FakeGraph, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:  # fmt: skip
    def down() -> Any:
        raise ConnectionError("redis://:secret@broker:6379/0")

    monkeypatch.setattr(limiter, "client", down)
    message, _ = outbound(world, talk(world))
    with pytest.raises(RuntimeError, match="retry"):
        tasks.deliver(Retrying(0), world.a, message.pk)
    assert graph.requests == [] and state(world, message).delivery_status == "QUEUED"
    assert "secret" not in caplog.text and "redis://" not in caplog.text


def test_two_dispatchers_at_once_send_to_meta_once(
    world: SimpleNamespace, graph: FakeGraph
) -> None:
    message, _ = outbound(world, talk(world))
    start = threading.Barrier(2)

    def go(_: int) -> None:
        try:
            start.wait(timeout=10)
            tasks.deliver(Retrying(0), world.a, message.pk)
        finally:
            connection.close()

    with ThreadPoolExecutor(2) as pool:
        list(pool.map(go, range(2)))
    assert len(graph.sent) == 1 and state(world, message).delivery_status == "SENT"


def test_a_tls_error_while_reading_the_answer_never_repeats_the_post_and_is_ambiguous() -> None:
    import ssl
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    hits: list[int] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.rfile.read(int(self.headers.get("content-length", 0)))
            hits.append(1)
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    real = urllib3.connection.HTTPConnection.getresponse

    def cut(self: Any, *args: Any, **kwargs: Any) -> Any:
        real(self, *args, **kwargs)  # el servidor ya tiene la petición
        raise ssl.SSLError("tlsv1 alert internal error")

    try:
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(urllib3.connection.HTTPConnection, "getresponse", cut)
            with pytest.raises(urllib3.exceptions.HTTPError) as caught:
                url = f"http://127.0.0.1:{server.server_address[1]}/x"
                plain = urllib3.PoolManager(retries=http._manager.connection_pool_kw["retries"])
                plain.request("POST", url, json={"a": 1}, redirect=False)  # los mismos reintentos
    finally:
        server.shutdown()
    assert len(hits) == 1 and not http.never_connected(caught.value)
    assert "POST" not in http._manager.connection_pool_kw["retries"].allowed_methods
    err = urllib3.exceptions
    assert http.never_connected(err.SSLError(ssl.SSLCertVerificationError("certificado")))
    assert not http.never_connected(err.SSLError(ssl.SSLError("alerta")))  # ni un cierre limpio
    assert not http.never_connected(err.SSLError(ssl.SSLZeroReturnError("cerrado")))


def test_only_a_failure_before_the_request_is_sent_is_safe_to_repeat() -> None:
    err, pool = urllib3.exceptions, cast("Any", None)
    assert http.never_connected(http.BlockedDestination("x"))
    assert http.never_connected(err.NewConnectionError(pool, "x"))
    assert http.never_connected(err.ConnectTimeoutError(pool, "x"))
    slow = err.ReadTimeoutError(pool, "u", "x")  # el tiempo de lectura es ambiguo, también envuelto
    assert not http.never_connected(slow) and not http.never_connected(err.ProtocolError("x"))
    assert not http.never_connected(err.MaxRetryError(pool, "u", reason=slow))
    assert http.never_connected(
        err.MaxRetryError(pool, "u", reason=err.NewConnectionError(pool, "x"))
    )


def test_no_token_or_text_in_the_logs_whatever_the_outcome(
    world: SimpleNamespace, graph: FakeGraph, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("DEBUG")
    convo = talk(world)
    for outcome in ("cut", "timeout_after", "server", "window_closed", "quality", "token"):
        graph.then(outcome)
        send(world, convo)
    assert TEST_TOKEN not in caplog.text and "Hola, ¿en" not in caplog.text


def test_a_phone_number_id_that_is_not_digits_never_reaches_the_url(
    monkeypatch: pytest.MonkeyPatch, settings: Any
) -> None:
    settings.META_ENABLED = True
    calls: list[str] = []
    monkeypatch.setattr(http, "request", lambda *a, **k: calls.append(a[1]))
    mine = new_id()
    box = sealed.seal(TOKEN_PURPOSE, credentials.owner(mine), TEST_TOKEN.encode())
    for bad in ("123/../x", "12345?a=b", "12345@evil.io", "1234"):
        claim = channels.Claim(new_id(), mine, "WHATSAPP", bad, PHONE, "x",
                               Credential(owner_id=mine, ciphertext=box))  # fmt: skip
        with pytest.raises(ValueError, match="identificador"):
            whatsapp.WhatsAppAdapter().send(claim)
    assert calls == []


OUT = {"direction": "OUTBOUND", "sender_type": "USER"}
ZERO = dt.timedelta(0)


ANSWERS = [  # (estado HTTP, cuerpo, qué pasa)
    (200, b"{}", AmbiguousChannelError), (200, b'{"messages":[{"id":" "}]}', AmbiguousChannelError),
    (400, b'{"error": {"code": 190}}', "TOKEN_INVALID"), (401, b"", "TOKEN_INVALID"),
    (400, b'{"error": {"code": true}}', "PROVIDER_REJECTED"), (429, b"", TemporaryChannelError),
    (503, b"", AmbiguousChannelError),
    *[(400, b'{"error": {"code": %d}}' % c, TemporaryChannelError)
      for c in (4, 17, 613, 80007, 130429, 131056)],  # los límites de tasa de Meta
    (400, b'{"error": {"code": 131047}}', "WINDOW_CLOSED"),
    (400, b'{"error": {"code": 131026}}', "RECIPIENT_UNREACHABLE"),
]  # fmt: skip


@pytest.mark.parametrize(("status", "raw", "outcome"), ANSWERS)
def test_every_shape_of_answer_is_a_closed_outcome_and_an_unclear_success_is_never_resent(
    monkeypatch: pytest.MonkeyPatch, settings: Any, status: int, raw: bytes, outcome: Any
) -> None:
    settings.META_ENABLED = True
    monkeypatch.setattr(http, "request", lambda *a, **k: SimpleNamespace(status=status, data=raw))
    mine = new_id()
    box = sealed.seal(TOKEN_PURPOSE, credentials.owner(mine), TEST_TOKEN.encode())
    claim = channels.Claim(new_id(), mine, "WHATSAPP", PHONE_NUMBER_ID, PHONE, "x",
                           Credential(owner_id=mine, ciphertext=box))  # fmt: skip
    nobody = dataclasses.replace(claim, recipient="")  # un contacto sin teléfono: no hay a quién
    assert whatsapp.WhatsAppAdapter().send(nobody).reason == "RECIPIENT_UNREACHABLE"
    copied = dataclasses.replace(
        claim,
        credential=Credential(owner_id=new_id(), ciphertext=box, key_version=sealed.current()),
    )
    assert whatsapp.WhatsAppAdapter().send(copied).reason == "TOKEN_INVALID"  # no se abre
    if isinstance(outcome, str):
        assert whatsapp.WhatsAppAdapter().send(claim) == channels.SendResult(False, reason=outcome)
    else:
        with pytest.raises(outcome):
            whatsapp.WhatsAppAdapter().send(claim)


def test_only_a_sending_message_can_be_settled_or_returned(world: SimpleNamespace) -> None:
    queued = tables.message(
        world.a, tables.first(world), delivery_status="QUEUED", sender_user_id=world.ana.pk, **OUT
    )
    with tenant_scope(ctx(world.a)) as tenant:
        ok = channels.SendResult(True, "x")
        assert commands.settle_outbound(tenant, message_id=queued.pk, result=ok) is None
        assert commands.release_outbound(tenant, message_id=queued.pk, wait=ZERO) is None


def test_the_migration_goes_back_failing_what_was_sending_and_forward_again(
    world: SimpleNamespace, migrator: Any
) -> None:
    held = tables.message(
        world.a, tables.first(world), delivery_status="SENDING", sender_user_id=world.ana.pk, **OUT
    )
    with as_migrator():
        call_command("migrate", "conversations", "0012", verbosity=0)
    status = "SELECT delivery_status FROM messages WHERE id = %s"  # el modelo ya no coincide
    assert migrator.execute(status, [held.pk]).fetchone() == ("FAILED",)  # ningún estado ajeno
    with as_migrator():
        call_command("migrate", verbosity=0)
    assert state(world, held).sending_at is None  # la columna vuelve
