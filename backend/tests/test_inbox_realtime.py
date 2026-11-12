"""F6-07a: el WebSocket del Inbox y sus eventos (PostgreSQL y RLS reales; canales en memoria)."""

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import ANY, Mock
from uuid import UUID, uuid4

import psycopg
import pytest
from asgiref.sync import async_to_sync, sync_to_async
from asgiref.testing import ApplicationCommunicator
from channels.layers import channel_layers, get_channel_layer
from django.conf import settings
from django.contrib.sessions.backends.db import SessionStore
from django.test import Client

from apps.accounts.models import User
from apps.accounts.services import AUTH_AT
from apps.conversations import commands, realtime, tasks
from apps.conversations.models import Conversation
from config.asgi import application
from core.outbox import emit, handlers_for
from core.outbox.publisher import publish_outbox
from core.tenancy.context import TenantContext
from core.tenancy.scope import tenant_scope
from tests.factories import sign_in
from tests.test_authorization import give
from tests.test_conversation_scopes import crm, inbox, real_stack
from tests.test_memberships import ctx

__all__ = ["crm", "inbox", "real_stack"]  # los fixtures, para pytest
pytestmark = pytest.mark.usefixtures("tenant_db", "no_realtime_broker")
DB = psycopg.Connection[Any]
SECRET = "texto-que-no-debe-salir-9f3a"  # noqa: S105 — dato ficticio de test
ORIGIN, FORBIDDEN, NOT_FOUND, NO_SESSION, TOO_MANY = b"http://testserver", 4403, 4404, 4401, 4429
LOSE_ROLE = "DELETE FROM membership_roles WHERE membership_id = %s"
SUSPEND = "UPDATE organization_memberships SET status = 'SUSPENDED' WHERE id = %s"
SUSPEND_ORG = "UPDATE organizations SET status = 'SUSPENDED' WHERE id = %s"
REVOKE = "UPDATE users SET session_epoch = session_epoch + 1 WHERE id = %s"
HOSTILE = ('{"slug": "org-b", "scope": "ORGANIZATION"}', "[1]", '"ping"', "{no es json")
ASSIGN = "UPDATE conversations SET assigned_user_id = %s WHERE id = %s"
PING = '{"type": "ping"}'


@pytest.fixture(autouse=True)
def fresh_layer(settings: Any) -> None:
    channel_layers.backends.clear()
    realtime._open.clear()
    settings.INBOX_WS_TICK_SECONDS = 0.05


class Socket(ApplicationCommunicator):
    def __init__(self, slug: str, session: bytes | None, origin: bytes | None, query: str) -> None:
        path = f"/ws/o/{slug}/inbox/"
        headers = [(b"origin", origin)] if origin else []
        headers += [(b"cookie", session)] if session else []
        scope = {"type": "websocket", "path": path, "query_string": query.encode(),
                 "headers": headers, "subprotocols": []}  # fmt: skip
        super().__init__(application, scope)

    async def connect(self) -> tuple[bool, Any]:
        await self.send_input({"type": "websocket.connect"})
        out = await self.receive_output(timeout=2)
        return out["type"] == "websocket.accept", out.get("code")

    async def send_to(self, text: str | None = None, data: bytes | None = None) -> None:
        await self.send_input({"type": "websocket.receive", "text": text, "bytes": data})

    async def frames(self, count: int) -> list[dict[str, Any]]:
        got = [await self.receive_output(timeout=10) for _ in range(count)]
        assert all(out["type"] == "websocket.send" for out in got), got
        return [json.loads(out["text"]) for out in got]

    async def closed_with(self) -> Any:
        try:
            out = await self.receive_output(timeout=6)
        except TimeoutError:
            return None
        return out.get("code") if out["type"] == "websocket.close" else out

    async def disconnect(self) -> None:
        await self.send_input({"type": "websocket.disconnect", "code": 1000})
        await self.wait(timeout=2)


def cookie(user: User) -> bytes:
    client, name = Client(), settings.SESSION_COOKIE_NAME
    sign_in(client, user)
    return f"{name}={client.cookies[name].value}".encode()


@asynccontextmanager
async def connected(slug: str, user: User) -> AsyncIterator[Socket]:
    comm = Socket(slug, await sync_to_async(cookie)(user), ORIGIN, "")
    ok, code = await comm.connect()
    assert ok, code
    try:
        yield comm
    finally:
        await comm.disconnect()


async def try_connect(
    slug: str, session: bytes | None, origin: bytes | None = ORIGIN, query: str = ""
) -> tuple[bool, Any]:
    comm = Socket(slug, session, origin, query)
    ok, code = await comm.connect()
    await comm.disconnect()
    return ok, code


