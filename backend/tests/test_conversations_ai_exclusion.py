"""F8-13 (#1071; ADR-034 §5, §10, ADR-011): excluir una conversación de la IA y volver a incluirla
(`PUT …/conversations/{id}/ai-exclusion/`), con middleware, sesión, motor de autorización y
PostgreSQL con `crm_app`; la auditoría y el outbox, leídos con el rol dueño. Datos ficticios."""

import importlib
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from django.db import IntegrityError, connection
from django.test import Client
from django.utils import timezone

from apps.access.selectors import AccessDenied
from apps.conversations import attention
from apps.conversations.models import Conversation
from core.tenancy.scope import tenant_scope
from tests.test_channel_accounts_api import signed as stepped
from tests.test_conversations_api import BASE, UNAUTHENTICATED, crm, inbox, real_stack
from tests.test_conversations_assign_api import duel
from tests.test_member_branch import DENIED
from tests.test_memberships import ctx
from tests.test_self_context import NOT_FOUND, reply, signed
from tests.test_team_writes import send

__all__ = ["crm", "inbox", "real_stack"]  # los fixtures, para pytest
pytestmark = pytest.mark.usefixtures("tenant_db")
DB = psycopg.Connection[Any]
NAME = "conversations_ai_excluded_ck"
MIGRATION = importlib.import_module("apps.conversations.migrations.0055_conversation_ai_exclusion")
STALE = "CONVERSATION_VERSION_CONFLICT"


def put(client: Client, convo: UUID, excluded: bool, version: Any = 1, base: str = BASE) -> Any:
    body = {"excluded": excluded, "version": version}
    return send(client, "put", f"{base}{convo}/ai-exclusion/", body)


def trail(migrator: DB) -> list[tuple[Any, ...]]:
    sql = "SELECT action, entity_id, changes, metadata FROM audit_logs WHERE action LIKE %s"
    return migrator.execute(sql + " ORDER BY occurred_at, id", ["conversation.ai_%"]).fetchall()


def flag(org: UUID, convo: UUID) -> tuple[Any, ...]:
    with tenant_scope(ctx(org)):
        row = Conversation.objects.get(pk=convo)
        return (row.ai_excluded_at is not None, row.ai_excluded_by_user_id, row.version)


def test_whoever_sees_it_excludes_it_once_and_the_inbox_learns_it(
    inbox: SimpleNamespace, migrator: DB
) -> None:
    convo, vera = inbox.convs["vera"], signed(inbox.vera)  # Vendedora, sin `ai.use` ni `ai.manage`
    done = put(vera, convo, True)
    assert done.status_code == 200, done.content
    body = done.json()
    assert set(body) == {"id", "ai_excluded_at", "version"} and body["version"] == 2
    assert flag(inbox.a, convo) == (True, inbox.vera.pk, 2)
    again = put(vera, convo, True, 2)  # la misma marca: nada cambia ni se audita
    assert again.status_code == 200 and again.json() == body
    assert trail(migrator) == [
        ("conversation.ai_excluded", convo, {"ai_excluded": [False, True]}, {})
    ]  # solo ids: ni texto ni quién más allá del actor de la fila
    events = "SELECT event_type, aggregate_id, payload FROM outbox_events ORDER BY id"
    assert migrator.execute(events).fetchall() == [("conversation.updated", convo, {})]
    row = {r["id"]: r for r in vera.get(BASE).json()["results"]}[str(convo)]
    assert row["ai_excluded_at"] == body["ai_excluded_at"] is not None
    detail = vera.get(f"{BASE}{convo}/").json()
    assert detail["ai_excluded_at"] == body[
        "ai_excluded_at"
    ] and "ai_excluded_by_user_id" not in str(detail)


def test_including_it_again_needs_ai_manage_and_then_a_recent_second_factor(
    inbox: SimpleNamespace, migrator: DB
) -> None:
    convo = inbox.convs["vera"]
    assert put(signed(inbox.vera), convo, True).status_code == 200
    assert reply(put(signed(inbox.vera), convo, False, 2)) == DENIED  # sin `ai.manage`
    late = put(signed(inbox.ana), convo, False, 2)  # Owner, sin segundo factor reciente
    assert (late.status_code, late.json()["code"]) == (403, "STEP_UP_REQUIRED")
    assert flag(inbox.a, convo) == (True, inbox.vera.pk, 2)
    done = put(stepped(inbox.ana), convo, False, 2)
    assert done.status_code == 200, done.content
    assert done.json()["ai_excluded_at"] is None and flag(inbox.a, convo) == (False, None, 3)
    assert [t[0] for t in trail(migrator)] == [
        "conversation.ai_excluded",
        "conversation.ai_included",
    ]
    assert trail(migrator)[1][2] == {"ai_excluded": [True, False]}
    with tenant_scope(ctx(inbox.a, inbox.vera)) as tenant:  # el comando también lo mira
        with pytest.raises(AccessDenied):
            attention.set_ai_excluded(tenant, conversation_id=convo, excluded=False, version=3)


