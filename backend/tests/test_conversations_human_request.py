"""F8-08n (#1048; ADR-034 §8): la solicitud de atención humana de una conversación. La ruta
`…/conversations/{id}/human-request/` (pedirla y cerrarla), su cierre al responder o al asignar y el
traspaso que detecta el copiloto, con middleware, sesión, motor de autorización y PostgreSQL con
`crm_app`; la auditoría y el outbox, leídos con el rol dueño. Datos ficticios."""

import datetime as dt
import importlib
import pathlib
import tomllib
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from django.db import IntegrityError, connection
from django.test import Client
from django.utils import timezone

from apps.access.selectors import AccessDenied
from apps.ai_agents import copilot
from apps.conversations import attention, commands
from apps.conversations.models import Conversation
from core.tenancy.scope import tenant_scope
from tests.test_ai_copilot_context import say
from tests.test_ai_copilot_runtime import PLAIN, desk, run
from tests.test_ai_gateway import world
from tests.test_conversations_api import BASE, UNAUTHENTICATED, crm, inbox, real_stack
from tests.test_conversations_assign_api import assign
from tests.test_conversations_send_api import post
from tests.test_member_branch import DENIED
from tests.test_memberships import ctx
from tests.test_self_context import NOT_FOUND, reply, signed
from tests.test_team_writes import send

__all__ = ["crm", "desk", "inbox", "real_stack", "world"]  # los fixtures, para pytest
pytestmark = pytest.mark.usefixtures("tenant_db")
DB = psycopg.Connection[Any]
MIGRATION = importlib.import_module("apps.conversations.migrations.0053_conversation_human_request")
COPILOT = "/api/v1/o/org-a/conversations/{}/copilot/suggest/"


def ask(client: Client, convo: UUID, method: str = "post", base: str = BASE) -> Any:
    return send(client, method, f"{base}{convo}/human-request/", None)


def trail(migrator: DB) -> list[tuple[Any, ...]]:
    """La auditoría de las solicitudes y los eventos para el Inbox, en orden."""
    audited = migrator.execute(
        "SELECT action, entity_id, changes, metadata FROM audit_logs "
        "WHERE action LIKE 'conversation.human_%%' ORDER BY occurred_at, id"
    ).fetchall()
    return list(audited)


def events(migrator: DB) -> list[tuple[Any, ...]]:
    found = migrator.execute(
        "SELECT event_type, aggregate_id, payload FROM outbox_events ORDER BY id"
    ).fetchall()
    return list(found)


def state(org: UUID, convo: UUID) -> tuple[Any, ...]:
    with tenant_scope(ctx(org)):
        row = Conversation.objects.get(pk=convo)
        return (row.human_request_reason, row.human_requested_by_user_id, row.human_requested_at)


def test_a_person_asks_once_and_the_list_and_the_inbox_learn_it(
    inbox: SimpleNamespace, migrator: DB
) -> None:
    convo, ana = inbox.convs["nobody"], signed(inbox.ana)
    messages = migrator.execute("SELECT count(*) FROM messages").fetchone()
    first = ask(ana, convo)
    assert first.status_code == 200, first.content
    body = first.json()
    assert body["id"] == str(convo) and set(body["human_request"]) == {
        "reason", "requested_at", "requested_by_user_id",
    }  # fmt: skip
    shown = body["human_request"]
    assert (shown["reason"], shown["requested_by_user_id"]) == ("AGENT", str(inbox.ana.pk))
    again = ask(ana, convo)  # idempotente: ni otra fila, ni otra auditoría, ni otro evento
    assert again.status_code == 200 and again.json() == body
    assert trail(migrator) == [
        ("conversation.human_requested", convo, {"human_request_reason": [None, "AGENT"]},
         {"reason": "AGENT"}),
    ]  # fmt: skip
    assert events(migrator) == [("conversation.updated", convo, {})]  # nada en cola para enviar
    assert migrator.execute("SELECT count(*) FROM messages").fetchone() == messages
    row = {r["id"]: r for r in ana.get(BASE).json()["results"]}[str(convo)]
    assert row["human_request"] == shown and row["assignee_kind"] == "UNASSIGNED"  # no la asigna
    assert ana.get(f"{BASE}{convo}/").json()["human_request"] == shown
    others = [r["human_request"] for r in ana.get(BASE).json()["results"] if r["id"] != str(convo)]
    assert others and set(map(str, others)) == {"None"}