def attempt(slug: str, user: User | None, **kwargs: Any) -> Any:
    kwargs.setdefault("session", cookie(user) if user else None)
    return run(lambda: try_connect(slug, **kwargs))


def run(coro: Callable[[], Awaitable[Any]]) -> Any:
    async def main() -> Any:
        return await coro()

    return async_to_sync(main)()


async def push(org: UUID, kind: str, convo: UUID, previous: UUID | None = None) -> None:
    """Un evento publicado al grupo de la organización, como lo hace la tarea del outbox."""
    aggregate = convo if kind.startswith("conversation.") else uuid4()
    payload = {"conversation_id": str(convo), "previous_user_id": previous and str(previous)}
    message = realtime.outgoing(kind, aggregate, payload, "t")
    assert message is not None
    await sync_to_async(realtime.publish)(org, message)


def seen(inbox: SimpleNamespace, got: list[dict[str, Any]]) -> set[str]:
    by_id = {str(v): k for k, v in inbox.convs.items()}
    return {by_id[f["conversation_id"]] for f in got}


def test_each_session_receives_only_what_its_scope_and_organization_reach(
    inbox: SimpleNamespace, migrator: DB
) -> None:
    give(inbox.b, inbox.m_luis, {"conversations.view": "ORGANIZATION"})

    async def go() -> None:
        async with connected("org-a", inbox.ana) as ana, connected("org-a", inbox.vera) as vera:
            async with connected("org-a", inbox.sam) as sam:
                for name in ("vera", "sam", "ana", "team", "nobody", "both"):
                    await push(inbox.a, "message.received", inbox.convs[name])
                assert seen(inbox, await ana.frames(6)) == set(inbox.convs) - {"b"}
                assert seen(inbox, await vera.frames(3)) == {"vera", "nobody", "both"}  # OWN
                assert seen(inbox, await sam.frames(4)) == {"sam", "team", "nobody", "both"}  # TEAM
                async with connected("org-b", inbox.luis) as luis:  # de otra organización
                    await push(inbox.a, "message.received", inbox.convs["ana"])
                    await push(inbox.b, "message.received", inbox.convs["ana"])  # id de A, en B
                    await push(inbox.b, "message.received", inbox.convs["b"])  # centinela de luis
                    assert seen(inbox, await luis.frames(1)) == {"b"}  # el de A no llegó antes
                    assert seen(inbox, await ana.frames(1)) == {"ana"}
                taken = inbox.convs["nobody"]  # sam la toma: vera ya no la ve, y sigue abierta
                migrator.execute(ASSIGN, [inbox.sam.pk, taken])
                await push(inbox.a, "message.received", taken)
                await push(inbox.a, "message.received", inbox.convs["vera"])  # centinela
                assert seen(inbox, await vera.frames(1)) == {"vera"}  # la tomada no llegó
                assert seen(inbox, await ana.frames(2)) == {"nobody", "vera"}

    run(go)


def test_the_outbox_reaches_the_connection_with_only_type_ids_and_time(
    inbox: SimpleNamespace, migrator: DB, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        tasks.publish_realtime, "apply_async", lambda **kw: tasks.publish_realtime.apply(**kw)
    )
    with tenant_scope(ctx(inbox.a)):
        convo = Conversation.objects.get(pk=inbox.convs["vera"])

    def receive() -> Any:
        with tenant_scope(TenantContext(inbox.a, "test")) as tenant:
            return commands.record_inbound(
                tenant, contact_id=convo.contact_id, channel_account_id=convo.channel_account_id,
                external_message_id="wamid.rt", body=SECRET,
            )[0]  # fmt: skip

    async def go() -> None:
        async with connected("org-a", inbox.vera) as vera:
            message = await sync_to_async(receive)()
            assert await sync_to_async(publish_outbox)() == 1
            (event,) = await vera.frames(1)
            assert event == {"type": "message.received", "message_id": str(message.pk),
                             "conversation_id": str(convo.pk), "at": event["at"]}  # fmt: skip
            assert SECRET not in json.dumps(event) and "vera" not in json.dumps(event)

    run(go)
    assert all(
        SECRET not in t for (t,) in migrator.execute("SELECT payload::text FROM outbox_events")
    )
    cid, mid, pii = uuid4(), uuid4(), {"body": SECRET, "name": "Ana", "phone": "+51"}
    sent = realtime.outgoing("message.updated", mid, {"conversation_id": str(cid), **pii}, "t")
    assert sent and set(sent["event"]) == {"type", "message_id", "conversation_id", "at"}
    assert realtime.outgoing("contact.created", mid, {"conversation_id": str(cid)}, "t") is None
    assert realtime.outgoing("message.received", mid, {"conversation_id": "x"}, "t") is None


