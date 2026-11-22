"""F8-08n (#1048; ADR-034 §8): la solicitud de atención humana de una conversación. La ruta
`…/conversations/{id}/human-request/` (pedirla y cerrarla), su cierre al responder o al asignar y el
traspaso que detecta el copiloto, con middleware, sesión, motor de autorización y PostgreSQL con
`crm_app`; la auditoría y el outbox, leídos con el rol dueño. Datos ficticios."""

import datetime as dt
import importlib
import logging
import pathlib
import re
import tomllib
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock
from uuid import UUID, uuid4

import psycopg
import pytest
from django.conf import settings
from django.db import IntegrityError, OperationalError, connection
from django.test import Client
from django.utils import timezone

from apps.access.selectors import AccessDenied, Denied
from apps.ai_agents import copilot
from apps.conversations import attention, commands
from apps.conversations.models import Conversation
from core.tenancy.scope import tenant_scope
from tests.ai_queries import captured, writes
from tests.conftest import migrator_conninfo
from tests.test_ai_copilot_context import say
from tests.test_ai_copilot_runtime import PLAIN, SHARED, desk, run
from tests.test_ai_gateway import world
from tests.test_conversations_api import BASE, UNAUTHENTICATED, crm, inbox, real_stack, stamp
from tests.test_conversations_assign_api import assign, duel
from tests.test_conversations_send_api import post
from tests.test_member_branch import DENIED
from tests.test_memberships import ctx
from tests.test_self_context import NOT_FOUND, reply, signed
from tests.test_team_writes import send

__all__ = ["crm", "desk", "inbox", "real_stack", "world"]  # los fixtures, para pytest
pytestmark = pytest.mark.usefixtures("tenant_db")
DB = psycopg.Connection[Any]
NAME = "conversations_human_request_ck"
MIGRATION = importlib.import_module("apps.conversations.migrations.0053_conversation_human_request")
VALIDATE = importlib.import_module(
    "apps.conversations.migrations.0054_validate_human_request_check"
)
HANDOFF = "Quiero hablar con una persona, por favor"
COPILOT = "/api/v1/o/org-a/conversations/{}/copilot/suggest/"


def ask(client: Client, convo: UUID, method: str = "post", base: str = BASE) -> Any:
    return send(client, method, f"{base}{convo}/human-request/", None)


def trail(migrator: DB) -> list[tuple[Any, ...]]:  # la auditoría de las solicitudes, en orden
    sql = "SELECT action, entity_id, changes, metadata FROM audit_logs WHERE action LIKE %s"
    return migrator.execute(sql + " ORDER BY occurred_at, id", ["conversation.human_%"]).fetchall()


def events(migrator: DB) -> list[tuple[Any, ...]]:
    sql = "SELECT event_type, aggregate_id, payload FROM outbox_events ORDER BY id"
    return migrator.execute(sql).fetchall()


def state(org: UUID, convo: UUID) -> tuple[Any, ...]:
    with tenant_scope(ctx(org)):
        row = Conversation.objects.get(pk=convo)
        return (row.human_request_reason, row.human_requested_by_user_id, row.human_requested_at)


