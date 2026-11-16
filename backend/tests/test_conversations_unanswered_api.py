"""F6-09: «Sin responder» en `…/conversations/?unanswered=true` y su número en
`…/conversations/unanswered/`. Middleware, sesión, autorización y PostgreSQL con `crm_app`."""

from importlib import import_module
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from django.core.management import call_command
from django.test import Client
from django.utils import timezone

from apps.conversations import commands
from apps.conversations.models import Conversation
from core.tenancy.scope import tenant_scope
from tests import test_conversation_scopes as scopes
from tests.test_conversation_commands import send
from tests.test_conversations_api import BASE, UNAUTHENTICATED, ids, names, stamp
from tests.test_member_branch import DENIED
from tests.test_memberships import ctx
from tests.test_pipelines_tables import as_migrator
from tests.test_self_context import NOT_FOUND, reply, signed

crm, inbox, real_stack = scopes.crm, scopes.inbox, scopes.real_stack
pytestmark = pytest.mark.usefixtures("tenant_db")
COUNT = BASE + "unanswered/"
INDEX = import_module("apps.conversations.migrations.0037_conversations_unanswered_index")


def said(inbox: SimpleNamespace, key: str, direction: str, **fields: Any) -> None:
    org = inbox.b if key == "b" else inbox.a
    stamp(org, inbox.convs[key], last_message_preview="Hola", last_message_direction=direction,
          **fields)  # fmt: skip


def count(client: Client, **query: Any) -> int:
    response = client.get(COUNT, query)
    assert response.status_code == 200, response.content
    assert set(response.json()) == {"count"}
    return response.json()["count"]  # type: ignore[no-any-return]


@pytest.fixture
def waiting(inbox: SimpleNamespace) -> SimpleNamespace:
    """Esperan respuesta: vera, team, nobody (abiertas, lo último del cliente) y la de B. No: sam
    (lo último es nuestro), ana (resuelta), both (cerrada) y ninguna sin mensajes."""
    for key in ("vera", "team", "nobody", "b"):
        said(inbox, key, "INBOUND")
    said(inbox, "sam", "OUTBOUND")
    said(inbox, "ana", "INBOUND", status="RESOLVED", resolved_at=timezone.now())
    said(inbox, "both", "INBOUND", status="CLOSED", closed_at=timezone.now())
    return inbox


def test_the_filter_and_the_count_follow_the_scope_of_each_role(waiting: SimpleNamespace) -> None:
    ana, vera, sam = (signed(getattr(waiting, k)) for k in ("ana", "vera", "sam"))
    cases = (
        (ana, {"vera", "team", "nobody"}),
        (vera, {"vera", "nobody"}),
        (sam, {"team", "nobody"}),
    )
    for client, wanted in cases:
        assert names(waiting, ids(client, unanswered="true")) == wanted
        assert count(client) == len(wanted)
        assert len(ids(client, unanswered="false")) == len(ids(client))  # `false`: sin filtro
    assert names(waiting, ids(ana, unanswered="true", box="unassigned")) == {"nobody"}
    assert count(ana, box="unassigned") == 1 and count(ana, box="mine") == 0
    assert count(vera, box="mine") == 1 and count(ana, status="RESOLVED") == 3  # solo `box`
    assert names(waiting, ids(ana, unanswered="true", status="PENDING")) == {
        "vera", "team", "nobody",
    }  # fmt: skip


def test_the_count_refuses_like_the_list_and_never_counts_another_organization(
    waiting: SimpleNamespace,
) -> None:
    assert reply(signed(waiting.bea).get(COUNT)) == DENIED  # sin rol
    assert reply(Client().get(COUNT)) == UNAUTHENTICATED
    assert reply(signed(waiting.luis).get(COUNT)) == NOT_FOUND  # de B, no de A
    other = COUNT.replace("org-a", "org-b")
    assert reply(signed(waiting.ana).get(other)) == NOT_FOUND
    ana = signed(waiting.ana)
    for url, query in (
        (BASE, {"unanswered": "maybe"}),
        (BASE, {"box": "x"}),
        (COUNT, {"box": "x"}),
    ):
        response = ana.get(url, query)
        assert response.status_code == 400 and response.json()["code"] == "VALIDATION_ERROR"
    twice = ana.get(BASE + "?unanswered=true&unanswered=false")
    assert twice.status_code == 400 and set(twice.json()["fields"]) == {"unanswered"}


def test_a_reply_ends_the_wait_and_a_note_does_not(inbox: SimpleNamespace) -> None:
    ana, convo = signed(inbox.ana), inbox.convs["vera"]
    assert count(ana) == 0  # sin mensajes no espera nada
    with tenant_scope(ctx(inbox.a, inbox.ana)) as tenant:
        row = Conversation.objects.get(pk=convo)
        commands.record_inbound(
            tenant, contact_id=row.contact_id, channel_account_id=row.channel_account_id,
            external_message_id="wamid.f609", body="¿Tienen arnés talla M?",
        )  # fmt: skip
        commands.add_note(tenant, conversation_id=convo, body="Revisar stock")
    assert ids(ana, unanswered="true") == [convo] and count(ana) == 1
    with tenant_scope(ctx(inbox.a, inbox.ana)) as tenant:
        send(tenant, conversation_id=convo, client_message_id=uuid4(), body="Sí, hay")
    assert ids(ana, unanswered="true") == [] and count(ana) == 0


def test_the_index_migration_replaces_an_invalid_index_and_goes_back(
    waiting: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    def state() -> list[Any]:
        return migrator.execute(INDEX.STATE, [INDEX.INDEX]).fetchall()

    with as_migrator():
        call_command("migrate", "conversations", "0036", verbosity=0)
    assert state() == []
    with pytest.raises(psycopg.errors.UniqueViolation):  # un `CONCURRENTLY` fallido: `INVALID`
        migrator.execute(
            f"CREATE UNIQUE INDEX CONCURRENTLY {INDEX.INDEX} ON conversations (organization_id)"
            " WHERE last_message_direction = 'INBOUND'"
        )
    assert state()[0][0] is False
    with as_migrator():
        call_command("migrate", "conversations", verbosity=0)
    assert state() == [(True, INDEX.DEFINITION)]
    assert len(ids(signed(waiting.ana), unanswered="true")) == 3