@pytest.mark.parametrize("lose", [LOSE_ROLE, SUSPEND])
def test_a_member_who_loses_the_permission_or_the_membership_is_dropped(
    inbox: SimpleNamespace, migrator: DB, lose: str
) -> None:
    async def go() -> None:
        async with connected("org-a", inbox.vera) as vera:
            migrator.execute(lose, [inbox.m["vera"]])
            await push(inbox.a, "message.received", inbox.convs["vera"])
            assert await vera.closed_with() == FORBIDDEN  # sin el dato
            await push(inbox.a, "message.received", inbox.convs["vera"])
            assert await vera.receive_nothing(timeout=0.2) and not realtime._open

    run(go)


@pytest.mark.parametrize("why", ["none", "garbage", "deleted", "expired", "old", "revoked", "url"])
def test_without_a_valid_session_in_the_cookie_the_connection_is_refused(
    inbox: SimpleNamespace, migrator: DB, why: str
) -> None:
    session: Any = cookie(inbox.ana)
    key, name = session.split(b"=")[1].decode(), settings.SESSION_COOKIE_NAME.encode()
    sql = {"deleted": "DELETE FROM django_session", "revoked": REVOKE.replace("WHERE id = %s", "")}
    sql["expired"] = "UPDATE django_session SET expire_date = now() - interval '1 day'"
    if why in sql:
        migrator.execute(sql[why])
    if why == "old":  # pasó la vida absoluta de la sesión
        store = SessionStore(key)
        store[AUTH_AT] = 1
        store.save()
    other = {
        "none": None,
        "garbage": name + b"=" + b"x" * 32,
        "url": None,
    }  # un token en la URL no vale
    extra = {"session": other.get(why, session), "query": f"sessionid={key}" * (why == "url")}
    assert attempt("org-a", inbox.ana, **extra) == (False, NO_SESSION)


@pytest.mark.parametrize("origin", [None, b"http://evil.example", b"https://testserver.evil.io"]
                         + [b"null", b"https://testserver", b"http://testserver:8000"]
                         + [b"http://testserver:99999", b"http://testserver:80.evil.io"]
                         + [b"http://[::1", b"http://testserver:-1"])  # fmt: skip
def test_a_missing_or_foreign_origin_is_refused_even_with_a_valid_session(
    inbox: SimpleNamespace, origin: bytes | None
) -> None:
    assert attempt("org-a", inbox.ana, origin=origin)[0] is False


def test_an_unknown_organization_and_one_without_membership_answer_the_same(
    inbox: SimpleNamespace, migrator: DB
) -> None:
    staff = User.objects.create_superuser("staff@example.com", "correct-horse-battery-staple")
    ana = inbox.ana
    wrong = [("no-existe", ana), ("org-b", ana), ("org-a", inbox.luis), ("org-a", staff)]
    assert [attempt(slug, user) for slug, user in wrong] == [(False, NOT_FOUND)] * 4
    assert attempt("org-a", inbox.bea) == (False, FORBIDDEN)  # miembro sin `conversations.view`
    migrator.execute(SUSPEND_ORG, [inbox.a])
    assert attempt("org-a", inbox.ana) == (False, FORBIDDEN)


@pytest.mark.parametrize("frame", ["x" * 1025, b"binario"])
def test_the_client_can_only_ping_and_a_big_or_binary_frame_closes(
    inbox: SimpleNamespace, frame: str | bytes
) -> None:
    async def go() -> None:
        async with connected("org-a", inbox.vera) as vera:
            for hostile in HOSTILE:
                await vera.send_to(hostile)
            await vera.send_to(PING)
            assert await vera.frames(1) == [{"type": "pong"}]  # los hostiles no respondieron
            await push(inbox.a, "message.received", inbox.convs["ana"])  # no es de vera
            await push(inbox.a, "message.received", inbox.convs["vera"])
            assert seen(inbox, await vera.frames(1)) == {"vera"}
            await (vera.send_to(data=frame) if isinstance(frame, bytes) else vera.send_to(frame))
            assert await vera.closed_with() == 1009
            await push(inbox.a, "message.received", inbox.convs["vera"])
            assert await vera.receive_nothing(timeout=0.2)  # tras cerrar no se entrega nada

    run(go)