def test_closing_by_hand_clears_it_once_and_a_closed_conversation_takes_no_request(
    inbox: SimpleNamespace, migrator: DB
) -> None:
    convo, ana = inbox.convs["nobody"], signed(inbox.ana)
    nothing = ask(ana, convo, "delete")  # sin solicitud: nada cambia, nada se audita
    assert nothing.status_code == 200 and nothing.json()["human_request"] is None
    assert trail(migrator) == [] and events(migrator) == []
    assert ask(ana, convo).status_code == 200
    closed = ask(ana, convo, "delete")
    assert closed.status_code == 200 and closed.json() == {"id": str(convo), "human_request": None}
    assert state(inbox.a, convo) == (None, None, None)
    assert ask(ana, convo, "delete").json()["human_request"] is None
    assert [a[0] for a in trail(migrator)] == [
        "conversation.human_requested", "conversation.human_request_closed",
    ]  # fmt: skip
    assert trail(migrator)[1][2:] == (
        {"human_request_reason": ["AGENT", None]},
        {"cause": "MANUAL"},
    )
    assert [e[0] for e in events(migrator)] == ["conversation.updated"] * 2
    with tenant_scope(ctx(inbox.a)):
        Conversation.objects.filter(pk=convo).update(status="CLOSED", closed_at=timezone.now())
    refused = ask(ana, convo)
    assert (refused.status_code, refused.json()["code"]) == (409, "CONVERSATION_CLOSED")
    assert state(inbox.a, convo) == (None, None, None)


def test_the_permission_the_scope_and_the_organization_come_from_the_session(
    inbox: SimpleNamespace, migrator: DB
) -> None:
    sam = inbox.convs["sam"]  # de sam: vera (Vendedora, `OWN`) no la ve
    for method in ("post", "delete"):
        assert reply(ask(signed(inbox.vera), sam, method)) == NOT_FOUND
        assert reply(ask(signed(inbox.vera), uuid4(), method)) == NOT_FOUND  # igual que inexistente
        assert reply(ask(signed(inbox.bea), sam, method)) == DENIED  # sin rol
        assert reply(ask(Client(), sam, method)) == UNAUTHENTICATED
        assert reply(ask(signed(inbox.luis), sam, method)) == NOT_FOUND  # de B, no de A
        b = BASE.replace("org-a", "org-b")  # la de B por la ruta de A, y la de A por la de B
        assert reply(ask(signed(inbox.ana), inbox.convs["b"], method)) == NOT_FOUND
        assert reply(ask(signed(inbox.ana), sam, method, base=b)) == NOT_FOUND
    assert trail(migrator) == [] and events(migrator) == []
    vera = ask(signed(inbox.vera), inbox.convs["vera"])  # la suya, sí
    assert vera.json()["human_request"]["requested_by_user_id"] == str(inbox.vera.pk)
    team = ask(signed(inbox.sam), inbox.convs["team"])  # Supervisor: las de su equipo
    assert team.status_code == 200, team.content
    with tenant_scope(ctx(inbox.b)):  # bajo la RLS de B, las de A no existen
        assert not Conversation.objects.filter(human_requested_at__isnull=False).exists()
    with tenant_scope(ctx(inbox.a, inbox.ana)) as tenant:
        with pytest.raises(ValueError, match="motivo"):
            attention.request_human(tenant, conversation_id=sam, reason="SOMEONE")
        with pytest.raises(ValueError, match="identificador"):
            attention.request_human(tenant, conversation_id=str(sam), reason="AGENT")  # type: ignore[arg-type]
    free = inbox.convs["nobody"]
    with tenant_scope(ctx(inbox.a, inbox.bea)) as tenant:  # el comando también mira el permiso
        with pytest.raises(AccessDenied):
            attention.request_human(tenant, conversation_id=free, reason="AGENT")
        with pytest.raises(AccessDenied):
            attention.close_request(tenant, conversation_id=free)