def test_a_person_asks_once_and_the_list_and_the_inbox_learn_it(
    inbox: SimpleNamespace, migrator: DB
) -> None:
    convo, ana = inbox.convs["nobody"], signed(inbox.ana)
    body = ask(ana, convo).json()
    shown, fields = body["human_request"], {"reason", "requested_at", "requested_by_user_id"}
    assert body["id"] == str(convo) and set(shown) == fields
    assert (shown["reason"], shown["requested_by_user_id"]) == ("AGENT", str(inbox.ana.pk))
    again = ask(ana, convo)  # idempotente: ni otra fila, ni otra auditoría, ni otro evento
    assert again.status_code == 200 and again.json() == body
    asked = ("conversation.human_requested", convo, {"human_request_reason": [None, "AGENT"]})
    assert trail(migrator) == [(*asked, {"reason": "AGENT"})]
    assert events(migrator) == [("conversation.updated", convo, {})]  # nada en cola para enviar
    row = {r["id"]: r for r in ana.get(BASE).json()["results"]}[str(convo)]
    assert row["human_request"] == shown and row["assignee_kind"] == "UNASSIGNED"  # no la asigna
    assert ana.get(f"{BASE}{convo}/").json()["human_request"] == shown


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
    stamp(inbox.a, convo, status="CLOSED", closed_at=timezone.now())
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
    note = {"client_message_id": str(uuid4()), "body": "Nota"}
    assert send(ana, "post", f"{BASE}{convo}/notes/", note).status_code == 201  # una nota, no
    padded = f"  {settings.AI_HANDOFF_REPLY}\n"  # el texto fijo de traspaso no es atender
    assert post(ana, convo, body=padded).status_code == 202 and state(inbox.a, convo)[0] == "AGENT"
    sent = post(ana, convo)
    assert sent.status_code == 202, sent.content
    assert state(inbox.a, convo) == (None, None, None)
    closed = (
        "conversation.human_request_closed",
        {"cause": "REPLIED", "message_id": sent.json()["id"]},
    )
    assert trail(migrator)[-1][0::3] == closed
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
        with pytest.raises(IntegrityError, match=NAME):
            stamp(inbox.a, convo, **partial)
    wrong = {"human_requested_at": timezone.now(), "human_requested_by_user_id": inbox.ana.pk}
    for bad in ("BOT", None):  # un NULL pasaría el `IN` de un CHECK
        with pytest.raises(IntegrityError, match=NAME):
            stamp(inbox.a, convo, **wrong, human_request_reason=bad)
    for stranger in (inbox.luis.pk, uuid4()):  # de B, o nadie: la FK a las membresías de A
        outside = wrong | {"human_requested_by_user_id": stranger, "human_request_reason": "AGENT"}
        with pytest.raises(IntegrityError, match="conversations_human_requested_by_fk"):
            stamp(inbox.a, convo, **outside)
    valid = "SELECT convalidated FROM pg_constraint WHERE conname = %s"
    assert migrator.execute(valid, [NAME]).fetchone() == (True,)  # la `0054` lo validó
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
    """Con traspaso, además de `ai_*` y `audit_logs` (ADR-034 §2), solo las columnas `human_*` y su
    `conversation.updated` (decisión del propietario en #771, 2026-11-21)."""
    say(desk, HANDOFF)
    desk.fake.replies, ana = [PLAIN, PLAIN], signed(desk.ana)
    with captured() as seen:
        answer = send(ana, "post", COPILOT.format(desk.convo), {})
    assert answer.status_code == 201 and answer.json()["handoff"]["reason"] == "ASKED_FOR_PERSON"
    tables = "ai_runs|ai_actions|ai_suggestions|audit_logs|login_throttles"
    locks = rf"SELECT pg_advisory_xact_lock_shared\(|{SHARED}"  # F8-13c: y la conversación
    ai = rf'\s*((INSERT INTO|UPDATE) "?({tables})"? |{locks})'
    left = [s for s in writes(seen) if not re.match(ai, s)]
    columns = ", ".join(f'"{c}" = [^,]+' for c in (*attention.FIELDS, "updated_at"))
    allowed = (
        rf'UPDATE "conversations" SET {columns} WHERE \(.*"conversations"."id" = \S+\)$',  # noqa: S608
        r'SELECT .* FROM "conversations" WHERE .* FOR NO KEY UPDATE$',
        r"INSERT INTO \"outbox_events\" .*'conversation\.updated'",
    )
    matched = sorted(i for s in left for i, a in enumerate(allowed) if re.match(a, s, re.S))
    assert matched == [0, 1, 2] and len(left) == 3, left
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
    assert [e[0] for e in events(migrator)] == ["conversation.updated"]