def test_idle_session_membership_and_permission_are_checked_while_connected(
    inbox: SimpleNamespace, migrator: DB, settings: Any
) -> None:
    settings.INBOX_WS_RECHECK_SECONDS, settings.INBOX_WS_IDLE_SECONDS = 0.1, 2.5
    settings.INBOX_WS_MAX_FRAMES = 1000

    async def go() -> None:
        async with connected("org-a", inbox.vera) as vera, connected("org-a", inbox.sam) as sam:
            async with connected("org-a", inbox.ana) as ana:
                migrator.execute(REVOKE, [inbox.sam.pk])  # sesión revocada (ADR-003 §6)
                assert await sam.closed_with() == NO_SESSION
                migrator.execute(LOSE_ROLE, [inbox.m["ana"]])  # sin esperar a un evento
                assert await ana.closed_with() == FORBIDDEN
                for _ in range(60):  # con `ping` sigue abierta más allá del plazo de inactividad
                    await vera.send_to(PING)
                    assert await vera.frames(1) == [{"type": "pong"}]
                    await asyncio.sleep(0.05)
                assert await vera.closed_with() == 4408 and not realtime._open

    run(go)


def test_with_the_channel_layer_down_the_socket_closes_without_logging_secrets(
    inbox: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, caplog: Any
) -> None:
    session = cookie(inbox.ana)

    async def down(*args: Any, **kwargs: Any) -> None:
        raise ConnectionError(f"redis://:{session.decode()}@host")

    async def go(method: str) -> Any:
        monkeypatch.setattr(get_channel_layer(), method, down)
        comm = Socket("org-a", session, ORIGIN, "")
        ok, code = await comm.connect()  # `receive` falla antes o después de aceptar
        closed = await comm.closed_with() if ok else code
        await comm.disconnect()
        return closed

    assert run(lambda: go("group_add")) == 1013
    channel_layers.backends.clear()
    assert run(lambda: go("receive")) == 1013
    assert not realtime._open and session.decode().split("=")[1] not in caplog.text
    assert str(inbox.ana.pk) not in caplog.text