def test_the_scope_the_organization_and_the_version_come_before_any_change(
    inbox: SimpleNamespace, migrator: DB
) -> None:
    sam, b = inbox.convs["sam"], BASE.replace("org-a", "org-b")
    for excluded in (True, False):
        who = stepped(inbox.vera) if excluded else stepped(inbox.ana)
        assert reply(put(stepped(inbox.vera), sam, True)) == NOT_FOUND  # fuera de su alcance
        assert reply(put(who, uuid4(), excluded)) == NOT_FOUND  # igual que inexistente
        assert reply(put(who, inbox.convs["b"], excluded)) == NOT_FOUND  # de B por la ruta de A
        assert reply(put(stepped(inbox.ana), sam, excluded, base=b)) == NOT_FOUND
        assert reply(put(signed(inbox.luis), sam, excluded)) == NOT_FOUND  # de B, no de A
        assert reply(put(signed(inbox.bea), sam, excluded)) == DENIED  # sin rol
        assert reply(put(Client(), sam, excluded)) == UNAUTHENTICATED
    stale = put(signed(inbox.ana), sam, True, 7)
    assert (stale.status_code, stale.json()["code"]) == (409, STALE)
    for bad in (
        {"excluded": "yes", "version": 1},
        {"excluded": True},
        {"version": 1},
        {"excluded": True, "version": 0},
        {"excluded": True, "version": 1, "x": 1},
    ):
        assert send(signed(inbox.ana), "put", f"{BASE}{sam}/ai-exclusion/", bad).status_code == 400
    assert trail(migrator) == [] and flag(inbox.a, sam) == (False, None, 1)
    with tenant_scope(ctx(inbox.a, inbox.ana)) as tenant:
        for wrong in ({"excluded": 1, "version": 1}, {"excluded": True, "version": True}):
            with pytest.raises(ValueError, match="exclusión"):
                attention.set_ai_excluded(tenant, conversation_id=sam, **wrong)


def test_two_people_at_once_with_the_same_version_change_it_once(
    inbox: SimpleNamespace, migrator: DB
) -> None:
    convo = inbox.convs["nobody"]

    def exclude(tenant: Any) -> bool:
        return attention.set_ai_excluded(tenant, conversation_id=convo, excluded=True, version=1)[1]

    assert duel(inbox.a, (inbox.ana, exclude), (inbox.vera, exclude)) == [True, "VersionConflict"]
    assert len(trail(migrator)) == 1 and flag(inbox.a, convo) == (True, inbox.ana.pk, 2)


def test_the_database_keeps_the_mark_whole_within_its_organization(
    inbox: SimpleNamespace, migrator: DB
) -> None:
    convo, now = inbox.convs["nobody"], timezone.now()
    raw = "UPDATE conversations SET ai_excluded_at = %s, ai_excluded_by_user_id = %s WHERE id = %s"
    for partial in ((now, None), (None, inbox.ana.pk)):  # SQL directo como `crm_app`
        with tenant_scope(ctx(inbox.a)), connection.cursor() as cursor:
            with pytest.raises(IntegrityError, match=NAME):
                cursor.execute(raw, [*partial, convo])
    for stranger in (inbox.luis.pk, uuid4()):  # de B, o nadie: la FK a las membresías de A
        with tenant_scope(ctx(inbox.a)), connection.cursor() as cursor:
            with pytest.raises(IntegrityError, match="conversations_ai_excluded_by_fk"):
                cursor.execute(raw, [now, stranger, convo])
    with tenant_scope(ctx(inbox.b)), connection.cursor() as cursor:  # bajo la RLS de B
        cursor.execute(raw, [now, inbox.luis.pk, convo])
        assert cursor.rowcount == 0
    assert flag(inbox.a, convo) == (False, None, 1)
    valid = "SELECT convalidated FROM pg_constraint WHERE conname = %s"
    assert migrator.execute(valid, [NAME]).fetchone() == (True,)  # la `0056` lo validó
    with pytest.raises(RuntimeError, match="BYPASSRLS"):
        MIGRATION.refuse_excluded(None, SimpleNamespace(connection=connection))
    editor = SimpleNamespace(connection=migrator)
    MIGRATION.refuse_excluded(None, editor)  # sin excluidas, vuelve atrás
    put(signed(inbox.ana), convo, True)
    with pytest.raises(RuntimeError, match="1 conversations are excluded from AI"):
        MIGRATION.refuse_excluded(None, editor)