def test_the_copilot_records_nothing_when_closed_or_answered_meanwhile_and_lost_is_not_found(
    desk: SimpleNamespace, migrator: DB, monkeypatch: pytest.MonkeyPatch, caplog: Any
) -> None:
    say(desk, HANDOFF)
    stamp(desk.a, desk.convo, status="CLOSED", closed_at=timezone.now())
    assert run(desk, PLAIN).handoff_reason == "ASKED_FOR_PERSON"  # el aviso sale igual, sin error
    assert "ai.copilot_handoff_unsaved" not in str([r.__dict__ for r in caplog.records])
    stamp(desk.a, desk.convo, status="OPEN", closed_at=None)
    prepare = copilot._prepare

    def answered(*args: Any) -> Any:  # una persona responde mientras el modelo trabaja
        found = prepare(*args)
        say(desk, "Ya te atiendo", direction="OUTBOUND", sender_type="USER",
            sender_user_id=desk.ana.pk, delivery_status="SENT")  # fmt: skip
        return found

    monkeypatch.setattr(copilot, "_prepare", answered)
    assert run(desk, PLAIN).handoff_reason == "ASKED_FOR_PERSON"
    assert state(desk.a, desk.convo) == (None, None, None) and trail(migrator) == []
    monkeypatch.setattr(copilot, "_prepare", prepare)
    say(desk, HANDOFF)
    for lost in (Conversation.DoesNotExist(), AccessDenied(Denied.PERMISSION)):  # a mitad
        monkeypatch.setattr(attention, "request_human", Mock(side_effect=lost))
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
    ago = timezone.now() - dt.timedelta(hours=100)  # resuelta hace más de 72 h: el próximo
    stamp(inbox.a, convo, status="RESOLVED", resolved_at=ago)  # mensaje la cierra
    with tenant_scope(ctx(inbox.a, inbox.ana)) as tenant:
        old = Conversation.objects.get(pk=convo)
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
    summary = COPILOT.replace("suggest", "summary").format(desk.convo)
    assert send(signed(desk.ana), "post", summary, {}).status_code == 201
    run = migrator.execute("SELECT id FROM ai_runs WHERE purpose = 'SUMMARY'").fetchone()
    assert run is not None and state(desk.a, desk.convo)[:2] == ("ASKED_FOR_PERSON", desk.ana.pk)
    assert trail(migrator)[0][3] == {"reason": "ASKED_FOR_PERSON", "ai_run_id": str(run[0])}


def test_two_requests_at_once_record_one(inbox: SimpleNamespace, migrator: DB) -> None:
    convo = inbox.convs["nobody"]

    def request(tenant: Any) -> bool:
        return attention.request_human(tenant, conversation_id=convo, reason="AGENT")[1]

    assert duel(inbox.a, (inbox.ana, request), (inbox.sam, request)) == [True, False]
    assert len(trail(migrator)) == 1


def test_validating_waits_at_most_its_lock_timeout_and_leaves_none(migrator: DB) -> None:
    migrator.execute("SET statement_timeout = '20s'")  # sin `lock_timeout`, fallaría por esto
    with psycopg.connect(migrator_conninfo()) as other:
        other.execute("LOCK TABLE conversations IN SHARE UPDATE EXCLUSIVE MODE")
        with pytest.raises(psycopg.errors.LockNotAvailable):
            VALIDATE.validate(None, SimpleNamespace(connection=migrator))
    assert migrator.execute("SHOW lock_timeout").fetchone() == ("0",)


@pytest.mark.usefixtures("real_stack")
def test_a_database_fault_while_recording_keeps_the_draft_and_the_notice(
    desk: SimpleNamespace, migrator: DB, monkeypatch: pytest.MonkeyPatch, caplog: Any
) -> None:
    say(desk, HANDOFF)
    desk.fake.replies = [PLAIN]
    monkeypatch.setattr(
        attention, "request_human", Mock(side_effect=OperationalError("lock timeout fila-x"))
    )
    with caplog.at_level(logging.DEBUG):
        answer = send(signed(desk.ana), "post", COPILOT.format(desk.convo), {})
    assert answer.status_code == 201, answer.content
    assert answer.json()["handoff"] == {"requested": True, "reason": "ASKED_FOR_PERSON"}
    assert migrator.execute("SELECT count(*) FROM ai_suggestions").fetchone() == (1,)
    logged = caplog.text + str([r.__dict__ for r in caplog.records])
    assert "ai.copilot_handoff_unsaved" in logged and "OperationalError" in logged
    assert "fila-x" not in logged