def test_a_reply_from_a_person_or_taking_it_closes_the_request_and_releasing_does_not(
    inbox: SimpleNamespace, migrator: DB
) -> None:
    convo, ana = inbox.convs["nobody"], signed(inbox.ana)
    ask(ana, convo)
    note = send(
        ana, "post", f"{BASE}{convo}/notes/", {"client_message_id": str(uuid4()), "body": "Nota"}
    )
    assert note.status_code in (200, 201) and state(inbox.a, convo)[0] == "AGENT"  # una nota, no
    sent = post(ana, convo)
    assert sent.status_code == 202, sent.content
    assert state(inbox.a, convo) == (None, None, None)
    closed = trail(migrator)[-1]
    assert closed[0] == "conversation.human_request_closed"
    assert closed[3] == {"cause": "REPLIED", "message_id": sent.json()["id"]}
    other = inbox.convs["team"]  # sin persona: tomarla también la cierra
    ask(ana, other)
    assert assign(ana, other, action="TAKE").status_code == 200
    assert state(inbox.a, other) == (None, None, None)
    assert trail(migrator)[-1][3] == {"cause": "ASSIGNED"}
    ask(ana, other)  # suya; soltarla no es que una persona la atienda
    assert assign(ana, other, action="RELEASE").status_code == 200
    assert state(inbox.a, other)[0] == "AGENT"
    assert assign(ana, other, action="TRANSFER", user_id=str(inbox.sam.pk)).status_code == 200
    assert state(inbox.a, other) == (None, None, None)  # pasársela a otra persona, sí


def test_the_database_keeps_the_request_whole_and_going_back_refuses_an_open_one(
    inbox: SimpleNamespace, migrator: DB
) -> None:
    convo = inbox.convs["nobody"]
    for partial in ({"human_request_reason": "AGENT"}, {"human_requested_at": timezone.now()}):
        with pytest.raises(IntegrityError, match="conversations_human_request_ck"):
            with tenant_scope(ctx(inbox.a)):
                Conversation.objects.filter(pk=convo).update(**partial)
    wrong = {"human_requested_at": timezone.now(), "human_requested_by_user_id": inbox.ana.pk}
    with pytest.raises(IntegrityError, match="conversations_human_request_ck"):
        with tenant_scope(ctx(inbox.a)):
            Conversation.objects.filter(pk=convo).update(**wrong, human_request_reason="BOT")
    for stranger in (inbox.luis.pk, uuid4()):  # de B, o nadie: la FK a las membresías de A
        outside = wrong | {"human_requested_by_user_id": stranger, "human_request_reason": "AGENT"}
        with pytest.raises(IntegrityError, match="conversations_human_requested_by_fk"):
            with tenant_scope(ctx(inbox.a)):
                Conversation.objects.filter(pk=convo).update(**outside)
    valid = migrator.execute(
        "SELECT convalidated FROM pg_constraint WHERE conname = 'conversations_human_request_ck'"
    ).fetchone()
    assert valid == (True,)  # la `0054` lo validó
    with pytest.raises(RuntimeError, match="BYPASSRLS"):  # `crm_app` no ve todas las filas
        MIGRATION.refuse_open(None, SimpleNamespace(connection=connection))
    editor = SimpleNamespace(connection=migrator)
    MIGRATION.refuse_open(None, editor)  # sin solicitudes abiertas, vuelve atrás
    ask(signed(inbox.ana), convo)
    with pytest.raises(RuntimeError, match="1 conversations wait for a person"):
        MIGRATION.refuse_open(None, editor)


@pytest.mark.usefixtures("real_stack")
def test_a_handoff_the_copilot_detects_is_recorded_once_without_sending_anything(
    desk: SimpleNamespace, migrator: DB
) -> None:
    say(desk, "Quiero hablar con una persona, por favor")
    desk.fake.replies = [PLAIN, PLAIN]
    ana = signed(desk.ana)
    answer = send(ana, "post", COPILOT.format(desk.convo), {})
    assert answer.status_code == 201, answer.content
    assert answer.json()["handoff"]["reason"] == "ASKED_FOR_PERSON"
    run = migrator.execute("SELECT id FROM ai_runs").fetchone()
    assert run is not None
    assert state(desk.a, desk.convo)[:2] == ("ASKED_FOR_PERSON", desk.ana.pk)
    assert trail(migrator) == [
        ("conversation.human_requested", desk.convo,
         {"human_request_reason": [None, "ASKED_FOR_PERSON"]},
         {"reason": "ASKED_FOR_PERSON", "ai_run_id": str(run[0])}),
    ]  # fmt: skip
    assert send(ana, "post", COPILOT.format(desk.convo), {}).status_code == 201  # otra vez
    assert len(trail(migrator)) == 1  # la misma solicitud, la primera
    queued = "SELECT count(*) FROM outbox_events WHERE event_type = 'message.queued'"
    assert migrator.execute(queued).fetchone() == (0,)
    assert [e[0] for e in events(migrator)] == ["conversation.updated"]