def test_the_outbox_handler_is_subscribed_runs_in_its_organization_and_retries(
    inbox: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert all(tasks.publish_realtime in handlers_for(kind) for kind in realtime.EVENTS)
    with tenant_scope(ctx(inbox.b)) as tenant:
        theirs = emit(tenant, "message.received", aggregate_type="message", aggregate_id=uuid4(),
                      payload={"conversation_id": str(inbox.convs["b"])})  # fmt: skip
    publish = Mock()
    monkeypatch.setattr(realtime, "publish", publish)
    for org in (inbox.a, inbox.b):  # el evento de B no se ve desde A (RLS)
        kwargs = {"event_id": str(theirs), "organization_id": str(org)}
        assert tasks.publish_realtime.apply(kwargs=kwargs).successful()
    publish.assert_called_once_with(inbox.b, ANY)
    publish.reset_mock(side_effect=True)
    publish.side_effect = ConnectionError("sin Redis")
    assert tasks.publish_realtime.apply(kwargs=kwargs).successful()
    assert publish.call_count == tasks.REALTIME_RETRIES + 1  # reintenta y se descarta


def test_the_watcher_renews_the_group_and_a_failure_in_it_closes_the_socket(
    inbox: SimpleNamespace, settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    layer = {"BACKEND": "channels.layers.InMemoryChannelLayer", "CONFIG": {"group_expiry": 1}}
    settings.CHANNEL_LAYERS, settings.INBOX_WS_RECHECK_SECONDS = {"default": layer}, 0.2
    channel_layers.backends.clear()

    def blip(key: str | None) -> Any:
        raise RuntimeError("la BD parpadeó")

    async def go() -> None:
        async with connected("org-a", inbox.vera) as vera:
            await asyncio.sleep(1.6)  # más que el plazo del grupo, con varias renovaciones
            await push(inbox.a, "message.received", inbox.convs["vera"])
            assert seen(inbox, await vera.frames(1)) == {"vera"}
            monkeypatch.setattr("apps.conversations.realtime.user_for", blip)
            assert await vera.closed_with() == 1013 and not realtime._open  # antes: seguía abierta

    run(go)


@pytest.mark.parametrize("key", ["../XXXXXXXX", "a" * 31, "a" * 33, "A" * 32, "a" * 31 + "\n"])
def test_a_session_key_outside_the_django_format_never_touches_the_database(
    key: str, django_assert_num_queries: Any
) -> None:
    from apps.accounts.websocket import user_for

    with django_assert_num_queries(0):
        assert not user_for(key).is_authenticated


def test_a_close_frees_exactly_one_place_and_a_rejection_frees_none(
    inbox: SimpleNamespace, settings: Any
) -> None:
    settings.INBOX_WS_MAX_PER_USER = 2

    async def go() -> None:
        session = await sync_to_async(cookie)(inbox.ana)
        async with connected("org-a", inbox.ana), connected("org-a", inbox.ana):
            for _ in range(2):  # los rechazados no liberan nada: el siguiente también se rechaza
                assert await try_connect("org-a", session) == (False, TOO_MANY)
            assert sum(realtime._open.values()) == 2
            async with connected("org-a", inbox.vera):
                pass
            assert sum(realtime._open.values()) == 2  # y cerrar la de otra persona, tampoco
        assert not realtime._open

    async def one_of_two() -> None:
        async with connected("org-a", inbox.ana):
            async with connected("org-a", inbox.ana):
                pass
            assert sum(realtime._open.values()) == 1  # cerrar una libera exactamente una

    run(go)
    run(one_of_two)


def test_without_a_channel_layer_the_socket_does_not_open_and_nothing_is_published(
    inbox: SimpleNamespace, settings: Any
) -> None:
    settings.CHANNEL_LAYERS = {}
    channel_layers.backends.clear()
    assert attempt("org-a", inbox.ana) == (False, 1013) and not realtime.publish(inbox.a, {})


def test_the_events_use_the_types_and_fields_the_client_knows() -> None:
    cid, mid = uuid4(), uuid4()
    sent = realtime.outgoing("message.queued", mid, {"conversation_id": str(cid)}, "t")
    assert sent and sent["event"] == {"type": "message.updated", "message_id": str(mid),
                                      "conversation_id": str(cid), "at": "t"}  # fmt: skip
    mine = realtime.outgoing("conversation.assigned", cid, {"previous_user_id": str(mid)}, "t")
    assert mine and mine["event"] == {"type": "conversation.updated",
                                      "conversation_id": str(cid), "at": "t"}  # fmt: skip
    assert mine["previous_user_id"] == str(mid)  # solo para el consumer: no sale en el evento
    other = {"previous_user_id": "u", "conversation_id": str(cid)}  # solo lo lleva la reasignación
    queued = realtime.outgoing("message.queued", mid, other, "t")
    assert queued and "previous_user_id" not in queued


def test_a_delivery_change_and_a_status_change_leave_hints_with_only_ids(
    inbox: SimpleNamespace, migrator: DB
) -> None:
    with tenant_scope(ctx(inbox.a, inbox.ana)) as tenant:
        message, _ = commands.record_outbound(
            tenant, conversation_id=inbox.convs["ana"], client_message_id=uuid4(), body=SECRET
        )  # abre la conversación: `conversation.updated`
        for _ in range(2):  # el segundo, repetido: nada
            commands.advance_delivery(tenant, message_id=message.pk, status="SENT")
    rows = migrator.execute(
        "SELECT event_type, aggregate_id, payload FROM outbox_events "
        "WHERE event_type IN ('message.updated', 'conversation.updated') ORDER BY occurred_at, id"
    ).fetchall()
    convo = inbox.convs["ana"]
    assert rows == [("conversation.updated", convo, {}),
                    ("message.updated", message.pk, {"conversation_id": str(convo)})]  # fmt: skip


def test_who_lost_the_conversation_by_a_reassignment_is_told_once(inbox: SimpleNamespace) -> None:
    convo = inbox.convs["sam"]  # sam la tiene; vera no la ve

    async def go() -> None:
        async with connected("org-a", inbox.vera) as vera:
            await push(inbox.a, "conversation.assigned", convo, previous=inbox.vera.pk)
            await push(inbox.a, "conversation.assigned", convo, previous=inbox.ana.pk)
            await push(inbox.a, "message.received", inbox.convs["vera"])  # centinela
            got = await vera.frames(2)  # el aviso de vera y el centinela, no el de otra persona
            assert [f["type"] for f in got] == ["conversation.updated", "message.received"]

    run(go)


def test_a_connection_that_sends_too_many_frames_is_closed_and_the_window_resets(
    inbox: SimpleNamespace, settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings.INBOX_WS_MAX_FRAMES = 3
    monkeypatch.setattr(realtime, "RATE_WINDOW", 0.5)

    async def go() -> None:
        async with connected("org-a", inbox.vera) as vera:
            for _ in range(3):
                await vera.send_to(PING)
                assert await vera.frames(1) == [{"type": "pong"}]
            await asyncio.sleep(0.6)  # otra ventana: el contador vuelve a cero
            for _ in range(3):
                await vera.send_to(PING)
                assert await vera.frames(1) == [{"type": "pong"}]
            await vera.send_to(PING)  # la cuarta de la ventana
            assert await vera.closed_with() == 1008 and not realtime._open

    run(go)