def test_the_copilot_on_a_closed_conversation_records_nothing_and_a_lost_one_is_not_found(
    desk: SimpleNamespace, migrator: DB, monkeypatch: pytest.MonkeyPatch
) -> None:
    say(desk, "Quiero hablar con una persona, por favor")
    with tenant_scope(ctx(desk.a)):
        Conversation.objects.filter(pk=desk.convo).update(status="CLOSED", closed_at=timezone.now())
    assert run(desk, PLAIN).handoff_reason == "ASKED_FOR_PERSON"  # el aviso sale igual
    assert state(desk.a, desk.convo) == (None, None, None) and trail(migrator) == []

    def gone(*_: Any, **__: Any) -> None:  # perdió el alcance mientras el modelo respondía
        raise Conversation.DoesNotExist

    monkeypatch.setattr(attention, "request_human", gone)
    with pytest.raises(copilot.NotFound):
        run(desk, PLAIN)


def test_only_the_api_the_commands_and_the_copilot_runtime_import_the_request() -> None:
    config = tomllib.loads((pathlib.Path(__file__).parents[1] / "pyproject.toml").read_text())
    contracts = config["tool"]["importlinter"]["contracts"]
    [guard] = [c for c in contracts if c.get("protected_modules") == [attention.__name__]]
    allowed = ["apps.conversations.api", "apps.conversations.commands", "apps.ai_agents.copilot"]
    assert (guard["type"], guard["allowed_importers"]) == ("protected", allowed)


def test_resolving_or_closing_the_conversation_closes_the_request(
    inbox: SimpleNamespace, migrator: DB
) -> None:
    convo, ana = inbox.convs["nobody"], signed(inbox.ana)
    ask(ana, convo)
    with tenant_scope(ctx(inbox.a)):  # resuelta hace más de 72 h: el próximo mensaje la cierra
        Conversation.objects.filter(pk=convo).update(
            status="RESOLVED", resolved_at=timezone.now() - dt.timedelta(hours=100)
        )
        old = Conversation.objects.get(pk=convo)
    with tenant_scope(ctx(inbox.a, inbox.ana)) as tenant:
        message, _ = commands.record_inbound(
            tenant, contact_id=old.contact_id, channel_account_id=old.channel_account_id,
            external_message_id="wamid.hr1", body="Hola de nuevo",
        )  # fmt: skip
    assert message.conversation_id != convo and state(inbox.a, convo) == (None, None, None)
    assert trail(migrator)[-1][0::3] == ("conversation.human_request_closed", {"cause": "CLOSED"})
    other = inbox.convs["team"]
    ask(ana, other)
    with tenant_scope(ctx(inbox.a, inbox.ana)) as tenant:  # ningún comando resuelve aún: el paso
        found = Conversation.objects.select_for_update().get(pk=other)
        commands._status(tenant, "default", found, "RESOLVED", "conversation.resolved",
                         resolved_at=timezone.now())  # fmt: skip
    assert state(inbox.a, other) == (None, None, None)
    assert trail(migrator)[-1][3] == {"cause": "CLOSED"}


@pytest.mark.usefixtures("real_stack")
def test_a_handoff_found_while_summarizing_is_recorded_too(
    desk: SimpleNamespace, migrator: DB
) -> None:
    say(desk, "quiero hablar con una persona")
    desk.fake.replies = [[{"type": "text", "text": "Pide hablar con una persona."}]]
    summary = send(
        signed(desk.ana), "post", COPILOT.replace("suggest", "summary").format(desk.convo), {}
    )
    assert summary.status_code == 201, summary.content
    run = migrator.execute("SELECT id FROM ai_runs WHERE purpose = 'SUMMARY'").fetchone()
    assert run is not None and state(desk.a, desk.convo)[:2] == ("ASKED_FOR_PERSON", desk.ana.pk)
    assert trail(migrator)[0][3] == {"reason": "ASKED_FOR_PERSON", "ai_run_id": str(run[0])}
